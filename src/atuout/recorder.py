"""Thin wrapper around asciinema for recording individual commands."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from atuout.recording import Recording

# Default directory where recordings are stored
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "atuout" / "recordings"


def _ensure_data_dir(data_dir: Path | None = None) -> Path:
    d = data_dir or DEFAULT_DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def record_command(
    command: str,
    *,
    atuin_id: str | None = None,
    shell: str = "/bin/zsh",
    data_dir: Path | None = None,
    env: dict[str, str] | None = None,
) -> Recording:
    """Record a single command execution using ``asciinema rec``.

    Parameters
    ----------
    command:
        The shell command to record.
    atuin_id:
        Optional Atuin history ID to associate with this recording.
    shell:
        Shell to use for the recording (default ``/bin/zsh``).
    data_dir:
        Override the directory where ``.cast`` files are stored.
    env:
        Extra environment variables forwarded to the subprocess.

    Returns
    -------
    Recording
        A lightweight wrapper around the resulting ``.cast`` file.
    """
    dest = _ensure_data_dir(data_dir)

    timestamp = int(time.time() * 1000)
    stem = f"{timestamp}"
    if atuin_id:
        stem = f"{timestamp}_{atuin_id}"
    cast_path = dest / f"{stem}.cast"

    cmd = [
        "asciinema",
        "rec",
        "--overwrite",
        "-c",
        command,
        str(cast_path),
    ]

    merged_env: dict[str, str] | None = None
    if env:
        import os

        merged_env = {**os.environ, **env}

    result = subprocess.run(cmd, capture_output=True, text=True, env=merged_env)

    return Recording(
        cast_path=cast_path,
        command=command,
        atuin_id=atuin_id,
        recorder_exit_code=result.returncode,
    )


def list_recordings(data_dir: Path | None = None) -> list[Recording]:
    """Return all recordings found in *data_dir*, newest first."""
    dest = _ensure_data_dir(data_dir)
    recordings: list[Recording] = []
    for p in sorted(dest.glob("*.cast"), reverse=True):
        # Try to extract atuin_id from filename convention: <ts>_<atuin_id>.cast
        parts = p.stem.split("_", 1)
        atuin_id = parts[1] if len(parts) == 2 else None

        # Read the header to get the command
        command: str | None = None
        try:
            with p.open() as f:
                header = json.loads(f.readline())
                # asciinema stores the -c arg in the header command field
                cmd_list = header.get("env", {}).get("SHELL_COMMAND") or header.get("command")
                if isinstance(cmd_list, str):
                    command = cmd_list
        except (json.JSONDecodeError, OSError):
            pass

        recordings.append(
            Recording(
                cast_path=p,
                command=command or "<unknown>",
                atuin_id=atuin_id,
            )
        )
    return recordings
