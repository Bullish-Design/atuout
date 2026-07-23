from __future__ import annotations

from pathlib import Path

from atuout import reconciler, store
from atuout._proto import history_pb2
from atuout.daemon_client import DaemonClient
from atuout.settings import daemon_socket_path
from tests.support.fake_daemon import FakeDaemon


def _entry(id: str, command: str = "ls", exit: int = 0) -> history_pb2.HistoryEntry:
    return history_pb2.HistoryEntry(id=id, command=command, exit=exit)


def test_reconcile_ended_backfills_missing(fake_daemon: FakeDaemon, db_file: Path) -> None:
    fake_daemon.add_capture("m1", "captured\n")
    conn = store.connect(db_file)
    with DaemonClient(daemon_socket_path()) as client:
        stored = reconciler.reconcile_ended(
            conn, client, _entry("m1", "grep x", 3), attempts=2, delay_ms=1
        )
    assert stored is True
    rec = store.get_recording(conn, "m1")
    assert rec is not None
    assert rec.output == "captured\n"
    assert rec.command == "grep x"
    assert rec.exit_code == 3
    assert rec.source == "reconciler"


def test_reconcile_ended_skips_already_present(fake_daemon: FakeDaemon, db_file: Path) -> None:
    conn = store.connect(db_file)
    store.upsert_recording(
        conn, atuin_id="dup", command="a", output="orig\n", exit_code=0,
        total_bytes=5, total_lines=1, captured_at_ms=1, source="fast",
    )
    fake_daemon.add_capture("dup", "different\n")
    with DaemonClient(daemon_socket_path()) as client:
        stored = reconciler.reconcile_ended(conn, client, _entry("dup"), attempts=2, delay_ms=1)
    assert stored is False
    assert store.get_recording(conn, "dup").output == "orig\n"


def test_reconcile_ended_not_found(fake_daemon: FakeDaemon, db_file: Path) -> None:
    conn = store.connect(db_file)
    with DaemonClient(daemon_socket_path()) as client:
        stored = reconciler.reconcile_ended(conn, client, _entry("ghost"), attempts=2, delay_ms=1)
    assert stored is False


def test_single_instance_lock(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ATUOUT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert reconciler.is_running() is False
    lock = reconciler._acquire_lock()
    assert lock is not None
    try:
        assert reconciler.is_running() is True
        assert reconciler._acquire_lock() is None  # second acquire fails
    finally:
        import fcntl

        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    assert reconciler.is_running() is False


def test_ensure_no_spawn_when_running(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    lock = reconciler._acquire_lock()
    assert lock is not None
    try:
        assert reconciler.ensure() is False  # already running → no spawn
    finally:
        import fcntl

        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def test_ensure_spawn_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert reconciler.ensure(spawn=False) is False
