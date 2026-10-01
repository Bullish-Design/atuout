from __future__ import annotations

from atuout import store
from atuout.recording import Recording


def test_recording_properties_from_store_row(db_file) -> None:
    conn = store.connect(db_file)
    store.upsert_recording(
        conn,
        atuin_id="record-1",
        command="echo hello",
        output="hello\nworld\n",
        exit_code=0,
        total_bytes=12,
        total_lines=2,
        captured_at_ms=123,
        source="agent-home",
    )

    recording = store.get_recording(conn, "record-1")
    assert recording is not None
    assert recording.command == "echo hello"
    assert recording.output == "hello\nworld\n"
    assert recording.output_lines == ["hello", "world"]
    assert recording.total_bytes == 12
    assert recording.total_lines == 2
    assert recording.captured_at_ms == 123
    assert recording.exit_code == 0
    assert recording.success
    assert recording.source == "agent-home"


def test_recording_unknown_command_from_store_row(db_file) -> None:
    conn = store.connect(db_file)
    store.upsert_recording(
        conn,
        atuin_id="record-2",
        command=None,
        output="",
        exit_code=None,
        total_bytes=0,
        total_lines=0,
        captured_at_ms=1,
    )
    recording = store.get_recording(conn, "record-2")
    assert recording is not None
    assert recording.command == "<unknown>"
    assert not recording.success


def test_recording_string_format() -> None:
    recording = Recording(command="ls", atuin_id="record-3", exit_code=0)
    assert str(recording) == "Recording(ok atuin=record-3 'ls')"
