from __future__ import annotations

from pathlib import Path

from atuout import store


def _seed(conn, atuin_id: str, output: str = "out", captured_at_ms: int = 1000) -> bool:
    return store.upsert_recording(
        conn,
        atuin_id=atuin_id,
        command="echo hi",
        output=output,
        exit_code=0,
        total_bytes=len(output),
        total_lines=len(output.splitlines()),
        captured_at_ms=captured_at_ms,
    )


def test_connect_creates_schema(db_file: Path) -> None:
    conn = store.connect(db_file)
    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key='version'"
    ).fetchone()["value"]
    assert version == str(store.SCHEMA_VERSION)
    assert db_file.exists()


def test_upsert_and_get_roundtrip(db_file: Path) -> None:
    conn = store.connect(db_file)
    assert _seed(conn, "abc", "hello\nworld") is True
    rec = store.get_recording(conn, "abc")
    assert rec is not None
    assert rec.atuin_id == "abc"
    assert rec.command == "echo hi"
    assert rec.output == "hello\nworld"
    assert rec.success is True


def test_insert_or_ignore_idempotent(db_file: Path) -> None:
    conn = store.connect(db_file)
    assert _seed(conn, "abc", "first") is True
    assert _seed(conn, "abc", "second") is False  # already present
    assert store.get_recording(conn, "abc").output == "first"


def test_has_recording(db_file: Path) -> None:
    conn = store.connect(db_file)
    assert store.has_recording(conn, "abc") is False
    _seed(conn, "abc")
    assert store.has_recording(conn, "abc") is True


def test_list_newest_first(db_file: Path) -> None:
    conn = store.connect(db_file)
    _seed(conn, "old", captured_at_ms=1000)
    _seed(conn, "new", captured_at_ms=2000)
    recs = store.list_recordings(conn)
    assert [r.atuin_id for r in recs] == ["new", "old"]
    assert store.count_recordings(conn) == 2
    assert [r.atuin_id for r in store.list_recordings(conn, limit=1)] == ["new"]
