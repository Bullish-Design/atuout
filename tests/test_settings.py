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


def test_daemon_socket_systemd_socket_uses_runtime_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """systemd_socket = true → the daemon binds the systemd runtime socket, not config's path."""
    monkeypatch.delenv("ATUOUT_DAEMON_SOCKET", raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "atuin.sock").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[daemon]\nenabled = true\nsystemd_socket = true\n'
        'socket_path = "/home/test/.local/share/atuin/atuin.sock"\n'
    )
    monkeypatch.setenv("ATUIN_CONFIG_DIR", str(tmp_path))
    assert settings.daemon_socket_path() == str(runtime / "atuin.sock")


def test_daemon_socket_systemd_socket_falls_back_when_runtime_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """systemd_socket = true but no runtime socket yet → use the configured path."""
    monkeypatch.delenv("ATUOUT_DAEMON_SOCKET", raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[daemon]\nenabled = true\nsystemd_socket = true\n'
        'socket_path = "/run/atuin/x.sock"\n'
    )
    monkeypatch.setenv("ATUIN_CONFIG_DIR", str(tmp_path))
    assert settings.daemon_socket_path() == "/run/atuin/x.sock"


def test_daemon_socket_without_systemd_socket_uses_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ATUOUT_DAEMON_SOCKET", raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "atuin.sock").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    cfg = tmp_path / "config.toml"
    cfg.write_text('[daemon]\nenabled = true\nsocket_path = "/run/atuin/y.sock"\n')
    monkeypatch.setenv("ATUIN_CONFIG_DIR", str(tmp_path))
    # systemd_socket unset/absent → configured path wins even if the runtime socket exists.
    assert settings.daemon_socket_path() == "/run/atuin/y.sock"


def test_runtime_dir_prefers_xdg_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    assert settings.runtime_dir() == Path("/run/user/1234/atuout")


def test_runtime_dir_falls_back_to_state_dir_when_no_per_user_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No XDG_RUNTIME_DIR and no /run/user/<uid> → state dir (old behavior)."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    # conftest's autouse fixture pins ATUOUT_STATE_DIR; clear it so XDG_STATE_HOME drives.
    monkeypatch.delenv("ATUOUT_STATE_DIR", raising=False)
    monkeypatch.setattr(settings.os, "getuid", lambda: 999999)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert settings.runtime_dir() == tmp_path / "state" / "atuout"
