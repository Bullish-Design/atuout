"""Live integration test against a real ``atuin daemon``.

Skipped automatically when the ``atuin`` binary isn't on PATH (it is provided by devenv). This
exercises the actual gRPC handshake, socket-path resolution, and History service. The Semantic
capture service (atuin PR #3510) is not in released atuin yet, so CommandOutput is expected to be
``unimplemented`` until it ships — asserted here so we notice when that changes.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from atuout._proto import history_pb2
from atuout.daemon_client import DaemonClient, DaemonError
from atuout.recording import reply_output_text

pytestmark = pytest.mark.skipif(
    shutil.which("atuin") is None, reason="atuin binary not available"
)


@pytest.fixture
def atuin_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ATUOUT_DAEMON_SOCKET", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    sock = home / ".local" / "share" / "atuin" / "atuin.sock"

    # Run the daemon in the foreground in its own session so we can kill the whole process
    # group in teardown (`daemon start` double-forks and detaches, which we can't reap).
    proc = subprocess.Popen(
        ["atuin", "daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not sock.exists():
            time.sleep(0.1)
        if not sock.exists():
            pytest.skip("atuin daemon did not create its socket")
        yield str(sock)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


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


def _semantic_available(sock: str) -> bool:
    with DaemonClient(sock) as client:
        try:
            client.command_output("probe")
            return True
        except DaemonError as e:
            return e.kind != "unimplemented"


def test_capture_roundtrip_via_record_commands(atuin_daemon: str) -> None:
    """Inject a capture through Semantic.RecordCommands and read it back via CommandOutput.

    This exercises the real daemon's semantic store end to end through our generated stubs,
    without needing a full pty-proxy OSC-133 session. The daemon matches CommandOutput to a
    capture by its history_id (semantic.rs record_has_history_id).
    """
    import grpc

    from atuout._proto import semantic_pb2, semantic_pb2_grpc

    if not _semantic_available(atuin_daemon):
        pytest.skip("atuin build predates PR #3510 (no Semantic service)")

    channel = grpc.insecure_channel(
        f"unix:{atuin_daemon}", options=[("grpc.default_authority", "localhost")]
    )
    stub = semantic_pb2_grpc.SemanticStub(channel)
    capture = semantic_pb2.CommandCapture(
        prompt="$ ",
        command="echo hi",
        output="hi\nthere\n",
        exit_code=0,
        history_id="integration-test-id",
        session_id="sess",
    )
    accepted = stub.RecordCommands(iter([capture]), timeout=5).accepted
    assert accepted == 1

    with DaemonClient(atuin_daemon) as client:
        reply = client.command_output("integration-test-id")
    assert reply.found is True
    assert reply.total_lines == 2
    assert reply_output_text(reply) == "hi\nthere"
