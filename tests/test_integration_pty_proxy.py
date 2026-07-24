"""End-to-end OSC-133 capture test driving a real ``atuin pty-proxy`` session.

This is the only test that exercises the *producer* half of the pipeline: a genuine
``atuin pty-proxy`` binary wraps an interactive zsh under a pseudo-terminal, atuin's zsh
integration emits OSC-133 ``C``/``D`` markers, atuin's streaming parser + vt100 render
reconstruct the command output, the batching sink ships it to the daemon over the socket,
and atuout's own ``harvest()`` reads it back into SQLite. The existing
``RecordCommands``-injection tests bypass all of that.

Opt-in and slow: it spawns a real shell under a pty. Run explicitly, inside ``devenv shell``
so the source-built ``atuin`` (with the Semantic service, PR #3510) and ``zsh`` are on PATH::

    devenv shell bash -- -c 'ATUOUT_PTY_E2E=1 uv run pytest \
      tests/test_integration_pty_proxy.py -q --no-cov -p no:cacheprovider -o addopts=""'
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support.atuin_daemon import semantic_available

pexpect = pytest.importorskip("pexpect")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("atuin") is None, reason="atuin not on PATH"),
    pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not on PATH"),
    pytest.mark.skipif(
        os.environ.get("ATUOUT_PTY_E2E") != "1",
        reason="set ATUOUT_PTY_E2E=1 to run the interactive pty-proxy capture test",
    ),
]

_PROMPT = "READY> "
_HISTORY_ID_RE = re.compile(r"history_id=([0-9a-fA-F-]+)")


@pytest.fixture
def atuin_home(atuin_daemon: str) -> tuple[Path, str]:
    """Write atuin ``config.toml`` (daemon enabled, socket pinned) and a minimal ``.zshrc``.

    HOME/XDG_DATA_HOME already point under the temp dir via ``atuin_daemon``; we reuse that so
    pty-proxy's ``Settings::new()`` resolves the same socket the daemon listens on.
    """
    home = Path(os.environ["HOME"])
    cfg_dir = home / ".config" / "atuin"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # daemon.enabled=true so history start/end route through the daemon and SemanticClient
    # resolves daemon usage; socket_path pinned to the daemon's socket to remove any ambiguity.
    (cfg_dir / "config.toml").write_text(f'[daemon]\nenabled = true\nsocket_path = "{atuin_daemon}"\n')
    # Minimal rc: load atuin's zsh hooks (emits OSC133 C/D under pty-proxy) and pin a fixed
    # prompt so `expect` has a deterministic target and vt100 render sees a clean prompt zone.
    (home / ".zshrc").write_text(f"PROMPT='{_PROMPT}'\nPS1='{_PROMPT}'\neval \"$(atuin init zsh)\"\n")
    return home, atuin_daemon


@pytest.fixture
def pty_proxy_shell(atuin_home: tuple[Path, str]) -> Iterator[object]:
    home, _sock = atuin_home
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["SHELL"] = shutil.which("zsh")  # portable_pty new_default_prog uses $SHELL
    env["ATUIN_LOG"] = "error"
    env.pop("ATUIN_SESSION", None)  # let atuin.zsh mint a fresh session
    env.pop("ATUIN_PTY_PROXY_ACTIVE", None)
    child = pexpect.spawn(
        "atuin",
        ["pty-proxy"],
        env=env,
        encoding="utf-8",
        timeout=20,
        dimensions=(24, 80),
        codec_errors="replace",
    )
    try:
        # Wait until the inner zsh has drawn its first prompt (atuin hooks are live).
        child.expect_exact(_PROMPT, timeout=20)
        yield child
    finally:
        with contextlib.suppress(Exception):
            child.sendline("exit")
            child.expect(pexpect.EOF, timeout=10)
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(child.pid), signal.SIGTERM)
        with contextlib.suppress(Exception):
            child.close(force=True)


def test_pty_proxy_capture_harvest_end_to_end(
    pty_proxy_shell: object, atuin_home: tuple[Path, str], tmp_path: Path
) -> None:
    from atuout import store
    from atuout.harvest import harvest

    _home, sock = atuin_home
    if not semantic_available(sock):
        pytest.skip("atuin build predates PR #3510 (no Semantic service)")

    child = pty_proxy_shell
    marker = "hello-capture-xyz"  # unique, unlikely in prompt/noise
    command = f"echo {marker}"

    # atuin preexec runs `atuin history start` (mints ATUIN_HISTORY_ID) and emits 133;C; on
    # return to the prompt, precmd emits 133;D;0;history_id=...;session=..., which completes the
    # capture and pushes it to the daemon via the batching sink.
    child.sendline(command)

    # The D marker (with history_id) is forwarded to pty-proxy stdout = our pty master, so we
    # read the real id straight from the stream. Waiting for it proves precmd (and thus the D
    # marker + capture) has fired.
    child.expect(_HISTORY_ID_RE, timeout=20)
    history_id = child.match.group(1)
    assert marker in (child.before or "") + (child.after or "")

    # Poll atuout's own harvest until the batched capture lands (<=25ms sink flush + gRPC).
    db = tmp_path / "pty_e2e.db"
    rec = None
    deadline = time.time() + 10
    while time.time() < deadline:
        rec = harvest(
            history_id,
            command=command,
            exit_code=0,
            db_path=db,
            socket_path=sock,
            attempts=1,
            delay_ms=0,
        )
        if rec is not None:
            break
        time.sleep(0.1)

    assert rec is not None, "capture never reached the daemon"
    assert marker in rec.output
    # Reconstruction should be exactly the visible stdout, no trailing blank line.
    assert rec.output.strip() == marker

    # Read back through a fresh store connection (persistence check).
    conn = store.connect(db)
    got = store.get_recording(conn, history_id)
    assert got is not None
    assert got.command == command
    assert got.exit_code == 0
    assert got.output.strip() == marker
    assert got.source == "fast"
