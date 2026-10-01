"""Shared fixtures for environment isolation and temporary SQLite stores."""

from __future__ import annotations

from pathlib import Path

import pytest

_ATUOUT_ENV = ("ATUOUT_DB_PATH", "ATUOUT_DATA_DIR")


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in _ATUOUT_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ATUOUT_DB_PATH", str(tmp_path / "atuout.db"))


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    return tmp_path / "atuout.db"
