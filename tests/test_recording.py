"""Tests for the Recording wrapper."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from atuout.recording import Recording


@pytest.fixture()
def cast_file(tmp_path: Path) -> Path:
    """Create a minimal asciicast v2 file."""
    header = {
        "version": 2,
        "width": 80,
        "height": 24,
        "timestamp": 1700000000,
        "duration": 1.5,
        "env": {"SHELL": "/bin/zsh", "exit_code": "0"},
        "command": "echo hello",
    }
    events = [
        [0.1, "o", "hello\r\n"],
        [0.2, "o", "$ "],
    ]
    p = tmp_path / "test.cast"
    lines = [json.dumps(header)] + [json.dumps(e) for e in events]
    p.write_text("\n".join(lines) + "\n")
    return p


@pytest.fixture()
def failed_cast_file(tmp_path: Path) -> Path:
    header = {
        "version": 2,
        "width": 80,
        "height": 24,
        "timestamp": 1700000000,
        "duration": 0.5,
        "env": {"SHELL": "/bin/zsh", "exit_code": "1"},
        "command": "false",
    }
    events = [
        [0.1, "o", ""],
    ]
    p = tmp_path / "fail.cast"
    lines = [json.dumps(header)] + [json.dumps(e) for e in events]
    p.write_text("\n".join(lines) + "\n")
    return p


class TestRecordingParsing:
    def test_header(self, cast_file: Path) -> None:
        rec = Recording(cast_path=cast_file, command="echo hello")
        assert rec.header["version"] == 2
        assert rec.header["width"] == 80

    def test_events(self, cast_file: Path) -> None:
        rec = Recording(cast_path=cast_file, command="echo hello")
        assert len(rec.events) == 2
        assert rec.events[0] == (0.1, "o", "hello\r\n")

    def test_output(self, cast_file: Path) -> None:
        rec = Recording(cast_path=cast_file, command="echo hello")
        assert "hello" in rec.output

    def test_output_lines(self, cast_file: Path) -> None:
        rec = Recording(cast_path=cast_file, command="echo hello")
        lines = rec.output_lines
        assert any("hello" in l for l in lines)

    def test_duration(self, cast_file: Path) -> None:
        rec = Recording(cast_path=cast_file, command="echo hello")
        assert rec.duration == 1.5


class TestRecordingSuccess:
    def test_success_from_header(self, cast_file: Path) -> None:
        rec = Recording(cast_path=cast_file, command="echo hello")
        assert rec.exit_code == 0
        assert rec.success is True

    def test_failure_from_header(self, failed_cast_file: Path) -> None:
        rec = Recording(cast_path=failed_cast_file, command="false")
        assert rec.exit_code == 1
        assert rec.success is False

    def test_success_fallback_to_recorder(self, tmp_path: Path) -> None:
        # Cast with no exit_code in header
        header = {"version": 2, "width": 80, "height": 24}
        p = tmp_path / "nocode.cast"
        p.write_text(json.dumps(header) + "\n")
        rec = Recording(cast_path=p, command="ls", recorder_exit_code=0)
        assert rec.exit_code is None
        assert rec.success is True

    def test_failure_fallback_to_recorder(self, tmp_path: Path) -> None:
        header = {"version": 2, "width": 80, "height": 24}
        p = tmp_path / "nocode.cast"
        p.write_text(json.dumps(header) + "\n")
        rec = Recording(cast_path=p, command="bad", recorder_exit_code=1)
        assert rec.success is False


class TestRecordingAtuin:
    def test_atuin_id_stored(self, cast_file: Path) -> None:
        rec = Recording(cast_path=cast_file, command="echo hello", atuin_id="abc123")
        assert rec.atuin_id == "abc123"

    def test_atuin_id_none(self, cast_file: Path) -> None:
        rec = Recording(cast_path=cast_file, command="echo hello")
        assert rec.atuin_id is None


class TestRecordingStr:
    def test_str_success(self, cast_file: Path) -> None:
        rec = Recording(cast_path=cast_file, command="echo hello", atuin_id="abc")
        s = str(rec)
        assert "ok" in s
        assert "abc" in s
        assert "echo hello" in s

    def test_str_failure(self, failed_cast_file: Path) -> None:
        rec = Recording(cast_path=failed_cast_file, command="false")
        s = str(rec)
        assert "fail" in s


class TestRecordingMissingFile:
    def test_missing_cast_file(self, tmp_path: Path) -> None:
        rec = Recording(cast_path=tmp_path / "missing.cast", command="ls")
        assert rec.output == ""
        assert rec.events == []
        assert rec.exit_code is None

    def test_empty_cast_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.cast"
        p.write_text("")
        rec = Recording(cast_path=p, command="ls")
        assert rec.output == ""
        assert rec.events == []
