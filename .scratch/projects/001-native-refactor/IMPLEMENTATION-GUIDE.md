# atuout native refactor — step-by-step implementation guide

Status: ready to execute. All design decisions (SQLite, unbounded retention, no asciinema
fallback, Option D) are **fixed**; this guide plans *around* them and does not re-open them.
Source-of-truth research: `ANALYSIS.md`, `PLAN.md`, `RACE-WINDOW-OPTIONS.md`, and
`reference/atuin-src/` (atuin @ `3f08db6b84bd2ff151d9e6560bb057dd55e3bc53`).

---

## 0. Decisions locked in before writing code

### 0.1 gRPC/proto tooling choice — **`grpcio` + `grpcio-tools`, code checked in**

Rationale:
- The daemon speaks **standard gRPC over HTTP/2 on a Unix domain socket** (`tonic` server). We
  need a client that (a) supports UDS and (b) supports server-streaming (`TailHistory`).
- `grpcio` (the official C-core binding) supports UDS via the `unix:` / `unix-abstract:` target
  scheme and supports streaming. It is synchronous, which fits our two consumers: a one-shot
  `harvest` process (blocking retry loop) and a long-lived reconciler thread (blocking stream
  iteration). No asyncio needed anywhere → simpler code and tests.
- `betterproto`/`grpclib` are asyncio-only and would force async into the harvester and the zsh
  hot path for no benefit. Rejected.
- **Codegen output is checked into the repo**, not generated at build time. Reasons: the two
  `.proto` files are vendored and pinned (`reference/atuin-src/*.proto`); end users installing
  `atuout` from a wheel must not need `protoc`/`grpcio-tools`; and we want the generated stubs to
  be import-stable and mypy-visible. Regeneration is a dev-time `make`/script step, not part of
  `pip install`.

Generated modules live in a dedicated package: `src/atuout/_proto/`:
```
src/atuout/_proto/
  __init__.py
  semantic_pb2.py        # generated
  semantic_pb2.pyi       # generated (grpcio-tools --mypy_out if mypy-protobuf available; else hand-thin)
  semantic_pb2_grpc.py   # generated
  history_pb2.py         # generated
  history_pb2.pyi
  history_pb2_grpc.py
```
Add a regeneration script `scripts/gen_proto.sh` (checked in) that runs:
```sh
python -m grpc_tools.protoc \
  -I .scratch/projects/001-native-refactor/reference/atuin-src \
  --python_out=src/atuout/_proto \
  --grpc_python_out=src/atuout/_proto \
  crates_atuin-daemon_proto_semantic.proto \
  crates_atuin-daemon_proto_history.proto
```
> ⚠️ grpcio-tools emits imports as `import <name>_pb2 as ...` using the proto file's basename.
> The vendored filenames (`crates_atuin-daemon_proto_semantic.proto`) produce ugly module names
> and broken package-relative imports. **Fix:** before generating, copy the two protos to plain
> names `semantic.proto` / `history.proto` inside `src/atuout/_proto/` (or a `proto/` dir),
> generate there with `--python_out`/`--grpc_python_out=.`, and post-process the one
> `import semantic_pb2` line in `history`/`grpc` files to `from atuout._proto import ...` (or add
> `src/atuout/_proto` to a sys.path shim in `_proto/__init__.py`). The script must be
> deterministic and idempotent. Keep the plain-named `.proto` copies in `src/atuout/_proto/` so
> regeneration doesn't depend on the `.scratch` reference tree, which is not shipped.

`.proto` package names: `semantic` and `history` (from `package semantic;` / `package history;`),
so the generated service stubs are `semantic_pb2_grpc.SemanticStub` and
`history_pb2_grpc.HistoryStub`.

### 0.2 Daemon socket path resolution — **mirror atuin's `settings.daemon.socket_path`**

⚠️ **The default is NOT in the vendored reference and must be verified against live atuin before
coding.** The vendored `runtime.rs` `screen::socket_path()` is the *pty-proxy screen socket*
(`ATUIN_PTY_PROXY_SOCKET`), a **different** socket from the daemon's gRPC socket — do not confuse
them. What we need is `Settings::daemon.socket_path`, defined in
`atuin-client/src/settings.rs` (not vendored).

Known/expected shape (verify each ⚠️ item):
- Config file: `~/.config/atuin/config.toml`, section `[daemon]`.
- Keys: `enabled` (bool), `socket_path` (string), possibly `systemd_socket` (bool),
  `socket_dir`, and on non-unix `tcp_port`. ⚠️ confirm exact key names + nesting.
- Default `socket_path`: ⚠️ atuin historically defaults to
  **`<data_dir>/atuin.sock`** where `data_dir` = `$XDG_DATA_HOME/atuin` →
  `~/.local/share/atuin/atuin.sock`. Confirm against live `settings.rs` (the default may be
  computed via `atuin_common::utils::data_dir()`), and confirm whether env override
  `ATUIN_DAEMON_SOCKET_PATH` / general atuin env-var override scheme applies.

Python resolution order in `src/atuout/settings.py` (`daemon_socket_path()`):
1. `ATUOUT_DAEMON_SOCKET` env var (our own explicit override — always wins, for tests/power users).
2. Parse `~/.config/atuin/config.toml` (`tomllib`, stdlib on 3.11+): `[daemon].socket_path` if present.
   Honor `$ATUIN_CONFIG_DIR` if set (atuin respects it) before falling back to `~/.config/atuin`.
3. Fallback default: `${XDG_DATA_HOME:-~/.local/share}/atuin/atuin.sock`. ⚠️ verify this literal.

Also read `[daemon].enabled` here so `atuout status` can warn "daemon disabled in atuin config".
grpcio UDS target string: `f"unix:{socket_path}"` (absolute path → `unix:/home/...`).

### 0.3 Confirmed: no migration path

There is **no production data migration**. Old `.cast`/`.meta` files are abandoned in place (not
read, not deleted, not imported). Fresh SQLite schema, version 1. `atuout list`/`show` no longer
look at `.cast` files at all. (If desired later, a one-off `atuout import-legacy` could scavenge
old casts, but it is explicitly out of scope.)

---

## 1. Dependencies & project metadata

**File: `pyproject.toml`** (modify)
- Remove any asciinema assumption from `description` (there's no asciinema *dependency* listed
  today — it was an implicit external binary — but the description mentions it).
- Add runtime deps:
  ```toml
  dependencies = [
    "pydantic>=2.12.5",
    "grpcio>=1.60",
    "protobuf>=4.25",
  ]
  ```
- Add dev deps for codegen + typing:
  ```toml
  dev = [
    "pytest>=7.0", "pytest-cov>=4.1", "mypy>=1.10", "ruff>=0.5.0",
    "grpcio-tools>=1.60",
    "mypy-protobuf>=3.5",   # optional, for .pyi generation
  ]
  ```
- `[tool.hatch.build.targets.wheel]` — ensure generated `_proto` package and `shell/atuout.zsh`
  ship in the wheel. Add package-data include for `shell/*.zsh` (see §7 note on `init-zsh` path
  resolution — currently it walks `../../../../shell`, which only works from a source checkout).
- `[tool.mypy]` — add `[[tool.mypy.overrides]]` with `module = ["atuout._proto.*"]` and
  `ignore_errors = true` (generated code isn't strict-clean) OR rely on generated `.pyi`. Simpler:
  ignore the generated package.

---

## 2. Proto codegen (Phase 1a)

**Add:** `src/atuout/_proto/{semantic,history}.proto` (plain-named copies of the vendored protos),
`scripts/gen_proto.sh`, and the generated `*_pb2*.py` files.

Steps:
1. Copy vendored protos to `src/atuout/_proto/semantic.proto` and `.../history.proto`.
2. Run `scripts/gen_proto.sh`; commit the generated outputs.
3. Add `src/atuout/_proto/__init__.py` that makes cross-module imports resolve (either post-process
   the generated `import semantic_pb2` → `from . import semantic_pb2`, or insert
   `sys.path.insert(0, os.path.dirname(__file__))` in `__init__.py`). Prefer the post-process:
   cleaner, no sys.path hacks. Encode the sed/replace in `gen_proto.sh` so it's reproducible.
4. Verify: `python -c "from atuout._proto import semantic_pb2_grpc, history_pb2_grpc"`.

---

## 3. Daemon gRPC client (Phase 1b)

**Add: `src/atuout/daemon_client.py`**

Responsibilities: connect to the daemon UDS, expose `command_output()` and `tail_history()`, and
translate gRPC failures into one distinguishable exception type.

```python
class DaemonError(Exception):
    """Any failure talking to the atuin daemon (connect/unavailable/unimplemented/other)."""
    def __init__(self, msg: str, *, kind: str) -> None: ...
    # kind ∈ {"connect", "unavailable", "unimplemented", "other"}  — mirrors
    # DaemonClientErrorKind in reference client.rs classify_error()
```

Client:
```python
class DaemonClient:
    def __init__(self, socket_path: str) -> None:
        self._target = f"unix:{socket_path}"
        self._channel = grpc.insecure_channel(self._target)
    def close(self) -> None: self._channel.close()
    def __enter__/__exit__ ...

    def command_output(self, history_id: str) -> semantic_pb2.CommandOutputReply:
        stub = semantic_pb2_grpc.SemanticStub(self._channel)
        req = semantic_pb2.CommandOutputRequest(history_id=history_id, ranges=[])  # empty=full
        try:
            return stub.CommandOutput(req, timeout=CALL_TIMEOUT)
        except grpc.RpcError as e:
            raise DaemonError(str(e), kind=_classify(e)) from e

    def tail_history(self) -> Iterator[history_pb2.TailHistoryReply]:
        stub = history_pb2_grpc.HistoryStub(self._channel)
        try:
            yield from stub.TailHistory(history_pb2.TailHistoryRequest())
        except grpc.RpcError as e:
            raise DaemonError(str(e), kind=_classify(e)) from e
```

Notes:
- Empty `ranges` = full output is **confirmed optional** by `atuin_output_ranges_are_optional`
  test referenced in `PLAN.md` (see `tools_mod.rs`). Good.
- `_classify(grpc.RpcError)` maps `e.code()`: `UNAVAILABLE→"unavailable"`,
  `UNIMPLEMENTED→"unimplemented"`, channel/connect failures also surface as `UNAVAILABLE` from
  grpcio → treat as retryable. Everything else → `"other"`.
- `CALL_TIMEOUT` short (e.g. 1s) for `command_output`; `tail_history` has no timeout (long-lived).
- `daemon_socket_path()` + `daemon_enabled()` live in `settings.py` (§0.2), passed in by callers.

---

## 4. SQLite store (Phase 2)

**Add: `src/atuout/store.py`**

DB location: `${XDG_DATA_HOME:-~/.local/share}/atuout/atuout.db` (env override `ATUOUT_DB_PATH`
and legacy `ATUOUT_DATA_DIR` honored — tests set these). The old `recordings/` dir is no longer used.

**Finalized schema (v1):**
```sql
PRAGMA journal_mode=WAL;         -- concurrent reconciler + harvest + reader
PRAGMA busy_timeout=5000;        -- absorb writer contention between processes
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- seed: INSERT OR IGNORE INTO schema_meta(key,value) VALUES ('version','1');

CREATE TABLE IF NOT EXISTS recordings (
  atuin_id    TEXT PRIMARY KEY,            -- ATUIN_HISTORY_ID; the join key
  command     TEXT NOT NULL,               -- from CommandOutputReply? NO — see note
  prompt      TEXT,                         -- reserved; CommandOutputReply has no prompt → NULL for now
  output      TEXT NOT NULL,               -- CommandOutputReply.output (full text)
  exit_code   INTEGER,                     -- see note: not in CommandOutputReply
  total_bytes INTEGER,                     -- CommandOutputReply.total_bytes
  total_lines INTEGER,                     -- CommandOutputReply.total_lines
  captured_at INTEGER NOT NULL,            -- unix ms, when atuout harvested it
  source      TEXT NOT NULL DEFAULT 'fast' -- 'fast' | 'reconciler' — provenance/debugging
);

CREATE INDEX IF NOT EXISTS idx_recordings_captured_at ON recordings(captured_at);
```

> **Important data-model note — `command` and `exit_code` are NOT in `CommandOutputReply`.**
> The semantic `CommandOutputReply` (semantic.proto:37) returns only
> `found, output, total_bytes, total_lines, lines[]` — **no command text, no exit code**.
> Two sources can supply them:
> 1. The **fast path** (`harvest <id>`) is invoked from `precmd`, which *does* know the command
>    string (`$1` in preexec is stashed) and `$?` exit code. So the zsh hook passes them as args:
>    `atuout harvest <id> --command "$cmd" --exit-code "$code"`.
> 2. The **reconciler** learns them from `TailHistoryReply.history` (`HistoryEntry.command`,
>    `.exit`) on the `ENDED` event — see history.proto:57-68. So the reconciler has them too.
> Therefore both write paths can populate `command`/`exit_code`. Keep both columns NULL-able so a
> harvest that somehow lacks them still persists `output`. `Recording.success` derives from
> `exit_code` (see §5).

Store API (sync, stdlib `sqlite3`, one connection per process, `INSERT OR IGNORE` for idempotency):
```python
def connect(db_path: Path | None = None) -> sqlite3.Connection   # applies PRAGMAs + migrations
def upsert_recording(conn, *, atuin_id, command, output, exit_code,
                     total_bytes, total_lines, captured_at_ms, source) -> bool   # False if already present
def get_recording(conn, atuin_id) -> Recording | None
def list_recordings(conn, *, limit=None) -> list[Recording]      # newest-first by captured_at
def has_recording(conn, atuin_id) -> bool                        # reconciler dedupe check
```
Migration approach: single-file idempotent `CREATE TABLE IF NOT EXISTS` + a `schema_meta.version`
row. No Alembic. A `_migrate(conn)` function switch on version handles future bumps; v1 is just
"create if absent". No migration from `.cast` (confirmed §0.3).

---

## 5. `Recording` — second construction path (Phase 2)

**Modify: `src/atuout/recording.py`**

Keep public API stable: `output`, `output_lines`, `exit_code`, `success`, `atuin_id`, `command`,
`__str__`, plus `duration` (see note). Change: it is no longer built by parsing a `.cast` file;
it is built from a DB row or a `CommandOutputReply`.

Approach — keep the dataclass but make cast-parsing optional/legacy and add explicit fields +
classmethods:
```python
@dataclass
class Recording:
    command: str
    atuin_id: str | None = None
    _output: str | None = None          # populated from DB/reply
    exit_code: int | None = None        # now a stored field, not parsed
    total_bytes: int | None = None
    total_lines: int | None = None
    captured_at_ms: int | None = None
    cast_path: Path | None = None       # legacy/optional; kept so old callers don't explode

    @property
    def output(self) -> str: return self._output or ""
    @property
    def output_lines(self) -> list[str]: return self.output.splitlines()
    @property
    def success(self) -> bool: return self.exit_code == 0    # None → False
    # duration: no longer meaningful (no timing in CommandOutputReply). Return 0.0 and deprecate,
    # OR drop from public surface. RECOMMEND: keep property returning 0.0 to avoid breaking callers,
    # document as always-0 under the new model. (Flag for user: is `duration` still needed?)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Recording": ...
    @classmethod
    def from_reply(cls, reply, *, atuin_id, command, exit_code) -> "Recording": ...
```
`__str__` unchanged in shape (`Recording(ok atuin=... 'cmd')`).

> The old lazy `.cast` parser (`_parse`, `header`, `events`, `input_events`) is **removed**.
> `input_events` had no consumers under the new model (no keystroke capture) — drop it. If any
> test references it, that test is being rewritten anyway (§9).

---

## 6. Harvester — fast path (Phase 3a)

**Add: `src/atuout/harvest.py`**

```python
def harvest(atuin_id, *, command=None, exit_code=None,
            attempts=..., delay_ms=..., db_path=None, socket_path=None,
            source="fast") -> Recording | None:
    """Fetch capture for atuin_id from the daemon and persist it. Returns the Recording on
    success, None if not found after the retry window."""
```
Logic:
1. Resolve `socket_path` (settings.py) and open `DaemonClient`.
2. Retry loop: `attempts` times (default 3), `delay_ms` apart (default 50ms) — absorbs the
   pty-proxy→daemon batching window (≤25ms/64-item, per `command_mod.rs`). Env-tunable:
   `ATUOUT_HARVEST_ATTEMPTS`, `ATUOUT_HARVEST_DELAY_MS`.
3. Call `command_output(atuin_id)`; if `reply.found`, `upsert_recording(...)` and return the
   `Recording`. If `INSERT OR IGNORE` reports already-present (reconciler beat us), still return
   the existing row.
4. On `DaemonError(kind in {unavailable, connect})` → these are retryable within the loop.
   `unimplemented` → daemon too old / feature off → log once and bail (no point retrying).
5. Exhausted without `found` → return None, log to the harvest log (§8). Since pty-proxy is
   mandatory (`PLAN.md` §hard requirement), a persistent `found=False` is a real error worth
   surfacing via `atuout status`, not steady-state noise.

The process must **never** raise into the shell — `cmd_harvest` catches everything, logs, exits 0
(it's detached/disowned anyway; exit code is unobserved).

---

## 7. Reconciler — safety net (Phase 3b)

**Add: `src/atuout/reconciler.py`**

A single system-wide long-lived process holding a `TailHistory()` stream open. On each `ENDED`
event it backfills any capture the fast path missed.

### 7.1 Lifecycle mechanics (concrete)

- **Spawn:** lazily from `atuout init-zsh`-emitted hook code, at shell startup, via a detached
  double-fork/`setsid`: `setsid atuout reconcile --daemonize >/dev/null 2>&1 &` (disowned). The
  hook calls a lightweight `atuout reconcile ensure` that (a) checks the pidfile/lock and (b)
  starts the daemon only if not already running — so opening 10 terminals starts at most one.
- **Single-instance guard — pidfile + advisory lock:**
  - Pidfile: `${XDG_RUNTIME_DIR:-~/.local/share/atuout}/atuout-reconciler.pid`.
    Prefer `$XDG_RUNTIME_DIR` (tmpfs, auto-cleaned on logout) if set; else
    `~/.local/share/atuout/atuout-reconciler.pid`.
  - Format: two lines — `pid\nstart_unix_ms\n`. (start time lets us detect PID reuse.)
  - Locking: `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on a sibling lockfile
    `atuout-reconciler.lock`. The running reconciler holds the flock for its whole life; a second
    `reconcile ensure` that fails to acquire the lock knows one is already running and exits. This
    is race-free in a way a bare pidfile isn't. Write the pidfile *after* acquiring the lock.
  - On start: acquire lock → write pidfile → run. On clean exit / signal: release lock, remove
    pidfile (best-effort; the flock is the real guard).
- **`ensure` decision logic (what `init-zsh` runs):**
  1. Try `flock(LOCK_NB)` on the lockfile in a probe child; if it *fails*, a reconciler is alive → do nothing.
  2. If it succeeds, release and spawn `setsid atuout reconcile --daemonize`. (Small TOCTOU window
     is fine — the real daemon re-acquires the lock and a duplicate that loses the race exits.)
- **Restart on crash:** no supervisor daemon; the next new shell's `reconcile ensure` restarts it
  (self-healing on next terminal open). Document this: if it dies, it comes back on the next shell
  start. (A future `systemd --user` unit is the clean upgrade; out of scope now.)
- **Shutdown / restart commands:** `atuout reconcile stop` (read pidfile, `SIGTERM`),
  `atuout reconcile restart` (stop + ensure), `atuout reconcile status` (alive? stream connected?
  last event ts). These make failures debuggable.

### 7.2 Loop logic

```python
def run_reconciler():
    acquire_single_instance_lock()      # exits if already held
    write_pidfile()
    install_signal_handlers()           # SIGTERM/SIGINT → clean shutdown flag
    conn = store.connect()
    while not shutting_down:
        try:
            with DaemonClient(socket_path) as dc:
                for reply in dc.tail_history():          # blocks, yields on each event
                    if reply.kind != HISTORY_EVENT_KIND_ENDED: continue
                    h = reply.history
                    if store.has_recording(conn, h.id):  continue
                    _reconcile_one(dc, conn, h)          # patient retry: e.g. 8 attempts, 250ms
        except DaemonError:
            sleep_backoff()   # daemon down/restarted → reconnect with backoff (1s→…→30s cap)
```
`_reconcile_one` calls `command_output(h.id)` with a **more patient** budget than the fast path
(it isn't blocking anything), and on `found` upserts with `source="reconciler"`, pulling
`command=h.command`, `exit_code=h.exit` straight from the `HistoryEntry`.

### 7.3 Accepted limitation (documented, not solved)

`TailHistoryRequest` has no cursor/replay (history.proto:49) — forward-only stream. If the
reconciler is down when a command ends, that event is lost. Out of scope to fix now
(`RACE-WINDOW-OPTIONS.md` Option D "costs/limits"). A future periodic diff against atuin's
persisted history DB would close it.

---

## 8. Logging / observability

**Add: `src/atuout/log.py`** (tiny helper) → log file at
`${XDG_STATE_HOME:-~/.local/state}/atuout/atuout.log` (or under the data dir). Both `harvest`
and `reconciler` append structured one-liners (ts, component, atuin_id, outcome). This is how a
silent fast-path miss becomes discoverable, and what `atuout status` summarizes. Keep it dead
simple (stdlib `logging` with a rotating file handler, or plain append) — no new dep.

---

## 9. Shell hook rewrite (Phase 3)

**Rewrite: `shell/atuout.zsh`**

Remove: asciinema `preexec` recording, the `_atuout_cast_*` helpers, `wait` on recorder pid, the
`.meta` sidecar python-json emit. Add: the hard pty-proxy check, the reconciler `ensure`, and the
detached `harvest` in `precmd`.

```zsh
# ── hard requirement: must run inside `atuin pty-proxy` ──
if [[ -z "${ATUIN_PTY_PROXY_ACTIVE:-}" ]]; then
  print -u2 "atuout: requires 'atuin pty-proxy'. Add BEFORE atuout's init in ~/.zshrc:"
  print -u2 '  eval "$(atuin pty-proxy init zsh)"'
  print -u2 '  eval "$(atuout init-zsh)"'
  return 1    # abort sourcing — install no hooks that would only no-op
fi

: "${ATUOUT_ENABLED:=1}"
typeset -g _atuout_command=""

# start the reconciler once, system-wide (no-op if already running)
atuout reconcile ensure &>/dev/null

_atuout_preexec() { _atuout_command="$1"; }   # just remember the command text

_atuout_precmd() {
  local exit_code=$?
  [[ "$ATUOUT_ENABLED" != "1" ]] && return
  local id="${ATUIN_HISTORY_ID:-}"
  [[ -z "$id" ]] && { _atuout_command=""; return; }
  # detached, disowned — never blocks the prompt (Option D fast path)
  atuout harvest "$id" --command "$_atuout_command" --exit-code "$exit_code" &>/dev/null &!
  _atuout_command=""
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec _atuout_preexec
add-zsh-hook precmd  _atuout_precmd
```
Notes:
- `&!` = background + disown in one step (zsh) — the idiom replacing today's `&` + tracked pid.
- Pass `--command`/`--exit-code` so the store can populate those columns (§4 note).
- `return 1` on the pty-proxy check requires `init-zsh` output to be `eval`'d at top level (it is).

---

## 10. CLI surface (Phase 4)

**Rewrite: `src/atuout/cli.py`.** Subcommands:

- **`harvest <atuin_id> [--command STR] [--exit-code N]`** — new. Calls `harvest()`. Always exits
  0 (detached; never disturb the shell). Logs outcome.
- **`list [--limit N]`** — reads `store.list_recordings()` (DB, not `.cast` glob). Same print shape.
- **`show <atuin_id>`** — **signature change**: now takes an **atuin id**, not a `.cast` path.
  `store.get_recording(id)`; print header + `--- output ---` + `rec.output`. (Old `cast_file`
  positional is gone. Flag this as a deliberate breaking CLI change — matches the new model.)
- **`init-zsh`** — prints `shell/atuout.zsh`. **Fix the path resolution**: today it walks
  `parent.parent.parent.parent/shell` (source-tree only). Change to prefer
  `importlib.resources.files("atuout").joinpath("shell/atuout.zsh")` (requires shipping the zsh
  file as package data — §1), falling back to the source-tree path for `-e` installs.
- **`reconcile {ensure|status|stop|restart|--daemonize}`** — new (§7). `ensure` is what the hook
  calls; `--daemonize` is the actual long-running entrypoint; `status`/`stop`/`restart` for humans.
- **`status`** — new, recommended: prints daemon reachability (try `command_output` on a bogus id
  or add a cheap ping), `[daemon].enabled` from atuin config, reconciler alive?, DB row count,
  last harvest ts, count of recent fast-path misses (from log). This is the user-facing surface
  for the "found=False is a real error" signal.
- **`record`** — **removed** entirely (and `cmd_record`, `recorder.record_command`).

**Delete: `src/atuout/recorder.py`** — `record_command` is gone; `list_recordings` moves to
`store.py` (DB-backed). Remove the file (or reduce it to a thin re-export shim if other code
imports it — prefer clean removal + update imports).

---

## 11. Test plan (Phase 5)

The existing tests assume asciinema/`.cast` and need **real rework**, not patching.

**Delete/replace:**
- `tests/test_recorder.py` — deleted (module gone). Replace with `tests/test_store.py`:
  round-trip `upsert_recording`/`get_recording`/`list_recordings`, `INSERT OR IGNORE` idempotency,
  WAL + `busy_timeout` don't error, migration creates schema v1, `ATUOUT_DB_PATH` override.
- `tests/test_recording.py` — rewrite: drop cast-parsing tests; test `Recording.from_row` and
  `from_reply`, `output`/`output_lines`/`success`/`exit_code`/`__str__` under the new fields.
- `tests/test_cli.py` — rewrite: `list`/`show` against a seeded temp DB; `show <id>` (not path);
  `record` subcommand removed → assert it errors; `init-zsh` prints hook text and the pty-proxy
  guard line is present.

**Add:**
- `tests/test_daemon_client.py` — spin up an in-process gRPC **fake** `Semantic`/`History` server
  bound to a temp UDS (grpcio server + our generated servicer stubs), assert `command_output`
  round-trips `found/output/total_*`, empty-ranges path, and error mapping
  (`UNAVAILABLE→DaemonError(kind="unavailable")`, connect-fail on missing socket, `UNIMPLEMENTED`).
  Also test `tail_history` yields `ENDED` replies from the fake.
- `tests/test_harvest.py` — with the fake daemon: `found` on first try persists;
  retry-then-succeed; exhaustion returns None and logs; daemon-down doesn't raise. Use tiny
  `attempts`/`delay_ms` via env so tests are fast.
- `tests/test_reconciler.py` — single-instance lock: second `run` exits; pidfile written/removed;
  fake `TailHistory` stream of `ENDED` events backfills only missing ids (`has_recording` skip);
  `command`/`exit_code` pulled from `HistoryEntry`. Test the `ensure` decision (lock held → no
  spawn) by mocking the spawn.
- `tests/test_settings.py` — `daemon_socket_path()` resolution order: env override, config.toml
  parse, default; `$ATUIN_CONFIG_DIR` honored; `[daemon].enabled` read.
- `tests/conftest.py` — fixtures: `temp_db`, `fake_daemon` (context manager returning socket path +
  handles to push captures/history events), env-var isolation.

The **fake daemon** is the linchpin — build it once (`tests/support/fake_daemon.py`) implementing
`SemanticServicer.CommandOutput`/`RecordCommands` and `HistoryServicer.TailHistory` over a temp
UDS, so harvest/reconciler/client tests all reuse it. No real `atuin` needed in CI.

**mypy/ruff:** exclude `src/atuout/_proto/*` from strict mypy (§1); keep everything else strict.

---

## 12. Commit / PR sequencing

Incremental landing **is** worth it — each step is independently reviewable and the risky
shell-hook swap comes last. Suggested order (each a commit; group into 1–2 PRs):

1. **`feat: vendor protos + grpcio codegen`** — add `_proto/` (protos + generated), `gen_proto.sh`,
   pyproject grpcio/protobuf deps, mypy exclude. No behavior change. (Phase 1a)
2. **`feat: daemon gRPC client`** — `daemon_client.py`, `settings.py` (socket resolution),
   `tests/test_daemon_client.py` + `tests/support/fake_daemon.py`. (Phase 1b)
3. **`feat: SQLite store + Recording rework`** — `store.py`, rewrite `recording.py`,
   `test_store.py`, `test_recording.py`. Old `.cast` path still present but unused. (Phase 2)
4. **`feat: harvester + harvest CLI`** — `harvest.py`, `cli.py harvest`, `test_harvest.py`. Not
   yet wired into the shell. (Phase 3a) — *at this point everything works when invoked by hand.*
5. **`feat: reconciler + reconcile CLI`** — `reconciler.py`, `log.py`, `cli.py reconcile/status`,
   `test_reconciler.py`. Still not wired into the shell. (Phase 3b)
6. **`feat!: swap shell hook to native capture; remove asciinema/record`** — rewrite
   `shell/atuout.zsh`, remove `record` + `recorder.py`, rewrite `test_cli.py`, fix `init-zsh` path,
   update `README`. **This is the breaking cutover** and lands only after 1–5 are green. (Phase 3/4)

Rationale for last-place cutover: steps 1–5 add the new machinery without touching the running
shell integration, so the repo stays usable (old asciinema hook still works) until the final
commit flips to the native model in one reviewable diff.

---

## 13. Things to verify against a live atuin before/while coding (do not guess)

1. **⚠️ Default `daemon.socket_path`** and exact `[daemon]` config key names/nesting
   (`enabled`, `socket_path`, `systemd_socket`, `tcp_port`) — read live
   `atuin-client/src/settings.rs`, or run `atuin` and inspect
   `~/.config/atuin/config.toml` + `ls ~/.local/share/atuin/*.sock`. §0.2.
2. **⚠️ Whether the daemon is even enabled by default** — capture requires `daemon.enabled=true`
   and an atuin built with `daemon`+`pty-proxy` features (`ANALYSIS.md`). `atuout status` should
   detect+report this; `harvest` should map `UNIMPLEMENTED`/connect-fail to a clear message.
3. **⚠️ grpcio UDS target syntax** on the target platform — confirm `unix:/abs/path` connects to
   the tonic UDS server (vs. `unix-abstract:` / `unix://`). Smoke-test against a running daemon.
4. **`atuin pty-proxy init zsh` output** — confirm it sets `ATUIN_PTY_PROXY_ACTIVE=1` in the
   wrapped shell env (runtime.rs:42 confirms the wrapper sets it; confirm it survives into
   interactive zsh so the §9 guard is reliable) and confirm the two-line setup order.
5. **`HistoryEntry.exit` semantics** — is `-1`/absent used for "not yet ended"? Only trust
   `exit` on `ENDED` events (we already gate on `kind==ENDED`). Confirm `command` is populated on
   `ENDED`.
6. **Proto drift** — `semantic.proto`/`history.proto` are internal/unversioned. Pin the vendored
   copies; add a note in `_proto/README` recording the atuin commit
   (`3f08db6b84bd2ff151d9e6560bb057dd55e3bc53`) they came from. Re-verify on atuin upgrades.

---

## 14. Open questions to confirm with the user

- **`Recording.duration`**: no timing exists in `CommandOutputReply`. Keep it as always-`0.0`
  (compat shim) or drop from the public API? (§5) — recommend keep-as-0.0 to avoid breaking callers.
- **`prompt` column**: `CommandOutputReply` has no prompt and `HistoryEntry` has none either; the
  prompt only exists in `CommandCapture` (write side, not readable back). So `prompt` will always
  be NULL under the current daemon API. Keep the column (future-proofing) or drop it? — recommend
  keep, documented as reserved.
- **Log/state dir**: `~/.local/state/atuout/` vs under the data dir — confirm preference.
