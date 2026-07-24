from __future__ import annotations

from atuout import store
from atuout._proto import semantic_pb2
from atuout.recording import Recording, reply_output_text


def test_reply_output_text_from_lines() -> None:
    # Real daemon shape: output field empty, content only in lines.
    reply = semantic_pb2.CommandOutputReply(
        found=True,
        output="",
        total_lines=2,
        lines=[
            semantic_pb2.OutputLine(line_number=1, content="first"),
            semantic_pb2.OutputLine(line_number=2, content="second"),
        ],
    )
    assert reply_output_text(reply) == "first\nsecond"
    rec = Recording.from_reply(reply, atuin_id="x")
    assert rec.output == "first\nsecond"
    assert rec.output_lines == ["first", "second"]


def test_reply_output_text_prefers_output_field() -> None:
    reply = semantic_pb2.CommandOutputReply(found=True, output="literal\n")
    assert reply_output_text(reply) == "literal\n"


def test_from_reply_populates_fields() -> None:
    reply = semantic_pb2.CommandOutputReply(
        found=True, output="a\nb\n", total_bytes=4, total_lines=2
    )
    rec = Recording.from_reply(reply, atuin_id="id1", command="ls", exit_code=0)
    assert rec.atuin_id == "id1"
    assert rec.command == "ls"
    assert rec.output == "a\nb\n"
    assert rec.output_lines == ["a", "b"]
    assert rec.total_bytes == 4
    assert rec.total_lines == 2
    assert rec.success is True


def test_from_reply_defaults_unknown_command() -> None:
    reply = semantic_pb2.CommandOutputReply(found=True, output="")
    rec = Recording.from_reply(reply, atuin_id="id1")
    assert rec.command == "<unknown>"


def test_success_semantics() -> None:
    reply = semantic_pb2.CommandOutputReply(found=True, output="")
    assert Recording.from_reply(reply, atuin_id="x", exit_code=1).success is False
    assert Recording.from_reply(reply, atuin_id="x", exit_code=None).success is False
    assert Recording.from_reply(reply, atuin_id="x", exit_code=0).success is True


def test_from_row(db_file) -> None:
    conn = store.connect(db_file)
    store.upsert_recording(
        conn,
        atuin_id="rid",
        command="pwd",
        output="/home\n",
        exit_code=2,
        total_bytes=6,
        total_lines=1,
        captured_at_ms=123,
        source="reconciler",
    )
    rec = store.get_recording(conn, "rid")
    assert rec is not None
    assert rec.command == "pwd"
    assert rec.exit_code == 2
    assert rec.source == "reconciler"
    assert rec.success is False


def test_str() -> None:
    reply = semantic_pb2.CommandOutputReply(found=True, output="")
    rec = Recording.from_reply(reply, atuin_id="id1", command="ls", exit_code=0)
    assert str(rec) == "Recording(ok atuin=id1 'ls')"


def test_duration_is_zero() -> None:
    reply = semantic_pb2.CommandOutputReply(found=True, output="")
    assert Recording.from_reply(reply, atuin_id="x").duration == 0.0
