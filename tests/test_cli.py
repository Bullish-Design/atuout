"""Tests for the CLI entry-point."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from atuout.cli import main


class TestCLIHelp:
    def test_no_args_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main([])
        assert ret == 0
        captured = capsys.readouterr()
        assert "atuout" in captured.out.lower()


class TestCLIRecord:
    @patch("atuout.recorder.subprocess.run")
    def test_record_subcommand(self, mock_run: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ret = main(["--data-dir", str(tmp_path), "record", "echo hi"])
        captured = capsys.readouterr()
        assert "Recording" in captured.out
        assert ret == 0

    @patch("atuout.recorder.subprocess.run")
    def test_record_with_atuin_id(
        self, mock_run: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ret = main(["--data-dir", str(tmp_path), "record", "--atuin-id", "abc", "echo hi"])
        assert ret == 0


class TestCLIList:
    def test_list_empty(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["--data-dir", str(tmp_path), "list"])
        assert ret == 0
        captured = capsys.readouterr()
        assert "No recordings" in captured.out

    def test_list_with_recordings(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        header = {"version": 2, "width": 80, "height": 24, "command": "echo hello"}
        (tmp_path / "100_abc.cast").write_text(json.dumps(header) + "\n")
        ret = main(["--data-dir", str(tmp_path), "list"])
        assert ret == 0
        captured = capsys.readouterr()
        assert "Recording" in captured.out


class TestCLIShow:
    def test_show_cast_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        header = {"version": 2, "width": 80, "height": 24, "env": {"exit_code": "0"}}
        events = [[0.1, "o", "hello world\r\n"]]
        p = tmp_path / "test.cast"
        lines = [json.dumps(header)] + [json.dumps(e) for e in events]
        p.write_text("\n".join(lines) + "\n")
        ret = main(["show", str(p)])
        assert ret == 0
        captured = capsys.readouterr()
        assert "hello world" in captured.out

    def test_show_missing_file(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["show", "/nonexistent/file.cast"])
        assert ret == 1
