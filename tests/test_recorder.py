"""Tests for the recorder module (mocked — no real asciinema needed)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from atuout.recorder import record_command, list_recordings


class TestRecordCommand:
    @patch("atuout.recorder.subprocess.run")
    def test_returns_recording(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        rec = record_command("echo hi", data_dir=tmp_path)
        assert rec.command == "echo hi"
        assert rec.recorder_exit_code == 0
        assert rec.cast_path.parent == tmp_path

    @patch("atuout.recorder.subprocess.run")
    def test_atuin_id_in_filename(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        rec = record_command("ls", atuin_id="xyz789", data_dir=tmp_path)
        assert "xyz789" in rec.cast_path.name
        assert rec.atuin_id == "xyz789"

    @patch("atuout.recorder.subprocess.run")
    def test_failed_recording(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        rec = record_command("bad", data_dir=tmp_path)
        assert rec.recorder_exit_code == 1
        assert rec.success is False

    @patch("atuout.recorder.subprocess.run")
    def test_calls_asciinema(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        record_command("echo test", data_dir=tmp_path)
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert cmd_args[0] == "asciinema"
        assert "rec" in cmd_args
        assert "-c" in cmd_args
        assert "echo test" in cmd_args


class TestListRecordings:
    def test_empty_dir(self, tmp_path: Path) -> None:
        recs = list_recordings(data_dir=tmp_path)
        assert recs == []

    def test_finds_recordings(self, tmp_path: Path) -> None:
        header = {"version": 2, "width": 80, "height": 24, "command": "echo hello"}
        for name in ["100_abc.cast", "200.cast", "300_def.cast"]:
            (tmp_path / name).write_text(json.dumps(header) + "\n")
        recs = list_recordings(data_dir=tmp_path)
        assert len(recs) == 3
        # Newest first
        assert "300" in recs[0].cast_path.stem

    def test_extracts_atuin_id(self, tmp_path: Path) -> None:
        header = {"version": 2, "width": 80, "height": 24, "command": "ls"}
        (tmp_path / "100_myatuin.cast").write_text(json.dumps(header) + "\n")
        recs = list_recordings(data_dir=tmp_path)
        assert recs[0].atuin_id == "myatuin"
