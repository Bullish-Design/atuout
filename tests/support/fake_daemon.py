"""An in-process fake of atuin's daemon (Semantic + History) over a temp Unix socket.

Used by the harvest/reconciler/client tests so no real ``atuin`` is needed. Push captures with
``add_capture`` and history events with ``emit_history`` / ``end_history``; the servicers serve
them over gRPC exactly like the real daemon's shape.
"""

from __future__ import annotations

import queue
import threading
from concurrent import futures
from pathlib import Path

import grpc

from atuout._proto import (
    history_pb2,
    history_pb2_grpc,
    semantic_pb2,
    semantic_pb2_grpc,
)


class _SemanticServicer(semantic_pb2_grpc.SemanticServicer):
    def __init__(self, captures: dict[str, dict[str, object]], unimplemented: bool) -> None:
        self._captures = captures
        self._unimplemented = unimplemented

    def CommandOutput(self, request, context):  # noqa: N802 (grpc naming)
        if self._unimplemented:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "capture disabled")
        cap = self._captures.get(request.history_id)
        if cap is None:
            return semantic_pb2.CommandOutputReply(found=False)
        output = str(cap["output"])
        lines = output.splitlines()
        # Mirror the real daemon: the top-level `output` field is left empty; content is
        # returned only via `lines` (see atuin-daemon semantic.rs command_output).
        return semantic_pb2.CommandOutputReply(
            found=True,
            output="",
            total_bytes=len(output.encode()),
            total_lines=len(lines),
            lines=[
                semantic_pb2.OutputLine(line_number=i + 1, content=line)
                for i, line in enumerate(lines)
            ],
        )


class _HistoryServicer(history_pb2_grpc.HistoryServicer):
    def __init__(self, events: queue.Queue[history_pb2.TailHistoryReply | None]) -> None:
        self._events = events

    def TailHistory(self, request, context):  # noqa: N802 (grpc naming)
        while context.is_active():
            try:
                reply = self._events.get(timeout=0.1)
            except queue.Empty:
                continue
            if reply is None:  # sentinel → close the stream
                return
            yield reply


class FakeDaemon:
    def __init__(self, socket_path: str, *, unimplemented: bool = False) -> None:
        self._socket_path = socket_path
        self._captures: dict[str, dict[str, object]] = {}
        self._events: queue.Queue[history_pb2.TailHistoryReply | None] = queue.Queue()
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        semantic_pb2_grpc.add_SemanticServicer_to_server(
            _SemanticServicer(self._captures, unimplemented), self._server
        )
        history_pb2_grpc.add_HistoryServicer_to_server(
            _HistoryServicer(self._events), self._server
        )
        self._server.add_insecure_port(f"unix:{socket_path}")
        self._lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._events.put(None)
        self._server.stop(grace=0).wait()
        Path(self._socket_path).unlink(missing_ok=True)

    def __enter__(self) -> FakeDaemon:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---- test controls ---------------------------------------------------
    def add_capture(self, history_id: str, output: str) -> None:
        with self._lock:
            self._captures[history_id] = {"output": output}

    def emit_history(self, kind: int, *, id: str, command: str = "", exit: int = 0) -> None:
        reply = history_pb2.TailHistoryReply(
            kind=kind,
            history=history_pb2.HistoryEntry(id=id, command=command, exit=exit),
        )
        self._events.put(reply)

    def end_history(self, id: str, command: str = "", exit: int = 0) -> None:
        self.emit_history(
            history_pb2.HISTORY_EVENT_KIND_ENDED, id=id, command=command, exit=exit
        )
