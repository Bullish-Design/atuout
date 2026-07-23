"""gRPC client for atuin's daemon (Semantic + History services) over its Unix socket."""

from __future__ import annotations

from collections.abc import Iterator

import grpc

from atuout._proto import (
    history_pb2,
    history_pb2_grpc,
    semantic_pb2,
    semantic_pb2_grpc,
)

# Short deadline for point lookups; the daemon is local so this is generous.
CALL_TIMEOUT_S = 1.0


class DaemonError(Exception):
    """A failure talking to the atuin daemon.

    ``kind`` mirrors atuin's ``DaemonClientErrorKind``:
    ``connect`` / ``unavailable`` / ``unimplemented`` / ``other``.
    ``kind in {"connect", "unavailable"}`` is retryable.
    """

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def retryable(self) -> bool:
        return self.kind in ("connect", "unavailable")


def _classify(error: grpc.RpcError) -> str:
    code = error.code()
    if code == grpc.StatusCode.UNAVAILABLE:
        return "unavailable"
    if code == grpc.StatusCode.UNIMPLEMENTED:
        return "unimplemented"
    return "other"


class DaemonClient:
    """Thin blocking gRPC client. One channel per process; safe to reuse across calls."""

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._channel = grpc.insecure_channel(f"unix:{socket_path}")

    def close(self) -> None:
        self._channel.close()

    def __enter__(self) -> DaemonClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def command_output(self, history_id: str) -> semantic_pb2.CommandOutputReply:
        """Fetch the full captured output for ``history_id`` (empty ranges = full output)."""
        stub = semantic_pb2_grpc.SemanticStub(self._channel)
        request = semantic_pb2.CommandOutputRequest(history_id=history_id, ranges=[])
        try:
            return stub.CommandOutput(request, timeout=CALL_TIMEOUT_S)
        except grpc.RpcError as e:
            raise DaemonError(str(e), kind=_classify(e)) from e

    def tail_history(self) -> Iterator[history_pb2.TailHistoryReply]:
        """Yield a TailHistoryReply for every history STARTED/ENDED event (long-lived)."""
        stub = history_pb2_grpc.HistoryStub(self._channel)
        try:
            yield from stub.TailHistory(history_pb2.TailHistoryRequest())
        except grpc.RpcError as e:
            raise DaemonError(str(e), kind=_classify(e)) from e
