"""SQLite-backed durable store for harvested command captures."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from atuout.recording import Recording
from atuout.settings import db_path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recordings (
  atuin_id    TEXT PRIMARY KEY,
  command     TEXT,
  prompt      TEXT,
  output      TEXT NOT NULL,
  exit_code   INTEGER,
  total_bytes INTEGER,
  total_lines INTEGER,
  captured_at INTEGER NOT NULL,
  source      TEXT NOT NULL DEFAULT 'fast'
);

CREATE INDEX IF NOT EXISTS idx_recordings_captured_at ON recordings(captured_at);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the atuout DB, applying PRAGMAs and migrations."""
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def upsert_recording(
    conn: sqlite3.Connection,
    *,
    atuin_id: str,
    command: str | None,
    output: str,
    exit_code: int | None,
    total_bytes: int | None,
    total_lines: int | None,
    captured_at_ms: int,
    source: str = "fast",
) -> bool:
    """Insert a recording. Returns True if inserted, False if one already existed."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO recordings
          (atuin_id, command, prompt, output, exit_code, total_bytes, total_lines,
           captured_at, source)
        VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            atuin_id,
            command,
            output,
            exit_code,
            total_bytes,
            total_lines,
            captured_at_ms,
            source,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def has_recording(conn: sqlite3.Connection, atuin_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM recordings WHERE atuin_id = ? LIMIT 1", (atuin_id,)
    ).fetchone()
    return row is not None


def get_recording(conn: sqlite3.Connection, atuin_id: str) -> Recording | None:
    row = conn.execute(
        "SELECT * FROM recordings WHERE atuin_id = ?", (atuin_id,)
    ).fetchone()
    return Recording.from_row(row) if row is not None else None


def list_recordings(
    conn: sqlite3.Connection, *, limit: int | None = None
) -> list[Recording]:
    sql = "SELECT * FROM recordings ORDER BY captured_at DESC"
    params: tuple[object, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return [Recording.from_row(row) for row in conn.execute(sql, params).fetchall()]


def count_recordings(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM recordings").fetchone()
    return int(row["n"])
