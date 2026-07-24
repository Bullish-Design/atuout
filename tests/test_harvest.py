from __future__ import annotations

from pathlib import Path

from atuout import store
from atuout.harvest import harvest
from tests.support.fake_daemon import FakeDaemon


def test_harvest_found_persists(fake_daemon: FakeDaemon, db_file: Path) -> None:
    fake_daemon.add_capture("abc", "line1\nline2\n")
    rec = harvest("abc", command="ls", exit_code=0, db_path=db_file, attempts=2, delay_ms=1)
    assert rec is not None
    assert rec.output == "line1\nline2"
    assert rec.command == "ls"
    assert rec.exit_code == 0
    conn = store.connect(db_file)
    assert store.has_recording(conn, "abc")


def test_harvest_retries_then_succeeds(fake_daemon: FakeDaemon, db_file: Path) -> None:
    # Capture is added only after the first attempt would have missed it.
    import threading

    def add_later() -> None:
        fake_daemon.add_capture("late", "here\n")

    threading.Timer(0.02, add_later).start()
    rec = harvest("late", db_path=db_file, attempts=10, delay_ms=10)
    assert rec is not None
    assert rec.output == "here"


def test_harvest_not_found_returns_none(fake_daemon: FakeDaemon, db_file: Path) -> None:
    rec = harvest("missing", db_path=db_file, attempts=2, delay_ms=1)
    assert rec is None
    conn = store.connect(db_file)
    assert not store.has_recording(conn, "missing")


def test_harvest_daemon_down_does_not_raise(db_file: Path, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ATUOUT_DAEMON_SOCKET", str(tmp_path / "nope.sock"))
    rec = harvest("abc", db_path=db_file, attempts=2, delay_ms=1)
    assert rec is None


def test_harvest_idempotent(fake_daemon: FakeDaemon, db_file: Path) -> None:
    fake_daemon.add_capture("abc", "one\n")
    first = harvest("abc", command="a", db_path=db_file, attempts=2, delay_ms=1)
    # Change the capture; a second harvest should keep the original (already stored).
    fake_daemon.add_capture("abc", "two\n")
    second = harvest("abc", command="b", db_path=db_file, attempts=2, delay_ms=1)
    assert first is not None and second is not None
    assert second.output == "one"
