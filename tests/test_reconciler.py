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
    assert rec.output == "captured"
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


# ---------------------------------------------------------------------------
# Agent retry queue
# ---------------------------------------------------------------------------


def _agent_entry(id: str = "a1", command: str = "echo hi") -> history_pb2.HistoryEntry:
    return history_pb2.HistoryEntry(
        id=id, command=command, exit=0, author="claude-code", timestamp=1
    )


class _Clock:
    """Manual monotonic clock so retry scheduling is testable without sleeping."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_agent_queue_holds_entry_until_due() -> None:
    clock = _Clock()
    q = reconciler._AgentRetryQueue(delays=(5.0, 10.0), now=clock)
    q.add(_agent_entry())
    assert q.take_due() == []  # not due yet
    clock.t = 5.0
    due = q.take_due()
    assert [p.atuin_id for p in due] == ["a1"]
    assert len(q) == 0  # taking removes it


def test_agent_queue_requeue_until_attempts_exhausted() -> None:
    clock = _Clock()
    q = reconciler._AgentRetryQueue(delays=(1.0, 2.0), now=clock)
    q.add(_agent_entry())
    clock.t = 1.0
    (pending,) = q.take_due()
    assert q.requeue(pending) is True  # second and last delay
    clock.t = 3.0
    (pending,) = q.take_due()
    assert q.requeue(pending) is False  # out of attempts → dropped
    assert len(q) == 0


def test_agent_queue_next_delay_tracks_soonest() -> None:
    clock = _Clock()
    q = reconciler._AgentRetryQueue(delays=(30.0,), now=clock)
    assert q.next_delay(5.0) == 5.0  # empty → caller's cap
    q.add(_agent_entry())
    assert q.next_delay(60.0) == 30.0
    assert q.next_delay(5.0) == 5.0  # never longer than the cap


def test_agent_queue_deduplicates_and_is_bounded() -> None:
    q = reconciler._AgentRetryQueue(delays=(1.0,), max_items=1)
    assert q.add(_agent_entry("same")) is True
    assert q.add(_agent_entry("same")) is False
    assert q.add(_agent_entry("other")) is False
    assert len(q) == 1


def test_reconcile_ended_enqueues_agent_miss(fake_daemon: FakeDaemon, db_file: Path, tmp_path, monkeypatch) -> None:
    """An agent entry with no transcript match goes to the retry queue, not the floor."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    conn = store.connect(db_file)
    q = reconciler._AgentRetryQueue()
    with DaemonClient(daemon_socket_path()) as client:
        stored = reconciler.reconcile_ended(conn, client, _agent_entry(), pending=q)
    assert stored is False
    assert len(q) == 1


def test_drain_agent_retries_stores_once_transcript_lands(db_file: Path, tmp_path, monkeypatch) -> None:
    """The retry succeeds after the agent writes its transcript."""
    import json
    from datetime import UTC, datetime

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    conn = store.connect(db_file)
    ts_ns = int(datetime(2026, 8, 2, 20, 0, tzinfo=UTC).timestamp()) * 10**9
    entry = history_pb2.HistoryEntry(
        id="late", command="echo late", exit=0, author="claude-code", timestamp=ts_ns
    )
    q = reconciler._AgentRetryQueue(delays=(0.0, 0.0), now=lambda: 0.0)
    q.add(entry)
    assert reconciler.drain_agent_retries(conn, q) == 0  # transcript not written yet

    session = home / ".claude" / "projects" / "p" / "s.jsonl"
    session.parent.mkdir(parents=True)
    events = [
        {
            "timestamp": "2026-08-02T20:00:00.000Z",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo late"}}
                ]
            },
        },
        {"message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "late output"}]}},
    ]
    with session.open("w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")

    assert reconciler.drain_agent_retries(conn, q) == 1
    rec = store.get_recording(conn, "late")
    assert rec is not None and rec.output == "late output" and rec.source == "agent-home"
