from __future__ import annotations

from pathlib import Path

import pytest

from atuout import settings


def test_daemon_socket_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATUOUT_DAEMON_SOCKET", "/tmp/custom.sock")
    assert settings.daemon_socket_path() == "/tmp/custom.sock"


def test_daemon_socket_from_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ATUOUT_DAEMON_SOCKET", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[daemon]\nenabled = true\nsocket_path = "/run/atuin/x.sock"\n')
    monkeypatch.setenv("ATUIN_CONFIG_DIR", str(tmp_path))
    assert settings.daemon_socket_path() == "/run/atuin/x.sock"
    assert settings.daemon_enabled() is True


def test_daemon_socket_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ATUOUT_DAEMON_SOCKET", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("ATUIN_CONFIG_DIR", str(tmp_path / "noconfig"))
    assert settings.daemon_socket_path() == str(tmp_path / "data" / "atuin" / "atuin.sock")


def test_daemon_enabled_defaults_true_without_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ATUIN_CONFIG_DIR", str(tmp_path / "noconfig"))
    assert settings.daemon_enabled() is True


def test_daemon_enabled_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[daemon]\nenabled = false\n")
    monkeypatch.setenv("ATUIN_CONFIG_DIR", str(tmp_path))
    assert settings.daemon_enabled() is False


def test_db_path_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATUOUT_DB_PATH", "/tmp/my.db")
    assert settings.db_path() == Path("/tmp/my.db")
