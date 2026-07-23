"""Shared fixtures: env isolation, temp DB, and the fake atuin daemon."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support.fake_daemon import FakeDaemon

_ATUOUT_ENV = (
    "ATUOUT_DAEMON_SOCKET",
    "ATUOUT_DB_PATH",
    "ATUOUT_DATA_DIR",
    "ATUOUT_STATE_DIR",
    "ATUOUT_HARVEST_ATTEMPTS",
    "ATUOUT_HARVEST_DELAY_MS",
    "ATUIN_CONFIG_DIR",
    "XDG_RUNTIME_DIR",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in _ATUOUT_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ATUOUT_DB_PATH", str(tmp_path / "atuout.db"))
    monkeypatch.setenv("ATUOUT_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    return tmp_path / "atuout.db"


@pytest.fixture
def fake_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDaemon]:
    socket_path = str(tmp_path / "atuin.sock")
    monkeypatch.setenv("ATUOUT_DAEMON_SOCKET", socket_path)
    with FakeDaemon(socket_path) as daemon:
        yield daemon


@pytest.fixture
def fake_daemon_unimplemented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FakeDaemon]:
    socket_path = str(tmp_path / "atuin.sock")
    monkeypatch.setenv("ATUOUT_DAEMON_SOCKET", socket_path)
    with FakeDaemon(socket_path, unimplemented=True) as daemon:
        yield daemon
