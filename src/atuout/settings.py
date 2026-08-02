"""Resolve atuin daemon connection settings and atuout storage locations.

The daemon socket path mirrors atuin's ``settings.daemon.socket_path``. The exact default
lives in atuin's (un-vendored) ``atuin-client/src/settings.rs``; the fallback encoded here
(``<data_dir>/atuin/atuin.sock``) should be re-verified against a live atuin install — see the
implementation guide's "verify against live atuin" section.

When atuin runs with ``systemd_socket = true`` (the home-manager default on NixOS), the daemon
binds the socket *systemd* hands it (``$XDG_RUNTIME_DIR/atuin.sock``) rather than the
``socket_path`` in config.toml — so we resolve to the runtime socket in that case. On systems
where the runtime socket doesn't exist (e.g. non-systemd login), we fall back to the configured
path.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _xdg_state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


def _atuin_config_path() -> Path:
    config_dir = os.environ.get("ATUIN_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "config.toml"
    return Path.home() / ".config" / "atuin" / "config.toml"


def _load_atuin_daemon_config() -> dict[str, object]:
    """Return the ``[daemon]`` table from atuin's config.toml, or empty if unreadable."""
    path = _atuin_config_path()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return {}
    daemon = data.get("daemon")
    return daemon if isinstance(daemon, dict) else {}


def _runtime_socket() -> Path:
    """The socket path systemd's atuin socket unit uses: ``$XDG_RUNTIME_DIR/atuin.sock``."""
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if not rt:
        rt = f"/run/user/{os.getuid()}"
    return Path(rt) / "atuin.sock"


def daemon_socket_path() -> str:
    """Resolve the atuin daemon's Unix socket path.

    Resolution order:
      1. ``ATUOUT_DAEMON_SOCKET`` env override (always wins).
      2. ``[daemon].systemd_socket = true`` → the systemd runtime socket
         (``$XDG_RUNTIME_DIR/atuin.sock``), falling back to the configured path if it
         doesn't exist.
      3. ``[daemon].socket_path`` from atuin's config.toml.
      4. Default ``<XDG_DATA_HOME>/atuin/atuin.sock``.
    """
    override = os.environ.get("ATUOUT_DAEMON_SOCKET")
    if override:
        return override

    daemon = _load_atuin_daemon_config()
    if daemon.get("systemd_socket") is True:
        runtime = _runtime_socket()
        if runtime.exists():
            return str(runtime)
        # Non-systemd runtime (or socket not yet created); fall through to the
        # configured path so mixed setups keep working.

    configured = daemon.get("socket_path")
    if isinstance(configured, str) and configured:
        return str(Path(configured).expanduser())

    return str(_xdg_data_home() / "atuin" / "atuin.sock")


def daemon_enabled() -> bool:
    """Whether atuin's config enables the daemon. Defaults to True when unspecified."""
    daemon = _load_atuin_daemon_config()
    enabled = daemon.get("enabled")
    if isinstance(enabled, bool):
        return enabled
    return True


def db_path() -> Path:
    """Path to atuout's SQLite database."""
    override = os.environ.get("ATUOUT_DB_PATH")
    if override:
        return Path(override)
    data_dir = os.environ.get("ATUOUT_DATA_DIR")
    base = Path(data_dir) if data_dir else _xdg_data_home() / "atuout"
    return base / "atuout.db"


def state_dir() -> Path:
    """Directory for atuout logs and the reconciler pidfile/lock."""
    override = os.environ.get("ATUOUT_STATE_DIR")
    if override:
        return Path(override)
    return _xdg_state_home() / "atuout"


def runtime_dir() -> Path:
    """Directory for the reconciler pidfile/lock (prefers XDG_RUNTIME_DIR).

    XDG_RUNTIME_DIR is missing in shells spawned outside a systemd login (e.g. by editors,
    agents, or cron). Fall back to the standard per-user runtime dir when it exists so the
    pidfile/lock stay consistent with the daemonized reconciler; only then fall back to the
    state dir."""
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt:
        return Path(rt) / "atuout"
    runtime = Path(f"/run/user/{os.getuid()}")
    if runtime.is_dir():
        return runtime / "atuout"
    return state_dir()
