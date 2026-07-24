# PLAN: End-to-end pty-proxy OSC-133 capture test

Status: PLAN ONLY (no source/test files changed). Author-verified against the
vendored atuin source in `reference/atuin-src/` and a live `devenv shell` probe.

## 1. Goal and what it proves

Drive a **real** `atuin pty-proxy` session under a Python pseudo-terminal, run an
actual shell command inside the wrapped zsh, and let atuin's genuine capture path
run end-to-end:

```
zsh (atuin init) --OSC133 C/D w/ history_id--> pty-proxy stdout stream
  -> CommandCaptureTracker (osc133 parser + vt100 render)
  -> semantic_command_capture_sink batch (<=25ms / 64 items)
  -> SemanticClient.record_commands over the daemon socket
  -> daemon SemanticComponent.record_capture (keyed by history_id)
```

then call atuout's own `harvest(history_id)` -> `Semantic.CommandOutput` -> SQLite,
and assert the stored `output` / `exit_code` / `command` match what the command
actually produced.

### What this test proves that the existing `RecordCommands` injection tests do NOT

The current live tests (`test_harvest_end_to_end`, `test_capture_roundtrip_via_record_commands`)
build a `CommandCapture` in Python and push it straight into `Semantic.RecordCommands`.
They exercise only the daemon store + atuout's read path. They bypass, and therefore
never verify:

- that the `atuin pty-proxy` binary launches and wraps a shell headlessly;
- atuin's OSC-133 streaming parser (`osc133.rs`) and the `CommandCaptureTracker`
  zone/append/finish state machine (`capture.rs`);
- the vt100 `render_plain_text` cleanup that turns raw terminal bytes into the
  stored `output` (ANSI/backspace/prompt stripping);
- atuin's zsh integration actually emitting `133;C` and
  `133;D;<exit>;history_id=<id>;session=<sess>` (`shell/atuin.zsh`);
- a **real** `history_id` minted by `atuin history start` flowing through the
  `D` marker into the capture;
- the batching sink (`command_mod.rs semantic_command_capture_sink`) and
  `SemanticClient::from_settings` socket discovery;
- atuout's retry/backoff window (`harvest`) absorbing the real batch-flush latency.

In short: it's the only test that verifies the **producer** half of the pipeline
and the true byte-for-byte reconstruction, not just the daemon<->atuout contract.

## 2. Key facts established from the reference source (verified)

- **OSC markers are emitted by `atuin init zsh` (`atuin.zsh`), not by
  `atuin pty-proxy init`.** `atuin.zsh` gates every marker on
  `[[ -n "$ATUIN_PTY_PROXY_ACTIVE" ]]`. `runtime.rs` sets `ATUIN_PTY_PROXY_ACTIVE=1`
  (plus `ATUIN_PTY_PROXY_SOCKET`/`ATUIN_HEX_SOCKET`) on the wrapped child's env.
  So the wrapped shell only needs `eval "$(atuin init zsh)"` in its rc — it does
  **not** need the `atuin pty-proxy init zsh` preamble (that preamble only
  re-`exec`s pty-proxy, which we are already inside).
- **`history_id` origin:** `atuin.zsh` `_atuin_preexec` runs
  `id=$(atuin history start -- "$1")`, exports `ATUIN_HISTORY_ID=$id`, then emits
  `133;C`. `_atuin_precmd` (fired when the shell returns to draw the next prompt)
  emits `133;D;$EXIT;history_id=$ATUIN_HISTORY_ID;session=$ATUIN_SESSION`, then
  runs `atuin history end`. `atuin history start` mints a local uuid-v7 id
  regardless of whether the daemon is enabled, so `ATUIN_HISTORY_ID` is always set.
- **The capture fires on the `D` marker itself.** `capture.rs handle_command_finished`:
  when the `D` marker carries `history_id`, it sets buffers and calls
  `finish_capture()` immediately -> `on_capture` -> sink. We do **not** need a
  second command to flush; simply returning to the prompt (which triggers `precmd`
  -> `D`) completes the capture.
- **CommandOutput match does not require history association.** `semantic.rs
  record_has_history_id` returns true when `capture.history_id == request.history_id`.
  So even without `DaemonEvent::HistoryEnded` association, `CommandOutput(history_id)`
  finds the record by the capture's own `history_id`. This means the test is robust
  even if daemon history routing is off — but we still enable the daemon so the sink
  has somewhere to send.
- **Output reconstruction:** `command_output` returns `output=""` and fills `lines`
  (line_number+content); atuout's `reply_output_text` joins `lines` with `\n`.
  `render_plain_text` -> `normalize_screen_contents` trims trailing blank lines and
  right-trims each line. Expect the stored output to equal the command's visible
  stdout with no trailing blank line (e.g. `echo hello-capture` -> `"hello-capture"`).
- **pty-proxy is a full-session wrapper.** `runtime.rs run()` calls
  `terminal::size()`, `enable_raw_mode()`, reads its **own** stdin, spawns
  `CommandBuilder::new_default_prog()` (portable_pty -> `$SHELL`, else `/bin/sh`)
  on an inner pty, and forwards bytes both ways. So we run `atuin pty-proxy` under
  our own pty and set `SHELL=<zsh>` so the inner program is zsh. Because all inner
  bytes are copied to pty-proxy's stdout, the `D` marker (including
  `history_id=...`) appears in the bytes we read from our pty master — we can
  parse the id directly from the stream.

### Live-environment probe results (from `devenv shell`)

- `zsh` present at `/etc/profiles/per-user/andrew/bin/zsh`.
- `atuin pty-proxy init zsh` exits 0 (pty-proxy feature compiled in).
- `python -c "import pexpect"` -> **NO_PEXPECT** (dependency must be added).
- CAVEAT to verify at implementation time: inside `devenv shell`, `which atuin`
  resolved to the **prebuilt** `/nix/store/nrqw09...-atuin`, not the source-built
  `atuinLatest`. The existing suite guards this with `_semantic_available()`; reuse
  that guard so the new test skips if the on-PATH atuin lacks the Semantic service.

## 3. Where the code lives

Add to a **new file** `tests/test_integration_pty_proxy.py` (not
`test_integration_daemon.py`). Rationale: the new test needs a heavier, opt-in
gate (pexpect + zsh + slow), its own fixtures, and should not slow the existing
daemon suite. Reuse the daemon-startup mechanics by importing/refactoring, but keep
the interactive machinery isolated.

Reuse from the existing suite:
- The `atuin_daemon` fixture pattern (foreground daemon, `start_new_session=True`,
  `XDG_DATA_HOME` HOME under tmp_path, socket at
  `<XDG_DATA_HOME>/atuin/atuin.sock`, killpg teardown). Refactor it into
  `tests/support/atuin_daemon.py` (or a `conftest.py` fixture) so both files share
  it. Minimal-risk alternative: copy the fixture body into the new file.
- `_semantic_available(sock)` as the skip probe.

## 4. New dev dependency

Add `pexpect>=4.9` to `[project.optional-dependencies].dev` in `pyproject.toml`.
It is pure-Python and installs via `uv sync --extra dev`; **no devenv.nix change
needed** (it is not a system/nix package). No new nix packages required — `zsh`
is already on PATH in the devenv shell.

(Stdlib `pty` + `select` is a fallback if we want zero new deps, but pexpect's
`expect`/`sendline`/timeout handling makes the interactive flow far less flaky;
recommend pexpect.)

## 5. Opt-in / marker

Mark the test `@pytest.mark.slow` **and** gate it behind an env opt-in so it never
runs in the default `uv run pytest`:

```python
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("atuin") is None, reason="atuin not on PATH"),
    pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not on PATH"),
    pytest.mark.skipif(
        os.environ.get("ATUOUT_PTY_E2E") != "1",
        reason="set ATUOUT_PTY_E2E=1 to run the interactive pty-proxy capture test",
    ),
]
```

Register the `slow` marker in `pyproject.toml` `[tool.pytest.ini_options].markers`
to avoid the unknown-marker warning. Run it explicitly:

```
devenv shell bash -- -c 'ATUOUT_PTY_E2E=1 uv run pytest \
  tests/test_integration_pty_proxy.py -q --no-cov -p no:cacheprovider -o addopts=""'
```

## 6. Fixtures

### 6.1 Shared daemon fixture (reused)

Same as `atuin_daemon` today. It must ensure `HOME`, `XDG_DATA_HOME`, and the
socket path are consistent so pty-proxy's `Settings::new()` resolves the same
socket the daemon listens on.

### 6.2 New `atuin_home` fixture — write config + zshrc

```python
@pytest.fixture
def atuin_home(atuin_daemon, tmp_path, monkeypatch):
    """Returns (home_path, socket). Writes atuin config.toml (daemon enabled) and a
    .zshrc that loads atuin's zsh integration. HOME/XDG_DATA_HOME already point here
    via the atuin_daemon fixture; reuse its env."""
    home = Path(os.environ["HOME"])            # set by atuin_daemon
    cfg_dir = home / ".config" / "atuin"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # daemon.enabled=true so history start/end route through the daemon and the
    # pty-proxy sink's Settings resolve daemon usage; socket_path defaults to
    # <XDG_DATA_HOME>/atuin/atuin.sock which the daemon already created.
    (cfg_dir / "config.toml").write_text(
        "[daemon]\nenabled = true\n"
        f'socket_path = "{atuin_daemon}"\n'
    )
    # Minimal interactive rc: load atuin's zsh hooks so OSC133 C/D are emitted.
    (home / ".zshrc").write_text('eval "$(atuin init zsh)"\n')
    return home, atuin_daemon
```

Notes:
- Setting `socket_path` explicitly removes any ambiguity between the daemon's
  socket and what pty-proxy's `Settings` resolves.
- Keep `.zshrc` minimal to avoid the user's global rc emitting stray output that
  vt100 rendering would fold into the capture. Also set `PROMPT` to a fixed,
  simple string in the rc (e.g. `PROMPT='READY> '`) so we get a deterministic
  `expect` target and a clean prompt zone.

### 6.3 New `pty_proxy_shell` fixture — spawn pty-proxy under a pty

```python
@pytest.fixture
def pty_proxy_shell(atuin_home):
    import pexpect
    home, sock = atuin_home
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["SHELL"] = shutil.which("zsh")     # portable_pty new_default_prog uses $SHELL
    env["ATUIN_LOG"] = "error"
    env.pop("ATUIN_SESSION", None)         # let atuin.zsh mint a fresh session
    env.pop("ATUIN_PTY_PROXY_ACTIVE", None)
    child = pexpect.spawn(
        "atuin", ["pty-proxy"],
        env=env, encoding="utf-8", timeout=20, dimensions=(24, 80),
        codec_errors="replace",
    )
    try:
        # Wait until the inner zsh has drawn its first prompt (atuin hooks live).
        child.expect_exact("READY> ", timeout=20)
        yield child
    finally:
        with contextlib.suppress(Exception):
            child.sendline("exit")
            child.expect(pexpect.EOF, timeout=10)
        with contextlib.suppress(Exception):
            child.close(force=True)   # SIGKILLs the pty-proxy proc-group if needed
```

Cleanup detail: pexpect's `close(force=True)` kills the child (pty-proxy), which in
turn ends the inner shell and closes the pty master/slave fds. As a belt-and-braces
guard mirroring the existing suite, capture `child.pid` and, in teardown,
`os.killpg(os.getpgid(child.pid), SIGTERM)` inside `contextlib.suppress`. The daemon
is torn down by the reused `atuin_daemon` fixture. Never use `pkill -f` (self-match
hazard).

## 7. The test body

```python
_HISTORY_ID_RE = re.compile(r"history_id=([0-9a-fA-F-]+)")

def test_pty_proxy_capture_harvest_end_to_end(pty_proxy_shell, atuin_home, tmp_path):
    from atuout import store
    from atuout.harvest import harvest

    home, sock = atuin_home
    if not _semantic_available(sock):
        pytest.skip("atuin build predates PR #3510 (no Semantic service)")

    child = pty_proxy_shell
    marker = "hello-capture-xyz"       # unique, unlikely in prompt/noise

    # Run a real command inside the wrapped zsh. atuin preexec runs
    # `atuin history start` (mints ATUIN_HISTORY_ID) and emits 133;C; on return to
    # prompt, precmd emits 133;D;0;history_id=...;session=..., which completes the
    # capture and pushes it to the daemon via the batching sink.
    child.sendline(f"echo {marker}")

    # The D marker (with history_id) is forwarded to pty-proxy stdout = our pty
    # master, so we can read the real id straight from the stream. Wait for the
    # NEXT prompt so we know precmd (and thus the D marker + capture) has fired.
    child.expect(_HISTORY_ID_RE, timeout=20)
    history_id = child.match.group(1)
    # Sanity: also confirm we saw the echoed output.
    assert marker in child.before + child.after

    # Poll the daemon via atuout's own harvest until the batched capture lands.
    # harvest() already retries; give it a generous window for the <=25ms batch
    # flush + gRPC. Poll-loop rather than a fixed sleep:
    db = tmp_path / "pty_e2e.db"
    rec = None
    deadline = time.time() + 10
    while time.time() < deadline:
        rec = harvest(
            history_id, command=f"echo {marker}", exit_code=0,
            db_path=db, socket_path=sock, attempts=1, delay_ms=0,
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
    assert got.command == f"echo {marker}"
    assert got.exit_code == 0
    assert got.output.strip() == marker
    assert got.source == "fast"
```

### 7.1 history_id discovery — recommendation

**Primary: parse `history_id=` from the pty stream** (shown above). It is
self-contained, needs no extra gRPC subscription, and is exactly the id the capture
was keyed with, eliminating any mismatch risk.

**Cross-check / fallback: `History.TailHistory`.** Optionally start a
`DaemonClient.tail_history()` thread *before* `sendline`, and record the `ENDED`
event's id. Use this only if stream-parsing proves flaky (e.g. the `D` marker gets
chunked oddly by pexpect). Note: matching CommandOutput does **not** need the
history event (it matches on capture.history_id), so TailHistory is purely a
belt-and-braces id source, not a functional requirement.

Reading the id from atuin's local history sqlite is a weaker option (async writes,
ordering) — not recommended.

### 7.2 Synchronization — recommendation

Two-stage, both deterministic (no blind sleeps):
1. Wait for the `history_id=` regex / next prompt via `child.expect` — proves the
   shell emitted `D` and the tracker completed the capture.
2. Poll `harvest()` / `CommandOutput` on a bounded 10s deadline with 100ms steps —
   absorbs the `<=25ms` sink batch window + gRPC. `harvest` is already
   idempotent/retryable, so re-calling is safe.

## 8. Skip conditions (summary)

- `atuin` not on PATH -> skip (module-level).
- `zsh` not on PATH -> skip (module-level).
- `ATUOUT_PTY_E2E != "1"` -> skip (opt-in gate).
- daemon didn't create its socket in time -> skip (reused fixture).
- `_semantic_available(sock)` false (UNIMPLEMENTED) -> skip (build predates #3510).
- `import pexpect` failing -> `pytest.importorskip("pexpect")` at top of module.

## 9. Risks / flakiness and mitigations

| Risk | Mitigation |
|------|------------|
| Interactive zsh timing / prompt not ready | Fixed `PROMPT='READY> '` + `expect_exact("READY> ")`; generous 20s timeouts. |
| Global zshrc / plugins injecting noise | Minimal test `.zshrc` under tmp HOME; do not source user rc. Set `ATUIN_LOG=error`. |
| `D` marker chunked across pty reads so regex misses | pexpect buffers across reads; regex `expect` scans the accumulated buffer. Fallback: TailHistory id source. |
| Prebuilt atuin on PATH lacks Semantic | `_semantic_available()` skip; document that the source-built `atuinLatest` must shadow the prebuilt in PATH. Verify at impl time. |
| vt100 rendering differs from naive expectation (wrapping, trailing blanks) | Assert on `marker in output` and `output.strip() == marker`, not exact multiline equality; keep command output single-line. |
| Sink socket mismatch (pty-proxy Settings vs daemon) | Pin `socket_path` in config.toml to the daemon's socket; share HOME/XDG_DATA_HOME. |
| Raw-mode / no controlling tty under CI | pexpect provides a real pty; still gate behind `ATUOUT_PTY_E2E` and `slow` so CI opts in deliberately. |
| Batch flush latency | 10s bounded poll loop, not a fixed sleep. |
| Leaked pty-proxy/zsh processes | `child.close(force=True)` + killpg fallback; daemon via reused fixture's killpg. |

Overall: moderately brittle (interactive PTY + real shell). Justified as an opt-in,
`slow`, single high-value test. Keep it to **one** command/assertion to minimize
surface. The existing injection tests remain the fast, always-on coverage.

## 10. Recommendation

- Implement as one opt-in `slow` test in a new
  `tests/test_integration_pty_proxy.py`.
- Add `pexpect>=4.9` to dev deps; register the `slow` marker.
- Use stream-parsed `history_id` as primary discovery, bounded poll for sync.
- Refactor the `atuin_daemon` fixture into a shared location so both integration
  files use it.
- Keep it excluded from the default `enterTest` run; document the explicit
  `ATUOUT_PTY_E2E=1` invocation in the test module docstring.

## 11. Items I could NOT fully confirm (flag for implementation)

- Whether `SemanticClient::from_settings` requires `daemon.enabled=true` or only a
  reachable `socket_path`. `from_settings` source was not in the vendored subset.
  Mitigation: set both `enabled=true` and explicit `socket_path`.
- Whether the on-PATH atuin inside `devenv shell` is the source-built (#3510) or the
  prebuilt binary — probe showed the prebuilt path winning. `_semantic_available`
  guard handles it, but PATH ordering should be verified/fixed so the test actually
  runs rather than silently skipping.
- Exact `portable_pty::new_default_prog` behavior for an argless zsh being
  interactive on a tty — expected yes, but confirm the atuin hooks (preexec/precmd)
  actually run in the spawned shell during a smoke run before committing assertions.
