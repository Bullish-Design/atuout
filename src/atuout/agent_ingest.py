"""Backfill outputs for agent-run commands from agent session transcripts.

Atuin's ``atuin hook install`` records agent bash commands as *metadata-only*
history entries (command, exit code, ``author`` = pi/claude-code/codex) — the
hook path has no output capture. The agents themselves persist full session
transcripts (command **and** output) in their home directories, so this module
correlates history entries with those transcripts and stores the recovered
output as atuout recordings (``source="agent-home"``).

Supported agents / transcript formats:

* **pi** — ``~/.pi/agent/sessions/<cwd-slug>/<session>.jsonl``. Bash tool calls
  are ``message.content[].toolCall`` events (``name="bash"``, ``arguments.command``);
  outputs are ``role="toolResult"`` messages. Results carry no call id, so calls
  are paired to results **FIFO within each assistant batch** (pi runs parallel
  calls; results arrive in call order).
* **claude** — ``~/.claude/projects/<cwd-slug>/<session>.jsonl``. ``tool_use``
  (``name="Bash"``, ``input.command``) paired to ``tool_result`` by
  ``tool_use_id`` — exact, no ordering heuristics needed.
* **codex** — not yet implemented (``response_item``/``custom_tool_call``
  transcripts exist but need format verification).

Correlation with atuin history is by exact command text plus result-timestamp
proximity to the history entry's timestamp.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from atuout import store

AGENT_AUTHORS = ("pi", "claude-code", "codex")

# How close (ms) a recovered result timestamp must be to the history entry's
# timestamp for us to consider it a match, when the command text matches.
_MATCH_WINDOW_MS = 60_000


@dataclass
class RecoveredCall:
    """A bash tool call recovered from an agent session transcript."""

    command: str
    output: str
    result_ts_ms: int | None = None


# ---------------------------------------------------------------------------
# Per-agent transcript parsers
# ---------------------------------------------------------------------------


def parse_pi_session(path: Path) -> list[RecoveredCall]:
    """Parse a pi session JSONL, pairing bash tool calls to results FIFO.

    pi batches parallel tool calls into one assistant message; the following
    ``toolResult`` messages arrive in call order (the first result's parentId
    points at the assistant event, subsequent ones chain to the previous
    result). A single FIFO queue over the whole file handles this.
    """
    calls: list[RecoveredCall] = []
    pending: list[tuple[str, str]] = []  # (tool name, command)
    for event in _iter_json(path):
        if event.get("type") != "message":
            continue
        msg = event.get("message", {})
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        parts = [p for p in content if isinstance(p, dict)]
        role = msg.get("role")

        if role == "assistant":
            for part in parts:
                if part.get("type") == "toolCall":
                    arguments = part.get("arguments") or {}
                    command = arguments.get("command")
                    pending.append((part.get("name") or "", command or ""))
        elif role == "toolResult" and pending:
            name, command = pending.pop(0)
            if name == "bash" and command:
                text = _join_text(parts)
                result_ts_ms = _ts_millis(event.get("timestamp"))
                calls.append(RecoveredCall(command=command, output=text, result_ts_ms=result_ts_ms))
    return calls


def parse_claude_session(path: Path) -> list[RecoveredCall]:
    """Parse a Claude Code session JSONL (tool_use_id pairing, exact)."""
    calls: list[RecoveredCall] = []
    commands: dict[str, tuple[str, int | None]] = {}  # tool_use_id -> (command, ts_ms)
    for event in _iter_json(path):
        msg = event.get("message", {})
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "tool_use":
                tool_id = part.get("id")
                if part.get("name") == "Bash" and tool_id:
                    command = (part.get("input") or {}).get("command") or ""
                    commands[tool_id] = (command, _ts_millis(event.get("timestamp")))
            elif ptype == "tool_result":
                tool_id = part.get("tool_use_id")
                if tool_id and tool_id in commands:
                    command, ts_ms = commands.pop(tool_id)
                    output = _claude_result_text(part.get("content"))
                    calls.append(RecoveredCall(command=command, output=output, result_ts_ms=ts_ms))
    return calls


def _claude_result_text(content: object) -> str:
    """Claude tool_result content is a string, a list of text blocks, or a dict."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n".join(parts)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


# ---------------------------------------------------------------------------
# Session file discovery + indexing
# ---------------------------------------------------------------------------


def _agent_home(author: str) -> Path | None:
    home = Path.home()
    if author == "pi":
        return home / ".pi" / "agent" / "sessions"
    if author == "claude-code":
        return home / ".claude" / "projects"
    return None  # codex: not implemented yet


def iter_session_files(author: str) -> Iterator[Path]:
    """Yield session JSONL files for an agent, newest mtime first."""
    base = _agent_home(author)
    if base is None:
        return
    files = [p for p in base.rglob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    yield from files


def build_index(authors: tuple[str, ...] = AGENT_AUTHORS) -> dict[str, list[RecoveredCall]]:
    """Parse every session transcript for ``authors`` into a command-keyed index."""
    index: dict[str, list[RecoveredCall]] = {}
    for author in authors:
        parser = parse_pi_session if author == "pi" else parse_claude_session
        for path in iter_session_files(author):
            for call in parser(path):
                index.setdefault(call.command.rstrip(), []).append(call)
    return index


def match_call(index: dict[str, list[RecoveredCall]], command: str, target_ms: int | None) -> RecoveredCall | None:
    """Find the recovered call for ``command`` nearest ``target_ms`` (entry time)."""
    candidates = index.get(command.rstrip())
    if not candidates:
        return None
    scored: list[tuple[int | None, RecoveredCall]] = [
        (
            abs(call.result_ts_ms - target_ms) if (call.result_ts_ms is not None and target_ms) else None,
            call,
        )
        for call in candidates
    ]
    timed = [s for s in scored if s[0] is not None]
    if timed:
        best_delta, best = min(timed, key=lambda s: s[0])
        if target_ms and best_delta > _MATCH_WINDOW_MS:
            return None
        return best
    return candidates[0]  # no timestamps anywhere; accept the first


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def ingest_entry(
    conn: sqlite3.Connection,
    *,
    atuin_id: str,
    command: str,
    author: str,
    exit_code: int | None,
    timestamp_ns: int | None,
) -> bool:
    """Recover one agent-run command's output and store it. True if stored."""
    if store.has_recording(conn, atuin_id):
        return False
    if author not in AGENT_AUTHORS:
        return False
    target_ms = (timestamp_ns or 0) // 1_000_000
    best = match_call(build_index((author,)), command, target_ms)
    if best is None:
        return False

    store.upsert_recording(
        conn,
        atuin_id=atuin_id,
        command=command,
        output=best.output,
        exit_code=exit_code,
        total_bytes=len(best.output.encode("utf-8")),
        total_lines=best.output.count("\n") + 1 if best.output else 0,
        captured_at_ms=best.result_ts_ms or target_ms or int(time.time() * 1000),
        source="agent-home",
    )
    return True


def atuin_history_db_path() -> Path:
    """Path to atuin's history database (source of agent-authored entries)."""
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "atuin" / "history.db"


def backfill(
    conn: sqlite3.Connection,
    *,
    authors: tuple[str, ...] = AGENT_AUTHORS,
    limit: int | None = None,
    dry_run: bool = False,
) -> int:
    """Scan atuin history for agent-authored entries missing recordings and ingest them.

    Returns the number of entries ingested (or that would be ingested with
    ``dry_run=True``).
    """
    db = atuin_history_db_path()
    if not db.exists():
        return 0
    try:
        src = sqlite3.connect(str(db))
    except sqlite3.Error:
        return 0
    src.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in authors)
        sql = (
            "SELECT id, command, author, exit, timestamp FROM history "
            f"WHERE author IN ({placeholders}) AND deleted_at IS NULL "
            "ORDER BY timestamp DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
        params: tuple[object, ...] = authors + ((limit,) if limit is not None else ())
        rows = src.execute(sql, params).fetchall()
    finally:
        src.close()

    index = build_index(authors)
    ingested = 0
    for row in rows:
        if store.has_recording(conn, row["id"]):
            continue
        if dry_run:
            ingested += 1
            continue
        target_ms = (row["timestamp"] or 0) // 1_000_000
        best = match_call(index, row["command"] or "", target_ms)
        if best is None:
            continue
        store.upsert_recording(
            conn,
            atuin_id=row["id"],
            command=row["command"] or "",
            output=best.output,
            exit_code=row["exit"],
            total_bytes=len(best.output.encode("utf-8")),
            total_lines=best.output.count("\n") + 1 if best.output else 0,
            captured_at_ms=best.result_ts_ms or target_ms or int(time.time() * 1000),
            source="agent-home",
        )
        ingested += 1
    return ingested


def _iter_json(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL file, skipping bad lines."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return


def _join_text(parts: list[dict]) -> str:
    return "".join(p.get("text", "") for p in parts if isinstance(p.get("text"), str))


def _ts_millis(iso: object) -> int | None:
    """Convert an ISO-8601 timestamp to epoch milliseconds (UTC)."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None
