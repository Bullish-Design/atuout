"""Tests for the CLI entry-point."""

from __future__ import annotations

from pathlib import Path

import pytest

from atuout import store
from atuout.cli import main


def _seed(db: Path, atuin_id: str, output: str = "hello world\n") -> None:
    conn = store.connect(db)
    store.upsert_recording(
        conn,
        atuin_id=atuin_id,
        command="echo hi",
        output=output,
        exit_code=0,
        total_bytes=len(output),
        total_lines=len(output.splitlines()),
        captured_at_ms=1000,
    )


class TestCLIHelp:
    def test_no_args_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main([])
        assert ret == 0
        assert "atuout" in capsys.readouterr().out.lower()


class TestCLIList:
    def test_list_empty(self, db_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["--db", str(db_file), "list"])
        assert ret == 0
        assert "No recordings" in capsys.readouterr().out

    def test_list_with_recordings(
        self, db_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_file, "abc")
        ret = main(["--db", str(db_file), "list"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Recording" in out
        assert "atuin=abc" in out


class TestCLIShow:
    def test_show_by_id(self, db_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_file, "abc")
        ret = main(["--db", str(db_file), "show", "abc"])
        assert ret == 0
        assert "hello world" in capsys.readouterr().out

    def test_show_missing(self, db_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["--db", str(db_file), "show", "nope"])
        assert ret == 1


class TestCLIInitZsh:
    def test_init_zsh_prints_hook(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["init-zsh"])
        assert ret == 0
        assert "add-zsh-hook" in capsys.readouterr().out


class TestCLIRecordRemoved:
    def test_record_subcommand_gone(self) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["record", "echo hi"])
        assert exc.value.code != 0
