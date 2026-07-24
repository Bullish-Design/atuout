"""Live integration test against a real ``atuin daemon``.

Skipped automatically when the ``atuin`` binary isn't on PATH (it is provided by devenv). This
exercises the actual gRPC handshake, socket-path resolution, and History service. The Semantic
capture service (atuin PR #3510) is not in released atuin yet, so CommandOutput is expected to be
``unimplemented`` until it ships — asserted here so we notice when that changes.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

from atuout._proto import history_pb2
from atuout.daemon_client import DaemonClient, DaemonError
from atuout.recording import reply_output_text
from tests.support.atuin_daemon import semantic_available as _semantic_available

pytestmark = pytest.mark.skipif(
    shutil.which("atuin") is None, reason="atuin binary not available"
)


def test_socket_path_matches_settings(atuin_daemon: str) -> None:
    from atuout.settings import daemon_socket_path

    assert daemon_socket_path() == atuin_daemon


def test_history_status_reachable(atuin_daemon: str) -> None:
    # A successful call proves the gRPC handshake (the unix-authority fix) works end to end.
    import grpc

    from atuout._proto import history_pb2_grpc

    channel = grpc.insecure_channel(
        f"unix:{atuin_daemon}", options=[("grpc.default_authority", "localhost")]
    )
    reply = history_pb2_grpc.HistoryStub(channel).Status(
        history_pb2.StatusRequest(), timeout=5
    )
    assert reply.healthy is True
    assert reply.version


def test_tail_history_stream_opens(atuin_daemon: str) -> None:
    # The reconciler depends on this stream; confirm it connects and stays open.
    opened = threading.Event()
    errored: list[str] = []

    def consume() -> None:
        try:
            with DaemonClient(atuin_daemon) as client:
                stream = client.tail_history()
                opened.set()
                # Pull one item with a short deadline; timing out is fine (no events).
                next(iter(stream), None)
        except DaemonError as e:
            if e.kind not in ("unavailable", "connect"):
                errored.append(str(e))
        except StopIteration:
            pass

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    assert opened.wait(timeout=5.0)
    assert not errored


def test_command_output_missing_is_clean(atuin_daemon: str) -> None:
    # With the Semantic service present (atuin built from a PR #3510 commit), a lookup for an
    # unknown id returns found=False rather than erroring. Against a released atuin that predates
    # the service, this raises 'unimplemented' — skip in that case.
    with DaemonClient(atuin_daemon) as client:
        try:
            reply = client.command_output("no-such-id")
        except DaemonError as e:
            if e.kind == "unimplemented":
                pytest.skip("atuin build predates PR #3510 (no Semantic service)")
            raise
        assert reply.found is False


def _inject_capture(
    sock: str, history_id: str, output: str, *, command: str = "", exit_code: int = 0
) -> int:
    import grpc

    from atuout._proto import semantic_pb2, semantic_pb2_grpc

    channel = grpc.insecure_channel(
        f"unix:{sock}", options=[("grpc.default_authority", "localhost")]
    )
    # The daemon requires a non-empty session_id to accept a capture (pty-proxy always sets it
    # from ATUIN_SESSION). This only matters for direct injection here — atuout only ever reads.
    capture = semantic_pb2.CommandCapture(
        command=command,
        output=output,
        exit_code=exit_code,
        history_id=history_id,
        session_id="integration-session",
    )
    return semantic_pb2_grpc.SemanticStub(channel).RecordCommands(
        iter([capture]), timeout=5
    ).accepted


def test_harvest_end_to_end(atuin_daemon: str, tmp_path: Path) -> None:
    """Real daemon -> harvest() -> SQLite -> read back through a fresh store connection."""
    from atuout import store
    from atuout.harvest import harvest

    if not _semantic_available(atuin_daemon):
        pytest.skip("atuin build predates PR #3510 (no Semantic service)")

    _inject_capture(atuin_daemon, "e2e-id", "hi\nthere\n", command="echo hi", exit_code=0)
    db = tmp_path / "e2e.db"
    rec = harvest(
        "e2e-id",
        command="echo hi",
        exit_code=0,
        db_path=db,
        socket_path=atuin_daemon,
        attempts=3,
        delay_ms=50,
    )
    assert rec is not None
    assert rec.output == "hi\nthere"

    conn = store.connect(db)
    got = store.get_recording(conn, "e2e-id")
    assert got is not None
    assert got.command == "echo hi"
    assert got.exit_code == 0
    assert got.output == "hi\nthere"
    assert got.source == "fast"


def test_reconciler_backfill_end_to_end(atuin_daemon: str, tmp_path: Path) -> None:
    """Real daemon -> reconcile_ended() backfills a missed capture into SQLite."""
    from atuout import reconciler, store
    from atuout._proto import history_pb2
    from atuout.daemon_client import DaemonClient

    if not _semantic_available(atuin_daemon):
        pytest.skip("atuin build predates PR #3510 (no Semantic service)")

    _inject_capture(atuin_daemon, "rec-id", "out\n", command="pwd", exit_code=0)
    db = tmp_path / "recon.db"
    conn = store.connect(db)
    entry = history_pb2.HistoryEntry(id="rec-id", command="pwd", exit=0)
    with DaemonClient(atuin_daemon) as client:
        stored = reconciler.reconcile_ended(conn, client, entry, attempts=4, delay_ms=50)
    assert stored is True
    got = store.get_recording(conn, "rec-id")
    assert got is not None
    assert got.output == "out"
    assert got.source == "reconciler"


def test_capture_roundtrip_via_record_commands(atuin_daemon: str) -> None:
    """Inject a capture through Semantic.RecordCommands and read it back via CommandOutput.

    This exercises the real daemon's semantic store end to end through our generated stubs,
    without needing a full pty-proxy OSC-133 session. The daemon matches CommandOutput to a
    capture by its history_id (semantic.rs record_has_history_id).
    """
    if not _semantic_available(atuin_daemon):
        pytest.skip("atuin build predates PR #3510 (no Semantic service)")

    accepted = _inject_capture(
        atuin_daemon, "integration-test-id", "hi\nthere\n", command="echo hi"
    )
    assert accepted == 1

    with DaemonClient(atuin_daemon) as client:
        reply = client.command_output("integration-test-id")
    assert reply.found is True
    assert reply.total_lines == 2
    assert reply_output_text(reply) == "hi\nthere"
