"""Real detached-process integration tests for the reconciler.

Unlike ``test_integration_daemon.py`` (which calls ``reconcile_ended`` in the test process),
these spawn an actual ``atuout reconcile --daemonize`` child and observe its effects entirely
out-of-process: it must boot, hold the flock, write the pidfile, open a ``TailHistory`` stream,
react to a live daemon ``ENDED`` event, and backfill into SQLite with no in-process reconciler
call. Also covers the single-instance guard, clean SIGTERM shutdown, and crash-restart.

Opt-in (``pytest.mark.slow``) because they spawn processes and wait on timers. Skipped when the
``atuin`` binary isn't on PATH, and per-test when the daemon build predates PR #3510 (no Semantic
service, so captures can't be injected).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("atuin") is None, reason="atuin binary not available"),
]

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Daemon fixture (mirrors test_integration_daemon.py::atuin_daemon)
# ---------------------------------------------------------------------------


@pytest.fixture
def atuin_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ATUOUT_DAEMON_SOCKET", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    sock = home / ".local" / "share" / "atuin" / "atuin.sock"

    # Foreground daemon in its own session so teardown can kill the whole process group.
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


@pytest.fixture
def runtime_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, atuin_daemon: str
) -> dict[str, object]:
    """Isolate the reconciler's lock/pidfile and DB under tmp_path for the test process.

    The detached child gets its env from ``_child_env`` (built explicitly), not from monkeypatch;
    these vars are so helpers running in the *test* process (is_running/read_pid/store) resolve to
    the same paths the child uses.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    db = tmp_path / "recon.db"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_dir))
    monkeypatch.setenv("ATUOUT_DB_PATH", str(db))
    monkeypatch.setenv("ATUOUT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATUOUT_DAEMON_SOCKET", atuin_daemon)
    return {"db": db, "sock": atuin_daemon, "run_dir": run_dir}


# ---------------------------------------------------------------------------
# Spawn / teardown helpers
# ---------------------------------------------------------------------------


def _child_env(env: dict[str, object]) -> dict[str, str]:
    e = os.environ.copy()
    e["ATUOUT_DB_PATH"] = str(env["db"])
    e["ATUOUT_DAEMON_SOCKET"] = str(env["sock"])
    e["XDG_RUNTIME_DIR"] = str(env["run_dir"])
    e["ATUOUT_STATE_DIR"] = str(Path(str(env["run_dir"])).parent / "state")
    return e


def _spawn_reconciler(env: dict[str, object]) -> subprocess.Popen[bytes]:
    # Launch via the venv interpreter as a module: no PATH assumption, correct interpreter, and
    # `-m` execs in-process so the child's os.getpid() == proc.pid (used by the read_pid asserts).
    return subprocess.Popen(
        [sys.executable, "-m", "atuout.cli", "reconcile", "--daemonize"],
        env=_child_env(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(REPO_ROOT),
    )


def _kill(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


@pytest.fixture
def reconciler_procs() -> Iterator[list[subprocess.Popen[bytes]]]:
    procs: list[subprocess.Popen[bytes]] = []
    try:
        yield procs
    finally:
        for proc in procs:
            _kill(proc)


def _wait_running(timeout: float = 10.0) -> bool:
    from atuout import reconciler

    deadline = time.time() + timeout
    while time.time() < deadline:
        if reconciler.is_running():
            return True
        time.sleep(0.05)
    return False


def _wait_not_running(timeout: float = 10.0) -> bool:
    from atuout import reconciler

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not reconciler.is_running():
            return True
        time.sleep(0.1)
    return False


def _wait_for_recording(db: Path, atuin_id: str, timeout: float = 10.0) -> object | None:
    from atuout import store

    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = store.connect(db)  # fresh conn per poll → sees the child's committed writes
        try:
            rec = store.get_recording(conn, atuin_id)
        finally:
            conn.close()
        if rec is not None:
            return rec
        time.sleep(0.1)
    return None


# ---------------------------------------------------------------------------
# gRPC history / capture helpers
# ---------------------------------------------------------------------------


def _start_history(sock: str, command: str = "pwd", session: str = "integration-session") -> str:
    import grpc

    from atuout._proto import history_pb2, history_pb2_grpc

    channel = grpc.insecure_channel(
        f"unix:{sock}", options=[("grpc.default_authority", "localhost")]
    )
    reply = history_pb2_grpc.HistoryStub(channel).StartHistory(
        history_pb2.StartHistoryRequest(
            command=command,
            cwd="/tmp",
            session=session,
            hostname="test",
            timestamp=int(time.time() * 1e9),
        ),
        timeout=5,
    )
    return reply.id


def _end_history(sock: str, hid: str, exit_code: int = 0, duration: int = 1000) -> None:
    import grpc

    from atuout._proto import history_pb2, history_pb2_grpc

    channel = grpc.insecure_channel(
        f"unix:{sock}", options=[("grpc.default_authority", "localhost")]
    )
    history_pb2_grpc.HistoryStub(channel).EndHistory(
        history_pb2.EndHistoryRequest(id=hid, exit=exit_code, duration=duration),
        timeout=5,
    )


def _inject_capture(
    sock: str, history_id: str, output: str, *, command: str = "", exit_code: int = 0
) -> int:
    import grpc

    from atuout._proto import semantic_pb2, semantic_pb2_grpc

    channel = grpc.insecure_channel(
        f"unix:{sock}", options=[("grpc.default_authority", "localhost")]
    )
    capture = semantic_pb2.CommandCapture(
        command=command,
        output=output,
        exit_code=exit_code,
        history_id=history_id,
        session_id="integration-session",
    )
    return (
        semantic_pb2_grpc.SemanticStub(channel)
        .RecordCommands(iter([capture]), timeout=5)
        .accepted
    )


def _semantic_available(sock: str) -> bool:
    from atuout.daemon_client import DaemonClient, DaemonError

    with DaemonClient(sock) as client:
        try:
            client.command_output("probe")
            return True
        except DaemonError as e:
            return e.kind != "unimplemented"


def _require_semantic(sock: str) -> None:
    if not _semantic_available(sock):
        pytest.skip("atuin build predates PR #3510 (no Semantic service)")


# ---------------------------------------------------------------------------
# Gating spike: confirm EndHistory broadcasts a live ENDED tail event
# ---------------------------------------------------------------------------


def test_end_history_broadcasts_ended_event(runtime_env: dict[str, object]) -> None:
    """Gate for the whole approach: an open TailHistory stream must observe an ENDED event with
    the entry id shortly after EndHistory. If this fails, the daemon does not broadcast on End and
    the spawn+backfill test can never pass — so prove it in isolation first."""
    import threading

    from atuout._proto import history_pb2
    from atuout.daemon_client import DaemonClient

    sock = str(runtime_env["sock"])
    seen: list[str] = []
    opened = threading.Event()
    stop = threading.Event()

    import grpc

    call_box: list[grpc.Future] = []

    def consume() -> None:
        with DaemonClient(sock) as client:
            call = client.tail_history_call()
            call_box.append(call)
            opened.set()
            with contextlib.suppress(grpc.RpcError):  # cancelled/torn down at test end
                for reply in call:
                    if reply.kind == history_pb2.HISTORY_EVENT_KIND_ENDED:
                        seen.append(reply.history.id)
                    if stop.is_set():
                        break

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    assert opened.wait(timeout=5)
    time.sleep(0.5)  # let the stream attach on the daemon side before firing events

    hid = _start_history(sock)
    _end_history(sock, hid)

    deadline = time.time() + 5
    while time.time() < deadline and hid not in seen:
        time.sleep(0.05)
    stop.set()
    if call_box:
        call_box[0].cancel()  # unblock the consume thread's iterator promptly
    t.join(timeout=5)
    assert hid in seen, "daemon did not broadcast an ENDED tail event on EndHistory"


# ---------------------------------------------------------------------------
# 5.1 Core test: spawn + live ENDED + out-of-process backfill
# ---------------------------------------------------------------------------


def test_spawn_and_backfill(
    runtime_env: dict[str, object],
    reconciler_procs: list[subprocess.Popen[bytes]],
) -> None:
    from atuout import reconciler

    sock = str(runtime_env["sock"])
    db = runtime_env["db"]
    assert isinstance(db, Path)
    _require_semantic(sock)

    proc = _spawn_reconciler(runtime_env)
    reconciler_procs.append(proc)
    assert _wait_running(), "reconciler child never acquired the lock"
    time.sleep(0.75)  # let the tail stream attach before firing history events

    assert reconciler.read_pid() == proc.pid

    hid = _start_history(sock)
    _inject_capture(sock, hid, "out\n", command="pwd", exit_code=0)  # capture BEFORE end
    _end_history(sock, hid, exit_code=0)

    rec = _wait_for_recording(db, hid, timeout=10)
    assert rec is not None
    assert rec.output == "out"
    assert rec.source == "reconciler"
    assert rec.command == "pwd"
    assert rec.exit_code == 0


# ---------------------------------------------------------------------------
# 5.2 Single-instance guarantee
# ---------------------------------------------------------------------------


def test_single_instance_no_duplicate(
    runtime_env: dict[str, object],
    reconciler_procs: list[subprocess.Popen[bytes]],
) -> None:
    from atuout import reconciler

    proc = _spawn_reconciler(runtime_env)
    reconciler_procs.append(proc)
    assert _wait_running()

    result = subprocess.run(
        [sys.executable, "-m", "atuout.cli", "reconcile", "ensure"],
        env=_child_env(runtime_env),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "already running" in result.stdout
    assert reconciler.read_pid() == proc.pid
    assert reconciler.is_running() is True


# ---------------------------------------------------------------------------
# 5.3 Clean SIGTERM shutdown
# ---------------------------------------------------------------------------


def test_stop_clean_shutdown(
    runtime_env: dict[str, object],
    reconciler_procs: list[subprocess.Popen[bytes]],
) -> None:
    from atuout import reconciler

    proc = _spawn_reconciler(runtime_env)
    reconciler_procs.append(proc)
    assert _wait_running()

    result = subprocess.run(
        [sys.executable, "-m", "atuout.cli", "reconcile", "stop"],
        env=_child_env(runtime_env),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "sent stop" in result.stdout

    # The reconciler runs its tail on a worker thread and cancels the in-flight call on SIGTERM,
    # so it shuts down promptly even when the stream is idle (no events).
    assert _wait_not_running(timeout=10)
    assert not reconciler.pidfile_path().exists()
    proc.wait(timeout=5)
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# 5.4 Crash-restart
# ---------------------------------------------------------------------------


def test_crash_restart(
    runtime_env: dict[str, object],
    reconciler_procs: list[subprocess.Popen[bytes]],
) -> None:
    from atuout import reconciler

    proc = _spawn_reconciler(runtime_env)
    reconciler_procs.append(proc)
    assert _wait_running()
    pid1 = proc.pid

    # SIGKILL: no finally runs, so the pidfile is left stale — but the kernel releases the flock
    # when the fd closes on death, so is_running() (lock-based) correctly reports False.
    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    proc.wait(timeout=5)
    assert _wait_not_running(timeout=5)

    proc2 = _spawn_reconciler(runtime_env)
    reconciler_procs.append(proc2)
    assert _wait_running()
    assert reconciler.read_pid() == proc2.pid
    assert proc2.pid != pid1
