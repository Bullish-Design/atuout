"""Resolve atuout's agent-recording storage location."""

from __future__ import annotations

import os
from pathlib import Path


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def db_path() -> Path:
    """Path to atuout's SQLite database."""
    override = os.environ.get("ATUOUT_DB_PATH")
    if override:
        return Path(override)
    data_dir = os.environ.get("ATUOUT_DATA_DIR")
    base = Path(data_dir) if data_dir else _xdg_data_home() / "atuout"
    return base / "atuout.db"

