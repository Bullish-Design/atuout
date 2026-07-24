"""Shared helpers for live integration tests that need a real ``atuin daemon``.

The :func:`atuin_daemon` fixture is re-exported from ``conftest.py`` so any test module can
request it. ``semantic_available`` is the skip probe: the Semantic capture service (atuin
PR #3510) is not in released atuin yet, so tests that need it skip when the on-PATH build
predates it.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from atuout.daemon_client import DaemonClient, DaemonError


def spawn_atuin_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Run a real ``atuin daemon`` under a temp HOME and yield its socket path.

    HOME and XDG_DATA_HOME are pinned under ``tmp_path`` so the daemon (and any pty-proxy
    session that resolves ``Settings::new()``) agree on the socket location. The daemon runs in
    the foreground in its own session so teardown can kill the whole process group.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ATUOUT_DAEMON_SOCKET", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    sock = home / ".local" / "share" / "atuin" / "atuin.sock"

    proc = subprocess.Popen(
        ["atuin", "daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not sock.exists():
            time.sleep(0.1)
        if not sock.exists():
            pytest.skip("atuin daemon did not create its socket")
        yield str(sock)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


def semantic_available(sock: str) -> bool:
    """True when the daemon exposes the Semantic service (atuin built from PR #3510)."""
    with DaemonClient(sock) as client:
        try:
            client.command_output("probe")
            return True
        except DaemonError as e:
            return e.kind != "unimplemented"
