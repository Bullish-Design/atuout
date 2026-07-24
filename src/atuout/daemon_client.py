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
        # grpcio's C-core derives the HTTP/2 ``:authority`` from the target; over a ``unix:``
        # socket that becomes the socket path, which tonic/hyper (atuin's daemon) rejects as a
        # malformed authority with an immediate RST_STREAM. Pin a valid authority so the
        # handshake succeeds. Verified against a live ``atuin daemon`` (18.16.1).
        self._channel = grpc.insecure_channel(
            f"unix:{socket_path}",
            options=[("grpc.default_authority", "localhost")],
        )

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

    def status(self) -> history_pb2.StatusReply:
        """Fetch daemon health/version/protocol (History.Status)."""
        stub = history_pb2_grpc.HistoryStub(self._channel)
        try:
            return stub.Status(history_pb2.StatusRequest(), timeout=CALL_TIMEOUT_S)
        except grpc.RpcError as e:
            raise DaemonError(str(e), kind=_classify(e)) from e

    def tail_history(self) -> Iterator[history_pb2.TailHistoryReply]:
        """Yield a TailHistoryReply for every history STARTED/ENDED event (long-lived)."""
        stub = history_pb2_grpc.HistoryStub(self._channel)
        try:
            yield from stub.TailHistory(history_pb2.TailHistoryRequest())
        except grpc.RpcError as e:
            raise DaemonError(str(e), kind=_classify(e)) from e

    def tail_history_call(self) -> grpc.Future:
        """Return the raw streaming call for History.TailHistory.

        Unlike ``tail_history``, this exposes the underlying grpc call object so a caller can
        ``cancel()`` it from another thread to unblock a thread parked in its iterator — grpcio's
        blocking iteration is not interruptible by Python signals, so cancellation is the only
        reliable way to stop it promptly. Iterating it raises ``grpc.RpcError`` on cancel/disconnect.
        """
        stub = history_pb2_grpc.HistoryStub(self._channel)
        return stub.TailHistory(history_pb2.TailHistoryRequest())
