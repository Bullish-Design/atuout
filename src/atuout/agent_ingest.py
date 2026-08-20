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
* **codex** — ``~/.codex/sessions/<date>/rollout-<session>.jsonl``. Shell calls
  are ``payload.function_call`` (``name="exec_command"``) or
  ``payload.custom_tool_call`` (``name="exec"``), paired to ``*_call_output``
  by ``call_id`` — exact. See :func:`parse_codex_session` for the two shapes.

Correlation with atuin history is by exact command text plus result-timestamp
proximity to the history entry's timestamp.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atuout import store

AGENT_AUTHORS = ("pi", "claude-code", "codex")

# How close (ms) a recovered result timestamp must be to the history entry's
# timestamp for us to consider it a match, when the command text matches.
_MATCH_WINDOW_MS = 60_000

# The live path (reconciler) only ever needs the session file the agent is
# writing right now. Parse transcripts touched within this window of the
# entry's timestamp; older ones cannot hold the call we want.
_LIVE_TRANSCRIPT_SLACK_S = 300.0

# Live ingestion is deliberately a small, rolling cache.  A command that has
# not appeared in the last ten minutes cannot match a live ENDED event (the
# retry schedule is five minutes and matching itself is one minute wide).
_LIVE_INDEX_REFRESH_S = 1.0
_LIVE_INDEX_RETENTION_S = 10 * 60.0
_LIVE_INDEX_MAX_FILES = 512
_LIVE_INDEX_MAX_CALLS_PER_FILE = 4096

_direct_indexes: dict[str, TranscriptIndex] = {}


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


def parse_codex_session(path: Path) -> list[RecoveredCall]:
    """Parse a Codex rollout JSONL (``call_id`` pairing, exact).

    Codex records shell work in two shapes, both under ``event.payload``:

    * ``function_call`` with ``name="exec_command"`` — ``arguments`` is a JSON
      string holding the shell command in ``cmd``.
    * ``custom_tool_call`` with ``name="exec"`` — ``input`` is a JavaScript
      snippet that calls ``tools.exec_command({cmd: "..."})``. One snippet can
      run several commands but the transcript keeps only one combined output,
      so snippets with more than one command are skipped: their output cannot
      be attributed to a single history entry.

    Both pair to a ``function_call_output`` / ``custom_tool_call_output`` by
    ``call_id``.
    """
    calls: list[RecoveredCall] = []
    commands: dict[str, tuple[str, int | None]] = {}  # call_id -> (command, ts_ms)
    for event in _iter_json(path):
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        ptype = payload.get("type")

        if ptype == "function_call" and payload.get("name") == "exec_command":
            command = _codex_argument_cmd(payload.get("arguments"))
            if command:
                commands[call_id] = (command, _ts_millis(event.get("timestamp")))
        elif ptype == "custom_tool_call" and payload.get("name") == "exec":
            raw_input = payload.get("input")
            found = _codex_script_commands(raw_input if isinstance(raw_input, str) else "")
            if len(found) == 1:
                commands[call_id] = (found[0], _ts_millis(event.get("timestamp")))
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            pending = commands.pop(call_id, None)
            if pending is None:
                continue
            command, ts_ms = pending
            output = _codex_output_text(payload.get("output"))
            calls.append(RecoveredCall(command=command, output=output, result_ts_ms=ts_ms))
    return calls


def _codex_argument_cmd(arguments: object) -> str:
    """Read ``cmd`` out of a codex ``function_call.arguments`` JSON string."""
    if not isinstance(arguments, str):
        return ""
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return ""
    if isinstance(parsed, dict) and isinstance(parsed.get("cmd"), str):
        return parsed["cmd"]
    return ""


# ``tools.exec_command({cmd: "...` — the key is sometimes quoted, sometimes not.
_CODEX_CMD_KEY_RE = re.compile(r"""exec_command\(\s*\{\s*(?:"cmd"|'cmd'|cmd)\s*:\s*""")
# The harness prepends a status block to every output: "Script completed\n
# Wall time 0.3 seconds\nOutput:\n". It is not command output; drop it.
_CODEX_HEADER_RE = re.compile(r"\AScript completed\n.*\nOutput:\n?\Z", re.DOTALL)

_JS_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0",
}


def _codex_script_commands(script: str) -> list[str]:
    """Extract every shell command an ``exec`` snippet passes to exec_command."""
    commands: list[str] = []
    for match in _CODEX_CMD_KEY_RE.finditer(script):
        literal = _js_string_at(script, match.end())
        if literal is not None:
            commands.append(literal)
    return commands


def _js_string_at(src: str, pos: int) -> str | None:
    """Read the JavaScript string literal that starts at ``src[pos]``.

    Handles the three quote forms and the escapes that turn up in shell
    commands. Returns None if ``pos`` is not a quote or the literal is
    unterminated (a template literal with ``${}`` keeps the placeholder text).
    """
    if pos >= len(src) or src[pos] not in "\"'`":
        return None
    quote = src[pos]
    out: list[str] = []
    i = pos + 1
    while i < len(src):
        char = src[i]
        if char == "\\":
            nxt = src[i + 1] if i + 1 < len(src) else ""
            if nxt == "u" and _is_hex(src[i + 2 : i + 6]):
                out.append(chr(int(src[i + 2 : i + 6], 16)))
                i += 6
                continue
            if nxt == "x" and _is_hex(src[i + 2 : i + 4]):
                out.append(chr(int(src[i + 2 : i + 4], 16)))
                i += 4
                continue
            out.append(_JS_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if char == quote:
            return "".join(out)
        out.append(char)
        i += 1
    return None


def _is_hex(text: str) -> bool:
    return len(text) > 0 and all(c in "0123456789abcdefABCDEF" for c in text)


def _codex_output_text(output: object) -> str:
    """Join a codex output block list, dropping the harness status header."""
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return ""
    texts: list[str] = []
    for block in output:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    if texts and _CODEX_HEADER_RE.match(texts[0]):
        texts = texts[1:]
    return "\n".join(texts)


def _claude_result_text(content: object) -> str:
    """Claude tool_result content is a string, a list of text blocks, or a dict."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    return ""


# ---------------------------------------------------------------------------
# Session file discovery + indexing
# ---------------------------------------------------------------------------


_PARSERS: dict[str, Callable[[Path], list[RecoveredCall]]] = {
    "pi": parse_pi_session,
    "claude-code": parse_claude_session,
    "codex": parse_codex_session,
}


def _agent_home(author: str) -> Path | None:
    home = Path.home()
    if author == "pi":
        return home / ".pi" / "agent" / "sessions"
    if author == "claude-code":
        return home / ".claude" / "projects"
    if author == "codex":
        return home / ".codex" / "sessions"
    return None


def iter_session_files(author: str, *, modified_since_s: float | None = None) -> Iterator[Path]:
    """Yield session JSONL files for an agent, newest mtime first.

    ``modified_since_s`` (epoch seconds) drops transcripts untouched since then.
    """
    base = _agent_home(author)
    if base is None:
        return
    files: list[tuple[float, Path]] = []
    for path in base.rglob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:  # rotated or removed while we scanned
            continue
        if modified_since_s is not None and mtime < modified_since_s:
            continue
        files.append((mtime, path))
    files.sort(key=lambda item: item[0], reverse=True)
    for _mtime, path in files:
        yield path


@dataclass
class RefreshStats:
    """Counters from one live-index refresh, useful for diagnostics and tests."""

    discovered: int = 0
    reparsed: int = 0
    reused: int = 0
    evicted: int = 0
    errors: int = 0


@dataclass
class _IndexedFile:
    mtime_ns: int
    size: int
    calls: list[RecoveredCall] = field(default_factory=list)


class TranscriptIndex:
    """Reusable, bounded index for transcripts used by the live reconciler.

    Discovery is throttled, unchanged files are not opened, and only the most
    recent files/calls are retained.  A file being appended to is reparsed on
    its next refresh because both size and nanosecond mtime are part of its
    identity.  Parsing failures are isolated to that file and never escape to
    the tail loop.
    """

    def __init__(
        self,
        authors: tuple[str, ...] = AGENT_AUTHORS,
        *,
        refresh_interval_s: float = _LIVE_INDEX_REFRESH_S,
        retention_s: float = _LIVE_INDEX_RETENTION_S,
        max_files: int = _LIVE_INDEX_MAX_FILES,
        max_calls_per_file: int = _LIVE_INDEX_MAX_CALLS_PER_FILE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.authors = authors
        self.refresh_interval_s = refresh_interval_s
        self.retention_s = retention_s
        self.max_files = max_files
        self.max_calls_per_file = max_calls_per_file
        self._clock = clock
        self._last_refresh = float("-inf")
        self._files: dict[Path, _IndexedFile] = {}
        self._index: dict[str, list[RecoveredCall]] = {}
        self.last_stats = RefreshStats()

    @property
    def file_count(self) -> int:
        return len(self._files)

    @property
    def call_count(self) -> int:
        return sum(len(calls) for calls in self._index.values())

    def refresh(
        self,
        *,
        min_timestamp_ms: int | None = None,
        force: bool = False,
    ) -> RefreshStats:
        """Refresh changed/new live files, unless the refresh interval has not elapsed."""
        now = self._clock()
        if not force and now - self._last_refresh < self.refresh_interval_s:
            self.last_stats = RefreshStats()
            return self.last_stats
        self._last_refresh = now
        stats = RefreshStats()
        cutoff = now - self.retention_s
        if min_timestamp_ms is not None:
            cutoff = min(cutoff, min_timestamp_ms / 1000.0 - _LIVE_TRANSCRIPT_SLACK_S)

        candidates: list[tuple[int, Path]] = []
        seen: set[Path] = set()
        for author in self.authors:
            for path in iter_session_files(author):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < cutoff:
                    continue
                candidates.append((stat.st_mtime_ns, path))
                seen.add(path)
        candidates.sort(reverse=True)
        candidates = candidates[: self.max_files]
        stats.discovered = len(candidates)

        for _mtime_ns, path in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            cached = self._files.get(path)
            if cached is not None and (cached.mtime_ns, cached.size) == (stat.st_mtime_ns, stat.st_size):
                stats.reused += 1
                continue
            author = next((a for a in self.authors if path.is_relative_to(_agent_home(a) or path)), None)
            parser = _PARSERS.get(author or "")
            if parser is None:
                continue
            try:
                calls = parser(path)
                # The newest calls are the only calls relevant to the live path.
                calls = calls[-self.max_calls_per_file :]
            except Exception:
                # A malformed shape in one vendor's evolving transcript
                # format must not abort refreshes for every other session.
                stats.errors += 1
                calls = []
            self._files[path] = _IndexedFile(stat.st_mtime_ns, stat.st_size, calls)
            stats.reparsed += 1

        keep = {path for _mtime, path in candidates}
        for path in list(self._files):
            if path not in keep or path not in seen:
                del self._files[path]
                stats.evicted += 1
        self._rebuild_index()
        self.last_stats = stats
        return stats

    def _rebuild_index(self) -> None:
        self._index = {}
        for state in self._files.values():
            for call in state.calls:
                self._index.setdefault(call.command.rstrip(), []).append(call)

    def match(self, command: str, target_ms: int | None) -> RecoveredCall | None:
        return match_call(self._index, command, target_ms)


def build_index(
    authors: tuple[str, ...] = AGENT_AUTHORS,
    *,
    modified_since_s: float | None = None,
    use_cache: bool = False,
) -> dict[str, list[RecoveredCall]]:
    """Parse the session transcripts for ``authors`` into a command-keyed index.

    ``modified_since_s`` limits the scan to recently written transcripts.
    ``use_cache`` reuses parses of unchanged files across calls; it also drops
    cache entries the scan no longer covers, so the cache stays bounded.
    """
    index: dict[str, list[RecoveredCall]] = {}
    for author in authors:
        parser = _PARSERS.get(author)
        if parser is None:
            continue
        for path in iter_session_files(author, modified_since_s=modified_since_s):
            # ``use_cache`` is retained for API compatibility.  Live callers
            # use TranscriptIndex; one-shot backfills should not retain an
            # unbounded process-global cache.
            calls = parser(path)
            for call in calls:
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
        best_delta, best = min(timed, key=lambda s: s[0] or 0)
        if target_ms and best_delta is not None and best_delta > _MATCH_WINDOW_MS:
            return None
        return best
    return candidates[0]  # no timestamps anywhere; accept the first


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def _output_line_count(output: str) -> int:
    """Return the line count used by Recording.output_lines."""
    return len(output.splitlines())


def ingest_entry(
    conn: sqlite3.Connection,
    *,
    atuin_id: str,
    command: str,
    author: str,
    exit_code: int | None,
    timestamp_ns: int | None,
    index: TranscriptIndex | None = None,
) -> bool:
    """Recover one agent-run command's output and store it. True if stored."""
    if store.has_recording(conn, atuin_id):
        return False
    if author not in AGENT_AUTHORS:
        return False
    target_ms = (timestamp_ns or 0) // 1_000_000
    # The agent appends the result to its transcript *after* the shell hook
    # records the command, so the file we want was written at or after
    # ``target_ms``. Scanning only that window keeps the live path cheap.
    if index is None:
        # Direct/CLI users do not have a reconciler-owned worker. Keep a small
        # reusable index for them too, keyed by the current HOME-derived roots.
        index = _direct_indexes.setdefault(author, TranscriptIndex((author,)))
    index.refresh(min_timestamp_ms=target_ms)
    best = index.match(command, target_ms)
    if best is None:
        return False

    store.upsert_recording(
        conn,
        atuin_id=atuin_id,
        command=command,
        output=best.output,
        exit_code=exit_code,
        total_bytes=len(best.output.encode("utf-8")),
        total_lines=_output_line_count(best.output),
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
    since_ms: int | None = None,
    dry_run: bool = False,
    index: TranscriptIndex | None = None,
) -> int:
    """Scan atuin history for agent-authored entries missing recordings and ingest them.

    ``since_ms`` bounds the sweep to history newer than that epoch time, and
    narrows the transcript scan to match — use it for a periodic safety-net
    pass. Without it the whole history and every transcript are read.

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
            f"WHERE author IN ({placeholders}) AND deleted_at IS NULL"
        )
        params: tuple[object, ...] = authors
        if since_ms is not None:
            sql += " AND timestamp >= ?"
            params += (since_ms * 1_000_000,)
        sql += " ORDER BY timestamp DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        rows = src.execute(sql, params).fetchall()
    finally:
        src.close()

    since_s = (since_ms / 1000.0) - _LIVE_TRANSCRIPT_SLACK_S if since_ms is not None else None
    lookup_index: dict[str, list[RecoveredCall]] | TranscriptIndex
    if index is None:
        lookup_index = build_index(authors, modified_since_s=since_s, use_cache=since_ms is not None)
    else:
        index.refresh(min_timestamp_ms=since_ms, force=True)
        lookup_index = index
    ingested = 0
    for row in rows:
        if store.has_recording(conn, row["id"]):
            continue
        if dry_run:
            ingested += 1
            continue
        target_ms = (row["timestamp"] or 0) // 1_000_000
        best = (
            lookup_index.match(row["command"] or "", target_ms)
            if isinstance(lookup_index, TranscriptIndex)
            else match_call(lookup_index, row["command"] or "", target_ms)
        )
        if best is None:
            continue
        store.upsert_recording(
            conn,
            atuin_id=row["id"],
            command=row["command"] or "",
            output=best.output,
            exit_code=row["exit"],
            total_bytes=len(best.output.encode("utf-8")),
            total_lines=_output_line_count(best.output),
            captured_at_ms=best.result_ts_ms or target_ms or int(time.time() * 1000),
            source="agent-home",
        )
        ingested += 1
    return ingested


def _iter_json(path: Path) -> Iterator[dict[str, Any]]:
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


def _join_text(parts: list[dict[str, Any]]) -> str:
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
