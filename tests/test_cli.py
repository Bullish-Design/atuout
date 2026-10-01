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
        source="agent-home",
    )
    conn.close()


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "atuout" in capsys.readouterr().out.lower()


def test_list_and_show_agent_recording(db_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(db_file, "abc")
    assert main(["--db", str(db_file), "list"]) == 0
    listing = capsys.readouterr().out
    assert "Recording" in listing
    assert "atuin=abc" in listing

    assert main(["--db", str(db_file), "show", "abc"]) == 0
    assert "hello world" in capsys.readouterr().out


def test_list_empty_store(db_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", str(db_file), "list"]) == 0
    assert "No agent recordings" in capsys.readouterr().out


def test_show_missing_recording_returns_one(db_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", str(db_file), "show", "missing"]) == 1
    assert "No agent recording" in capsys.readouterr().err


def test_status_reports_agent_store(db_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(db_file, "abc")
    assert main(["--db", str(db_file), "status"]) == 0
    output = capsys.readouterr().out
    assert f"agent store: {db_file}" in output
    assert "agent recordings: 1" in output


def test_ingest_agent_calls_backfill(db_file: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    from atuout import agent_ingest

    calls = []

    def backfill(conn, **kwargs):
        calls.append(kwargs)
        return 2

    monkeypatch.setattr(agent_ingest, "backfill", backfill)
    assert main(["--db", str(db_file), "ingest-agent", "--agent", "codex", "--since-hours", "6"]) == 0
    assert calls[0]["authors"] == ("codex",)
    assert calls[0]["since_ms"] is not None
    assert "ingested 2 agent commands" in capsys.readouterr().out


@pytest.mark.parametrize("subcommand", ["harvest", "reconcile", "init-zsh", "check"])
def test_dead_harvest_commands_are_removed(subcommand: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main([subcommand])
    assert exc.value.code != 0
