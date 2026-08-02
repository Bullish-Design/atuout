"""Tests for the agent-transcript ingester (pi / claude-code backfill)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from atuout import agent_ingest, store

# Base epoch ns for the fake session results (2026-08-02T20:00:00Z), matching the
# timestamps emitted by ``_pi_session_lines`` / the claude fixture.
TS0_NS = int(datetime(2026, 8, 2, 20, 0, tzinfo=UTC).timestamp()) * 10**9
TS0_MS = TS0_NS // 10**6


def _pi_session_lines(
    calls: list[tuple[str, str]], *, parallel: bool = False
) -> list[dict]:
    """Build a minimal pi session event stream.

    Each (name, command) becomes a toolCall; outputs are ``"out:<name>"``.
    Sequential mode emits one assistant event + one toolResult per call;
    parallel mode batches all calls into one assistant event followed by one
    toolResult per call.
    """
    events: list[dict] = []
    if parallel:
        assistant = {
            "type": "message",
            "id": "evt-assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": f"call_{i}", "name": name, "arguments": {"command": cmd}}
                    for i, (name, cmd) in enumerate(calls)
                ],
            },
        }
        events.append(assistant)
        for i, (name, _cmd) in enumerate(calls):
            events.append(
                {
                    "type": "message",
                    "id": f"evt-res-{i}",
                    "parentId": "evt-assistant" if i == 0 else f"evt-res-{i-1}",
                    "timestamp": f"2026-08-02T20:0{i}:00.000Z",
                    "message": {
                        "role": "toolResult",
                        "content": [{"type": "text", "text": f"out:{name}"}],
                    },
                }
            )
    else:
        for i, (name, cmd) in enumerate(calls):
            events.append(
                {
                    "type": "message",
                    "id": f"evt-a-{i}",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall", "id": f"call_{i}", "name": name, "arguments": {"command": cmd}}
                        ],
                    },
                }
            )
            events.append(
                {
                    "type": "message",
                    "id": f"evt-r-{i}",
                    "parentId": f"evt-a-{i}",
                    "timestamp": f"2026-08-02T20:0{i}:00.000Z",
                    "message": {
                        "role": "toolResult",
                        "content": [{"type": "text", "text": f"out:{name}"}],
                    },
                }
            )
    return events


def _write_jsonl(path: Path, events: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return path


def _pi_session(home: Path, events: list[dict]) -> Path:
    return _write_jsonl(home / ".pi" / "agent" / "sessions" / "--home--" / "s.jsonl", events)


# ---------------------------------------------------------------------------
# parse_pi_session
# ---------------------------------------------------------------------------


def test_parse_pi_sequential(home_tmp: Path) -> None:
    path = _pi_session(
        home_tmp,
        _pi_session_lines([("bash", "echo one"), ("read", "x"), ("bash", "echo two")]),
    )
    calls = agent_ingest.parse_pi_session(path)
    assert [(c.command, c.output) for c in calls] == [
        ("echo one", "out:bash"),
        ("echo two", "out:bash"),
    ]
    assert calls[0].result_ts_ms is not None


def test_parse_pi_parallel_fifo(home_tmp: Path) -> None:
    """Parallel tool calls pair to results in call order (FIFO)."""
    path = _pi_session(
        home_tmp,
        _pi_session_lines([("bash", "ls -la"), ("bash", "cat x"), ("read", "f")], parallel=True),
    )
    calls = agent_ingest.parse_pi_session(path)
    assert [(c.command, c.output) for c in calls] == [("ls -la", "out:bash"), ("cat x", "out:bash")]


def test_parse_pi_ignores_garbage_and_empty_commands(home_tmp: Path) -> None:
    path = _write_jsonl(
        home_tmp / ".pi" / "agent" / "sessions" / "--home--" / "s.jsonl",
        [
            {"not": "json",
            "type": "message",
            "message": {"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": {}}]}},
            {"type": "message",
             "message": {"role": "toolResult", "content": [{"type": "text", "text": "x"}]}},
        ],
    )
    assert agent_ingest.parse_pi_session(path) == []


# ---------------------------------------------------------------------------
# parse_claude_session
# ---------------------------------------------------------------------------


def test_parse_claude_pairing(home_tmp: Path) -> None:
    path = _write_jsonl(
        home_tmp / ".claude" / "projects" / "-home-" / "c.jsonl",
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "grep x"}},
                        {"type": "tool_use", "id": "tu_2", "name": "Read", "input": {"file_path": "a"}},
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": [{"type": "text", "text": "line1\nline2"}],
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu_2", "content": "not bash"},
                    ]
                },
            },
        ],
    )
    calls = agent_ingest.parse_claude_session(path)
    assert [(c.command, c.output) for c in calls] == [("grep x", "line1\nline2")]


# ---------------------------------------------------------------------------
# match_call
# ---------------------------------------------------------------------------


def test_match_call_nearest_timestamp(home_tmp: Path) -> None:
    index = {
        "ls": [
            agent_ingest.RecoveredCall("ls", "old", result_ts_ms=1000),
            agent_ingest.RecoveredCall("ls", "new", result_ts_ms=10_000),
        ]
    }
    best = agent_ingest.match_call(index, "ls", target_ms=9_000)
    assert best is not None and best.output == "new"


def test_match_call_rejects_outside_window(home_tmp: Path) -> None:
    index = {"ls": [agent_ingest.RecoveredCall("ls", "old", result_ts_ms=0)]}
    assert agent_ingest.match_call(index, "ls", target_ms=10 * 60 * 1000) is None


def test_match_call_untimestamped_accepted(home_tmp: Path) -> None:
    index = {"ls": [agent_ingest.RecoveredCall("ls", "out")]}
    best = agent_ingest.match_call(index, "ls", None)
    assert best is not None and best.output == "out"


# ---------------------------------------------------------------------------
# ingest_entry + backfill
# ---------------------------------------------------------------------------


def test_ingest_entry_stores_agent_home_recording(home_tmp: Path, db_file: Path) -> None:
    _pi_session(home_tmp, _pi_session_lines([("bash", "echo hello")]))
    conn = store.connect(db_file)
    stored = agent_ingest.ingest_entry(
        conn,
        atuin_id="pi1",
        command="echo hello",
        author="pi",
        exit_code=0,
        timestamp_ns=TS0_NS,
    )
    assert stored is True
    rec = store.get_recording(conn, "pi1")
    assert rec is not None
    assert rec.output == "out:bash"
    assert rec.exit_code == 0
    assert rec.source == "agent-home"


def test_ingest_entry_skips_existing_and_unknown_author(home_tmp: Path, db_file: Path) -> None:
    _pi_session(home_tmp, _pi_session_lines([("bash", "echo hello")]))
    conn = store.connect(db_file)
    store.upsert_recording(
        conn, atuin_id="pi1", command="echo hello", output="already\n", exit_code=0,
        total_bytes=8, total_lines=1, captured_at_ms=1, source="fast",
    )
    assert agent_ingest.ingest_entry(
        conn, atuin_id="pi1", command="echo hello", author="pi", exit_code=0, timestamp_ns=TS0_NS
    ) is False
    assert agent_ingest.ingest_entry(
        conn, atuin_id="x2", command="echo hello", author="nobody", exit_code=0, timestamp_ns=1
    ) is False


def test_backfill_from_history_and_sessions(home_tmp: Path, db_file: Path, data_home_tmp: Path) -> None:
    _pi_session(home_tmp, _pi_session_lines([("bash", "echo backfill")]))
    db = data_home_tmp / "atuin" / "history.db"
    db.parent.mkdir(parents=True)
    src = sqlite3.connect(str(db))
    src.execute(
        "CREATE TABLE history (id TEXT, command TEXT, author TEXT, exit INTEGER, timestamp INTEGER, deleted_at TEXT)"
    )
    src.execute(
        "INSERT INTO history VALUES ('h1', 'echo backfill', 'pi', 0, ?, NULL)", (TS0_NS,)
    )
    src.execute(
        "INSERT INTO history VALUES ('h2', 'missing command', 'pi', 1, 3000000000, NULL)"
    )
    src.commit()
    src.close()

    conn = store.connect(db_file)
    assert agent_ingest.backfill(conn) == 1
    rec = store.get_recording(conn, "h1")
    assert rec is not None and rec.output == "out:bash" and rec.source == "agent-home"
    assert store.get_recording(conn, "h2") is None


def test_reconcile_ended_ingests_agent_entry(home_tmp: Path, db_file: Path) -> None:
    """The reconciler routes agent-authored ENDED events to the transcript ingester."""
    from atuout import reconciler
    from atuout._proto import history_pb2

    _pi_session(home_tmp, _pi_session_lines([("bash", "echo agent-cmd")]))
    conn = store.connect(db_file)
    entry = history_pb2.HistoryEntry(
        id="agent1", command="echo agent-cmd", author="pi", exit=0, timestamp=TS0_NS
    )
    # The daemon has no capture for it; the ingester recovers from the transcript.
    stored = reconciler.reconcile_ended(conn, None, entry, attempts=2, delay_ms=1)
    assert stored is True
    rec = store.get_recording(conn, "agent1")
    assert rec is not None and rec.output == "out:bash" and rec.source == "agent-home"


def test_backfill_dry_run_counts_without_storing(home_tmp: Path, db_file: Path, data_home_tmp: Path) -> None:
    _pi_session(home_tmp, _pi_session_lines([("bash", "echo backfill")]))
    db = data_home_tmp / "atuin" / "history.db"
    db.parent.mkdir(parents=True)
    src = sqlite3.connect(str(db))
    src.execute(
        "CREATE TABLE history (id TEXT, command TEXT, author TEXT, exit INTEGER, timestamp INTEGER, deleted_at TEXT)"
    )
    src.execute(
        "INSERT INTO history VALUES ('h1', 'echo backfill', 'pi', 0, ?, NULL)", (TS0_NS,)
    )
    src.commit()
    src.close()
    conn = store.connect(db_file)
    assert agent_ingest.backfill(conn, dry_run=True) == 1
    assert store.count_recordings(conn) == 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def data_home_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    return data
