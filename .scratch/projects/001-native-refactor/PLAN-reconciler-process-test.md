# PLAN — Reconciler real-process integration test

Status: PLANNING ONLY. No source or test files modified by this document.
Verified against source on 2026-07-23 (reconciler.py, settings.py, cli.py, daemon_client.py,
tests/test_integration_daemon.py, tests/test_reconciler.py, tests/conftest.py,
reference proto `crates_atuin-daemon_proto_history.proto`, `crates_atuin-daemon_src_client.rs`).

---

## 1. Goal and what these tests prove beyond existing coverage

The existing unit tests (`tests/test_reconciler.py`) and the in-process integration tests
(`tests/test_integration_daemon.py::test_reconciler_backfill_end_to_end`) all call
`reconciler.reconcile_ended(...)` **in the test process**. They prove the *function* works.
They do NOT prove:

- that a spawned, detached `atuout reconcile --daemonize` process actually boots, acquires the
  flock, writes the pidfile, opens a `TailHistory` stream, and reacts to a live `ENDED` event;
- that the process backfills into SQLite with **no in-process call** to reconciler logic;
- that the single-instance guard blocks a second real spawn;
- that `stop` (SIGTERM) cleanly removes the pidfile / releases the lock;
- crash-restart via `ensure`.

New file: **`tests/test_reconciler_process.py`** (do not extend `test_integration_daemon.py`;
these need a distinct fixture set and are opt-in/slow). Reuse patterns from the existing
`atuin_daemon` fixture and `_inject_capture` / `_semantic_available` helpers (copy them into the
new module or import them — see §8).

---

## 2. Key facts confirmed from source / reference

- **`runtime_dir()`** (settings.py:90) prefers `XDG_RUNTIME_DIR` → returns `$XDG_RUNTIME_DIR/atuout`.
  Pidfile = `runtime_dir()/atuout-reconciler.pid`, lockfile = `runtime_dir()/atuout-reconciler.lock`
  (reconciler.py:35-40). So controlling `XDG_RUNTIME_DIR` fully isolates lock/pid per test.
- **Lock** = `fcntl.flock(LOCK_EX|LOCK_NB)` on the lockfile held open for process lifetime
  (reconciler.py:48-58). `is_running()` probes by trying to acquire (reconciler.py:61-68).
  flock is per-open-file-description and auto-released on process death — this is what makes the
  kill -9 crash-restart case work.
- **Pidfile format** = two lines: `pid\n<epoch_ms>\n` (reconciler.py:72). `read_pid()` reads line 0.
- **`run()`** (reconciler.py:168): acquire lock → if None return 0 (silent no-op for a duplicate)
  → install SIGTERM/SIGINT handlers that set `stop_flag["stop"]=True` → write pidfile →
  `_run_loop` → finally remove pidfile + unlock. Clean shutdown path is fully deterministic.
- **`_run_loop`** (reconciler.py:142): opens `DaemonClient(daemon_socket_path())`, iterates
  `tail_history()`; on `HISTORY_EVENT_KIND_ENDED` calls `reconcile_ended`. On `DaemonError`/any
  exception it logs and sleeps `backoff` (1s → 30s doubling). **`TailHistory` only streams NEW
  events after the stream opens** — the test must open the reconciler's tail BEFORE ending history.
- **`ensure()`** (reconciler.py:198) spawns `subprocess.Popen(["atuout","reconcile","--daemonize"],
  start_new_session=True)` with stdio to DEVNULL. Hardcoded `"atuout"` on PATH → testability
  concern (§7).
- **`stop()`** (reconciler.py:214) reads pid, `os.kill(pid, SIGTERM)`; if `ProcessLookupError`
  removes pidfile and returns False.
- **RPCs available** (history_pb2_grpc): `StartHistory(StartHistoryRequest)` → `StartHistoryReply{id}`,
  `EndHistory(EndHistoryRequest{id,exit,duration})` → `EndHistoryReply{id,idx,...}`,
  `TailHistory` streaming. Capture injection via `Semantic.RecordCommands` (existing `_inject_capture`).
- **CommandOutput matches a capture by `history_id`** (semantic.rs `record_has_history_id`, noted in
  existing test docstring). Capture requires non-empty `session_id` (daemon rejects otherwise).

### 2a. HARD QUESTION 1 — making the daemon emit a live `ENDED` event  (partially UNCONFIRMED)

Mechanism to plan:
1. `reply = HistoryStub.StartHistory(StartHistoryRequest(command=..., cwd=..., session=<non-empty>,
   hostname=..., timestamp=<ns>))` → `hid = reply.id` (daemon generates the id).
2. Inject a capture keyed to that same id:
   `_inject_capture(sock, hid, "out\n", command="pwd", exit_code=0)` (sets `session_id`).
3. `HistoryStub.EndHistory(EndHistoryRequest(id=hid, exit=0, duration=1000))`.

Expectation: `EndHistory` causes the daemon to broadcast a `TailHistoryReply{kind=ENDED,
history=HistoryEntry{id=hid,...}}` to every open `TailHistory` stream (the reconciler's). The
reconciler then calls `command_output(hid)`, finds the injected capture, and upserts it.

CONFIRMED from reference: the daemon has a `DaemonEvent::HistoryEnded(_)` variant
(client.rs:447) and `Start/EndHistory` RPCs exist (proto:75-81) — strongly implies End broadcasts
an ENDED tail event. NOT fully confirmable: the daemon **server** source is not vendored in
`reference/atuin-src` (only `client.rs` + semantic component). **Mitigation / de-risking step:**
before writing the assertions, add a throwaway spike (or a first sub-test) that opens a
`TailHistory` stream in a thread, then does Start→End, and asserts an `ENDED` reply with the id
arrives within ~5s. If that fails, the broadcast assumption is wrong and the whole approach needs
revisiting (fallback: drive a real command through `atuin` shell integration — much heavier).
Flag this spike as the gating check.

**Ordering:** inject the capture BEFORE `EndHistory`. `reconcile_ended` retries 8×250ms (2s) so a
capture arriving slightly late still works, but injecting first removes that race. `StartHistory`
must precede injection (need the daemon-generated id). All three (Start/inject/End) must happen
AFTER the reconciler's tail stream is open (see §4 readiness gate).

---

## 3. Fixtures and helpers

All in `tests/test_reconciler_process.py`.

```python
import contextlib, os, shutil, signal, socket, subprocess, sys, time
from pathlib import Path
from collections.abc import Iterator
import pytest

pytestmark = [
    pytest.mark.slow,   # opt-in: spawns processes, waits on timers
    pytest.mark.skipif(shutil.which("atuin") is None, reason="atuin binary not available"),
]

REPO_ROOT = Path(__file__).resolve().parent.parent
```

### 3a. `atuin_daemon` fixture — reuse the existing pattern (test_integration_daemon.py:32-60)

Same body: set `HOME`, `XDG_DATA_HOME` under `tmp_path`, delete `ATUOUT_DAEMON_SOCKET`, spawn
`atuin daemon` with `start_new_session=True`, wait ≤15s for the socket, teardown via
`os.killpg(os.getpgid(proc.pid), SIGTERM)`. Yields the socket path string.
NOTE: this fixture sets HOME etc via monkeypatch — that env is what the *test process* sees; the
child reconciler gets its env from an explicit dict we build (§3c), not from monkeypatch.

### 3b. Per-test isolation env additions

The autouse `_isolate_env` (conftest.py:24) already sets `ATUOUT_DB_PATH`, `ATUOUT_STATE_DIR`,
and clears `XDG_RUNTIME_DIR`. For these tests we additionally need a temp `XDG_RUNTIME_DIR` so the
lock/pidfile live under `tmp_path` (avoids leaking into the real `$XDG_RUNTIME_DIR/atuout`):

```python
@pytest.fixture
def runtime_env(tmp_path, monkeypatch, atuin_daemon):
    run_dir = tmp_path / "run"; run_dir.mkdir()
    db = tmp_path / "recon.db"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_dir))
    monkeypatch.setenv("ATUOUT_DB_PATH", str(db))
    monkeypatch.setenv("ATUOUT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATUOUT_DAEMON_SOCKET", atuin_daemon)
    return {"db": db, "sock": atuin_daemon, "run_dir": run_dir}
```

### 3c. Child env dict (explicit, not inherited-by-accident)

The detached child must resolve the SAME db/socket/runtime dirs. Build from `os.environ` + overrides
so the child sees exactly what the test configured:

```python
def _child_env(env):
    e = os.environ.copy()
    e["ATUOUT_DB_PATH"]      = str(env["db"])
    e["ATUOUT_DAEMON_SOCKET"]= env["sock"]
    e["XDG_RUNTIME_DIR"]     = str(env["run_dir"])
    e["ATUOUT_STATE_DIR"]    = str(Path(env["run_dir"]).parent / "state")
    return e
```

### 3d. Spawn helper — bypass `ensure()`'s PATH dependency (HARD QUESTION 3, recommended)

Launch the reconciler as a module via the venv interpreter running the test. `sys.executable` is
the venv python (pytest runs under `.devenv/state/venv/bin/python`), so `-m atuout.cli` guarantees
the correct entrypoint without depending on `atuout` being on PATH:

```python
def _spawn_reconciler(env):
    proc = subprocess.Popen(
        [sys.executable, "-m", "atuout.cli", "reconcile", "--daemonize"],
        env=_child_env(env),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,   # own process group → clean pgid kill in teardown
        cwd=str(REPO_ROOT),
    )
    return proc
```

RECOMMENDATION: prefer `_spawn_reconciler` for the spawn+backfill and stop/crash tests — it is the
most reliable (no PATH assumptions, correct interpreter, explicit env). Use the real `ensure()`
ONLY in the single-instance test (§5.2) where testing `ensure`'s spawn decision is the point, and
even there mitigate the hardcoded-`"atuout"` problem via the seam in §7.

### 3e. Readiness gate — wait until the reconciler is tailing

`is_running()` becomes true as soon as the child grabs the lock, but the `TailHistory` stream may
open a beat later; events sent before it opens are missed. Poll for the pidfile (written right
before `_run_loop`) plus a short settle, OR (more robust) poll `is_running()` then sleep ~0.5s:

```python
def _wait_running(timeout=10.0):
    from atuout import reconciler
    deadline = time.time() + timeout
    while time.time() < deadline:
        if reconciler.is_running():
            return True
        time.sleep(0.05)
    return False
```
After `_wait_running()` returns, sleep an extra ~0.75s to let the tail stream attach before firing
Start/End. (Optional stronger gate: §9 seam to log/flag "tailing".)

### 3f. DB poll waiter (HARD QUESTION 2 — observe effect out-of-process)

The test NEVER calls `reconcile_ended`. It opens its own store connection and polls:

```python
def _wait_for_recording(db, atuin_id, timeout=10.0):
    from atuout import store
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = store.connect(db)          # fresh conn each poll: sees the child's committed writes
        rec = store.get_recording(conn, atuin_id)
        if rec is not None:
            return rec
        time.sleep(0.1)
    return None
```
Fresh `store.connect()` per iteration avoids sqlite snapshot/caching staleness across processes.

### 3g. History Start/End + capture helper

```python
def _start_history(sock, command="pwd", session="integration-session"):
    import grpc
    from atuout._proto import history_pb2, history_pb2_grpc
    ch = grpc.insecure_channel(f"unix:{sock}", options=[("grpc.default_authority","localhost")])
    reply = history_pb2_grpc.HistoryStub(ch).StartHistory(
        history_pb2.StartHistoryRequest(
            command=command, cwd="/tmp", session=session,
            hostname="test", timestamp=int(time.time()*1e9)),
        timeout=5)
    return reply.id

def _end_history(sock, hid, exit=0, duration=1000):
    import grpc
    from atuout._proto import history_pb2, history_pb2_grpc
    ch = grpc.insecure_channel(f"unix:{sock}", options=[("grpc.default_authority","localhost")])
    history_pb2_grpc.HistoryStub(ch).EndHistory(
        history_pb2.EndHistoryRequest(id=hid, exit=exit, duration=duration), timeout=5)
```
Reuse `_inject_capture` and `_semantic_available` verbatim from test_integration_daemon.py.

### 3h. Guaranteed teardown of the reconciler child (avoid the pkill self-match trap)

Never `pkill -f`. Kill by pgid of the Popen handle. Wrap each spawned proc in a fixture/try-finally:

```python
def _kill(proc):
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)
```
A `@pytest.fixture` `reconciler_procs` returning a list, with teardown looping `_kill`, guarantees
cleanup even on assertion failure. The `atuin_daemon` fixture already kills the daemon by pgid.

---

## 4. Skip conditions

At test top (after fixtures resolve): `if not _semantic_available(sock): pytest.skip("atuin build
predates PR #3510 (no Semantic service)")`. Module-level skipif already covers missing `atuin`
binary. Also `pytest.mark.slow` so the suite is opt-in (run with `-m slow`, or register the marker
in pyproject/pytest.ini — call out that `slow` marker registration may be needed to avoid
`PytestUnknownMarkWarning`; check pyproject `[tool.pytest.ini_options] markers`).

---

## 5. Test scenarios (recommended minimal high-value set)

### 5.1 `test_spawn_and_backfill` — THE core test (spawn + live ENDED + out-of-process backfill)

Proves: a real detached process boots, holds the lock, tails, reacts to a live daemon ENDED event,
and persists to SQLite with zero in-process reconciler calls.

Steps:
1. `if not _semantic_available(env["sock"]): pytest.skip(...)`.
2. `proc = _spawn_reconciler(env)`; register for teardown.
3. `assert _wait_running()`; sleep ~0.75s (tail attach settle, §3e).
4. `assert reconciler.read_pid() == proc.pid` (pidfile written by the child; proves the spawned
   PID owns the lock — see caveat below).
5. `hid = _start_history(env["sock"])`.
6. `_inject_capture(env["sock"], hid, "out\n", command="pwd", exit_code=0)` (capture BEFORE end).
7. `_end_history(env["sock"], hid, exit=0)`  → daemon broadcasts ENDED.
8. `rec = _wait_for_recording(env["db"], hid, timeout=10)`.
9. `assert rec is not None; assert rec.output == "out"; assert rec.source == "reconciler";
   assert rec.command == "pwd"; assert rec.exit_code == 0`.

Caveat on step 4: `read_pid()` returns the child's `os.getpid()`. Since we spawn via
`python -m atuout.cli`, `os.getpid()` in the child == `proc.pid` (no extra fork; `-m` execs in the
same process). Confirmed: `cmd_reconcile` calls `reconciler.run()` in-process, no re-exec.
So the equality holds. (If it proves flaky, downgrade to `assert reconciler.read_pid() is not None`.)

### 5.2 `test_single_instance_no_duplicate` — single-instance guarantee

Proves: while a real reconciler holds the lock, `reconcile ensure` does NOT spawn a duplicate.

Steps:
1. `proc = _spawn_reconciler(env)`; `assert _wait_running()`.
2. Run a second `atuout reconcile ensure` as a subprocess with the SAME `_child_env`:
   `out = subprocess.run([sys.executable,"-m","atuout.cli","reconcile","ensure"], env=_child_env(env),
   capture_output=True, text=True, timeout=15)`.
3. `assert "already running" in out.stdout` (cmd_reconcile prints "reconciler already running"
   when `ensure()` returns False — cli.py:75).
4. Assert only ONE holder: `pid1 = reconciler.read_pid()`; assert it still equals `proc.pid`;
   assert no second reconciler python process exists for this run dir (optional: `pgrep -g` by the
   first proc's pgid, or simply that `read_pid()` is unchanged and `is_running()` true).
   The `ensure` subprocess returns quickly and exits 0 without spawning (verified: `ensure()`
   short-circuits on `is_running()`).

This exercises the REAL `ensure()` path (via the CLI subprocess) — but note it internally would
`Popen(["atuout",...])` only if `is_running()` were False; since it is True, the hardcoded-PATH
branch is never reached here, so no seam is strictly required for THIS test. (Seam still wanted if
we ever want a test where `ensure` actually spawns — see §7.)

### 5.3 `test_stop_clean_shutdown` — SIGTERM removes pidfile, releases lock

Proves: `reconcile stop` (SIGTERM) → child runs its `finally` (remove pidfile, unlock) → is_running
false.

Steps:
1. `proc = _spawn_reconciler(env)`; `assert _wait_running()`.
2. `subprocess.run([sys.executable,"-m","atuout.cli","reconcile","stop"], env=_child_env(env),
   capture_output=True, text=True, timeout=10)`; assert stdout contains "sent stop".
3. Poll for shutdown: `deadline=...; while is_running() and not timeout: sleep(0.1)`.
4. `assert reconciler.is_running() is False`.
5. `assert not reconciler.pidfile_path().exists()` (child's `_remove_pidfile`).
6. `proc.wait(timeout=5)` returns (process exited); `assert proc.returncode == 0`.

Timing risk: the tail stream's `next()` may be blocked when SIGTERM arrives; the signal handler
just sets the flag, and the loop only re-checks the flag between events. See §6 flakiness — the
handler interrupting a blocked `next()` should raise/return and let the loop see the flag; if the
stream is idle-blocking with no events, SIGTERM still terminates the process because Python's
default behavior after the handler returns resumes the blocked C call... This is the main risk;
mitigation in §6.

### 5.4 `test_crash_restart` (feasibility: MODERATE — include, guarded)

Proves: SIGKILL leaves stale pidfile but flock auto-releases (fd closed on death) → `is_running()`
false → `ensure` can start a fresh instance.

Steps:
1. `proc = _spawn_reconciler(env)`; `assert _wait_running()`; record `pid1 = proc.pid`.
2. `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)`; `proc.wait(5)`.
3. Poll `assert reconciler.is_running() is False` within ~5s (kernel releases flock on process
   death; the stale pidfile may still exist — `is_running()` is lock-based, not pidfile-based, so
   it correctly reports False). This is the valuable assertion.
4. Restart: `proc2 = _spawn_reconciler(env)` (equivalent to what `ensure` would do); `assert
   _wait_running()`; `assert reconciler.read_pid() == proc2.pid != pid1`.
   (Using `_spawn_reconciler` rather than `ensure()` avoids the PATH issue; if you want to prove
   `ensure` specifically restarts, that needs the §7 seam.)

Feasibility note: fully feasible and fast (no backoff timers involved). Recommend INCLUDE.

### Scenarios kept at unit level (do NOT re-test as processes)
- `reconcile_ended` backfill/skip/not-found logic → covered by test_reconciler.py + in-process
  integration test.
- Lock acquire/release semantics in-process → test_single_instance_lock.
- `ensure(spawn=False)` / ensure-no-spawn-when-running (lock held in-process) → existing.

---

## 6. Timing / flakiness risks and mitigations

1. **Tail-attach race** (events fired before the child's stream opens): mitigate with the
   readiness gate + 0.75s settle (§3e). Stronger option: §9 seam to expose a "tailing" signal
   (e.g. touch a sentinel file / log line the test can poll). Recommend starting with the settle;
   add the seam only if flaky.
2. **Reconnect backoff (1s→30s)**: only triggers on `DaemonError`/exceptions. With a healthy daemon
   the loop never sleeps. Keep the daemon alive for the whole test; never kill it mid-test. This
   keeps tests fast. If the child starts BEFORE the socket is fully ready it could hit one 1s
   backoff — the `atuin_daemon` fixture already waits for the socket to exist, and we spawn the
   reconciler after, so first connect should succeed. Budget waiter timeouts ≥10s to absorb one
   backoff cycle just in case.
3. **SIGTERM while blocked in `next(stream)`**: the handler sets the flag but a blocking gRPC
   `next()` may not return until an event arrives, so the process might not exit promptly on an
   idle stream. RISK for 5.3. Mitigations, in order of preference:
   - Give the stop test a generous poll (up to ~10s) for `is_running()` to flip.
   - If it hangs: this indicates a real product gap (reconciler doesn't shut down promptly when
     idle). Call it out as a **potential source bug / seam** (§7): the loop could pass a per-item
     deadline to `tail_history()` or run the stream on a thread it can cancel. DO NOT fix here —
     flag it. As a test-only fallback, fire one more `Start/End` after sending SIGTERM to unblock
     the stream so the loop wakes, sees the flag, and exits; document why.
4. **DB visibility across processes**: fresh `store.connect` per poll (§3f); WAL/rollback commit is
   flushed by the child's `upsert_recording`. Confirm `store.connect` doesn't hold a long snapshot.
5. **Lock/pidfile leakage between tests**: per-test `XDG_RUNTIME_DIR` under `tmp_path` (§3b) +
   guaranteed pgid kill (§3h). No shared global state.
6. **`read_pid()==proc.pid`**: valid because `-m atuout.cli` does not re-fork; downgrade assertion
   if flaky.

---

## 7. Source-code testability seams to CALL OUT (do NOT implement here)

1. **`reconciler.ensure()` hardcodes `["atuout", ...]`** (reconciler.py:204). For a test that
   exercises `ensure`'s actual spawn (not just its no-spawn branch), `atuout` must be on PATH as the
   venv console script. Seam options (pick one when implementing):
   - Add an env override, e.g. read `ATUOUT_RECONCILER_CMD` (default `["atuout","reconcile",
     "--daemonize"]`), OR
   - Accept an optional `cmd`/`argv` parameter on `ensure()` defaulting to current behavior.
   Recommendation: env-var seam (no signature churn; the detached child inherits env anyway).
   NOTE: the recommended tests avoid needing this by spawning via `sys.executable -m atuout.cli`;
   the seam is only required if we want to assert `ensure()` itself restarts a crashed instance
   (5.4 alternative). Flag as optional.
2. **Prompt shutdown when the tail stream is idle** (reconciler.py:148-157, §6.3). If 5.3 proves the
   process doesn't exit promptly on SIGTERM while blocked in `next()`, that is a genuine
   responsiveness gap. Seam: iterate the stream with a bounded per-item deadline so the loop
   periodically re-checks `stop_flag`. Flag as a possible source improvement; do not implement in
   this planning task.
3. **Optional "tailing" readiness signal** (§9): a log line or sentinel the test can poll to close
   the tail-attach race deterministically instead of a fixed sleep. Optional.

No other seams required — env-based config (`ATUOUT_DB_PATH`, `ATUOUT_DAEMON_SOCKET`,
`XDG_RUNTIME_DIR`, `ATUOUT_STATE_DIR`) already fully controls the child.

---

## 8. Code organization

- New file `tests/test_reconciler_process.py`.
- Copy `_inject_capture`, `_semantic_available` from `test_integration_daemon.py` (small, and
  keeping process tests self-contained avoids cross-module import coupling), OR refactor both into
  `tests/support/atuin_daemon.py` and import from both files (cleaner; a minor optional refactor —
  flag it, don't require it).
- The `atuin_daemon` fixture body is duplicated or, better, moved to `tests/support/` and imported.
  Recommendation: extract `atuin_daemon`, `_inject_capture`, `_semantic_available` into a shared
  `tests/support/atuin_daemon.py` in a follow-up; for the first cut, duplicate to keep the diff
  contained.

---

## 9. Optional stronger readiness gate (if fixed-sleep proves flaky)

Add to the reconciler (seam) a one-line sentinel write right after `tail_history()` connects
(inside `_run_loop` after `log.info("reconciler: tailing history")`), e.g. touch
`runtime_dir()/atuout-reconciler.tailing`. Test polls for that file instead of sleeping. Keep it
behind the existing log call so it's cheap. Flag as optional; implement only if §6.1 flakes.

---

## 10. Running the tests

```
devenv shell bash -- -c 'uv run pytest tests/test_reconciler_process.py -q --no-cov \
  -p no:cacheprovider -o addopts="" -m slow'
```
(Inside `devenv shell`, `LD_LIBRARY_PATH` for grpcio and `atuin` on PATH are already set.)
If the `slow` marker isn't registered, either register it in pyproject `[tool.pytest.ini_options]`
`markers = ["slow: spawns processes / waits on timers"]` or drop the marker and rely on the
`atuin`-missing skipif for CI gating. Recommend registering the marker and making these opt-in.

---

## 11. Open items to verify during implementation (flagged, unconfirmed)

- [ ] **CONFIRM the ENDED broadcast** on `EndHistory` (§2a) via the gating spike before writing
      assertions. Daemon server source not vendored — this is the single biggest assumption.
- [ ] Confirm `StartHistory` accepts the minimal request fields used in §3g against the live daemon
      (session non-empty; timestamp as u64 ns).
- [ ] Confirm the daemon associates the injected capture (RecordCommands) with the StartHistory id
      purely by matching `history_id` (existing roundtrip test uses an arbitrary id, so likely yes).
- [ ] Confirm prompt SIGTERM shutdown on an idle tail stream (§6.3); if not, log the source gap.
- [ ] Confirm `read_pid() == proc.pid` under `-m atuout.cli` (no re-fork) — expected true.
```
