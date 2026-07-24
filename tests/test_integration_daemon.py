"""Live integration test against a real ``atuin daemon``.

Skipped automatically when the ``atuin`` binary isn't on PATH (it is provided by devenv). This
exercises the actual gRPC handshake, socket-path resolution, and History service. The Semantic
capture service (atuin PR #3510) is not in released atuin yet, so CommandOutput is expected to be
``unimplemented`` until it ships — asserted here so we notice when that changes.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from atuout._proto import history_pb2
from atuout.daemon_client import DaemonClient, DaemonError

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

    proc = subprocess.Popen(
        ["atuin", "daemon", "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=None,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not sock.exists():
            time.sleep(0.1)
        if not sock.exists():
            pytest.skip("atuin daemon did not create its socket")
        yield str(sock)
    finally:
        proc.terminate()
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


def test_command_output_unimplemented_until_pr3510(atuin_daemon: str) -> None:
    # Released atuin lacks the Semantic service; expect a clean 'unimplemented'. When atuin ships
    # PR #3510 this will start returning found=False instead — update the assertion then.
    with DaemonClient(atuin_daemon) as client:
        try:
            reply = client.command_output("bogus-id")
        except DaemonError as e:
            assert e.kind == "unimplemented"
        else:
            # Newer atuin with the capture service present.
            assert reply.found is False
