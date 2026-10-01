from __future__ import annotations

from pathlib import Path

import pytest

from atuout import settings


def test_db_path_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATUOUT_DB_PATH", "/tmp/agent-recordings.db")
    assert settings.db_path() == Path("/tmp/agent-recordings.db")


def test_db_path_uses_xdg_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ATUOUT_DB_PATH", raising=False)
    monkeypatch.delenv("ATUOUT_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert settings.db_path() == tmp_path / "data" / "atuout" / "atuout.db"


def test_db_path_uses_data_dir_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ATUOUT_DB_PATH", raising=False)
    monkeypatch.setenv("ATUOUT_DATA_DIR", str(tmp_path / "atuout"))
    assert settings.db_path() == tmp_path / "atuout" / "atuout.db"
