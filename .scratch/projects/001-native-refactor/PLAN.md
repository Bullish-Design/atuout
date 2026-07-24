# Implementation plan (draft — not yet approved)

## Corrected goal (per user, 2026-07-23)

atuout should **stop spawning `asciinema`** as its capture mechanism, and instead become a
**harvester** for Atuin's own native command-output capture (PR #3510): fetch each
completed capture from the Atuin daemon over gRPC and persist it into atuout's **own
database**, so it survives after Atuin's in-memory ring buffer evicts/loses it.

In other words: Atuin does the *capturing* (via `atuin pty-proxy` watching OSC 133), atuout
does the *archiving* (durable storage keyed by Atuin history id) — replacing today's asciinema
recorder with a thin harvester + local DB, while keeping the same external shape (`Recording`
objects, `atuout list`/`show`, one entry per Atuin history id).

## Why this requires eager harvesting, not lazy fetch-on-demand

From `reference/atuin-src/crates_atuin-daemon_src_components_semantic.rs`:
- Captures live in a `VecDeque` capped at `MAX_RECORDS = 512`, plus a per-session byte cap and
  session LRU (see PR commit "Bound in-memory command output captures").
- Nothing is ever written to disk by Atuin itself — a daemon restart drops everything.
- There is **no subscribe/tail RPC** for captures (unlike `HistoryClient::tail_history`).
  `semantic.proto` only has:
  - `RecordCommands` (pty-proxy → daemon, write-only, not useful to us)
  - `CommandOutput(history_id, ranges)` (point lookup by history id)

So atuout **cannot** passively stream new captures — it must actively call `CommandOutput`
for a given `history_id` soon after each command finishes, before the ring buffer evicts it
(under load, eviction could happen within ~512 commands session-wide). This makes the natural
integration point the existing `precmd` hook, immediately after Atuin has assigned/finalized
the history id for the command that just ran.

## Architecture

```
 command runs inside `atuin pty-proxy`
        │  (OSC133 capture, forwarded to daemon)
        ▼
 atuin-daemon (semantic component, in-memory ring buffer)
        │  gRPC: CommandOutput(history_id, ranges=[])
        ▼
 atuout precmd hook  ──►  atuout harvester  ──►  atuout's own DB (SQLite)
        │                                              │
        └── same ATUIN_HISTORY_ID atuout already reads ┘  (keyed join, like today)
```

## Phase 1 — gRPC client for the Semantic service

- Compile `reference/atuin-src/crates_atuin-daemon_proto_semantic.proto` (via
  `grpcio-tools`/`grpclib`) into a small generated module.
- `src/atuout/daemon_client.py`:
  - Connect over the Unix socket at Atuin's `settings.daemon.socket_path`.
  - `command_output(history_id: str) -> CommandOutputReply` (empty `ranges` = full output,
    confirmed optional by the `atuin_output_ranges_are_optional` test in
    `reference/atuin-src/crates_atuin-ai_src_tools_mod.rs`).
  - Map gRPC errors (`Unavailable`/`Unimplemented`/connect failure) to a distinguishable
    exception so the harvester can log-and-skip rather than crash the shell hook.

## Phase 2 — local database (replaces `.cast` files as the store of record)

- New `src/atuout/store.py` backed by SQLite at `~/.local/share/atuout/atuout.db` (replacing
  `DEFAULT_DATA_DIR`'s `.cast`/`.meta` file pair).
- Schema sketch:
  ```sql
  CREATE TABLE recordings (
    atuin_id     TEXT PRIMARY KEY,
    command      TEXT NOT NULL,
    prompt       TEXT,
    output       TEXT NOT NULL,
    exit_code    INTEGER,
    total_bytes  INTEGER,
    total_lines  INTEGER,
    captured_at  INTEGER NOT NULL   -- unix ms, when atuout harvested it
  );
  ```
- `Recording` (`recording.py`) gains a second construction path — from a DB row / from a
  `CommandOutputReply` — instead of only parsing a `.cast` file. Its public properties
  (`output`, `output_lines`, `exit_code`, `success`, `atuin_id`) stay the same so existing
  consumers/tests don't need to change; only backing storage changes. `cast_path` likely
  becomes optional/legacy.
- `list_recordings`/`record_command` in `recorder.py` get replaced by DB-backed
  equivalents (`list_recordings()` queries the table; there's no more "start a recording").

## Phase 3 — harvester + shell hook changes (Option D fast path)

Decision: race-window handling uses **Option D** — see `RACE-WINDOW-OPTIONS.md` for the full
tradeoff writeup. Option D = Option C's detached fast-path retry, backed by a `TailHistory`-
driven reconciler process as a safety net (Phase 3b below).

- New `src/atuout/harvest.py`: `harvest(atuin_id) -> Recording | None` — calls
  `daemon_client.command_output(atuin_id)`, and on `found=True`, `INSERT OR IGNORE`s into the
  `recordings` table, returning the persisted `Recording`. Internally retries a few times with a
  short backoff (e.g. 3 attempts, 50ms apart) before giving up, to absorb the normal
  pty-proxy → daemon batching delay (≤25ms/64-item window, see `command_mod.rs`
  `semantic_command_capture_sink`).
- `shell/atuout.zsh` rewritten:
  - Drop the `asciinema rec` call entirely from `_atuout_preexec`.
  - In `_atuout_precmd`, after getting `ATUIN_HISTORY_ID`, spawn `atuout harvest <id> &`
    detached/disowned (same backgrounding idiom the hook already uses for `asciinema rec &`
    today) — never blocks the prompt.
  - `atuout init-zsh` fails loudly at shell-startup if `ATUIN_PTY_PROXY_ACTIVE` isn't set (see
    "hard requirement" section below) rather than installing hooks that would only ever no-op.

## Phase 3b — `TailHistory` reconciler (Option D safety net)

- New `src/atuout/reconciler.py`: a small long-running process that:
  - Connects to Atuin's daemon `History` service (`reference/atuin-src/crates_atuin-daemon_proto_history.proto`)
    and opens `TailHistory()` — a genuine server-streaming RPC, distinct from the `Semantic`
    service, that pushes a `TailHistoryReply{kind, history: HistoryEntry{id, command, exit, ...}}`
    on every history `STARTED`/`ENDED` event.
  - On each `ENDED` event, checks whether `history.id` is already present in the `recordings`
    table; if not, calls `daemon_client.command_output(id)` itself (more patient backoff than the
    fast path, since it isn't blocking anything) and persists on success.
  - Runs as a single system-wide instance, guarded by a pidfile/lock (e.g.
    `~/.local/share/atuout/reconciler.pid`) so multiple terminals don't each start their own
    copy; started lazily from `atuout init-zsh` if not already running.
  - Accepted limitation (documented, not solved yet): `TailHistoryRequest` has no cursor/replay
    param — the stream is forward-only, so if the reconciler itself has downtime when a command
    finishes, that event is simply missed. Revisit only if this proves to matter in practice (see
    `RACE-WINDOW-OPTIONS.md`).
- New `daemon_client.py` method: `tail_history() -> Iterator[TailHistoryReply]` (or async
  generator), wrapping `HistoryClient::tail_history` from
  `reference/atuin-src/crates_atuin-daemon_src_client.rs`.

## Phase 4 — CLI surface

- `atuout harvest <atuin-id>` — new, does the fetch-and-persist, called from the hook.
- `atuout list` / `atuout show <atuin-id>` — now read from the DB instead of globbing `.cast`
  files.
- `atuout reconcile` (or similar) — new, runs/manages the Phase 3b reconciler process
  (start/status/stop), likely invoked implicitly by `init-zsh` rather than needing to be typed
  by hand day-to-day.
- `atuout record <command>` (today: runs asciinema directly) is **removed**. There is no
  per-command recording concept anymore — capture is entirely a side effect of running inside
  `atuin pty-proxy`; a per-command "record" verb doesn't map to that model.
- Drop the `asciinema` dependency entirely.

## Decision: `atuin pty-proxy` is a hard requirement — no fallback (confirmed by user)

atuout **requires** the shell to be running inside `atuin pty-proxy` (or an
`ATUIN_TERMINAL`-native integration that forwards to the daemon the same way). There is no
asciinema fallback, no dual-mode operation, no silent degradation path. Concretely:

- `atuout init-zsh` emits hook code that assumes `ATUIN_PTY_PROXY_ACTIVE=1` is set (the env var
  `atuin pty-proxy`'s `runtime.rs` injects into the wrapped shell). If it's absent, the hook
  should **fail loudly** at shell-startup time — e.g. print a clear one-line error ("atuout
  requires `atuin pty-proxy` — add `eval \"$(atuin pty-proxy init zsh)\"` before atuout's init
  in your `.zshrc`") — rather than silently installing hooks that will only ever no-op.
- `harvest(atuin_id)` does not need to distinguish "daemon has no capture yet" (transient, worth
  retrying) from "capture will never exist" (not in pty-proxy) — since pty-proxy is mandatory,
  every `found=False` after the retry window is a real error worth surfacing (e.g. daemon not
  running, `daemon.enabled=false`, or capture eviction), not an expected steady-state.
- No `asciinema` dependency at all — remove it from `pyproject.toml` and the README's
  quickstart.
- README/docs get rewritten around a single required setup:
  ```zsh
  eval "$(atuin pty-proxy init zsh)"   # must come first — wraps the shell
  eval "$(atuout init-zsh)"            # harvests captures via the daemon
  ```

## Decisions (confirmed by user, 2026-07-23)

1. **DB choice: SQLite.** Confirmed — local-only, single-user, personal use, no security/
   multi-tenancy concerns. No changes needed beyond the Phase 2 sketch above.
2. **Retention/growth: unbounded, keep everything.** No TTL, no row cap, no pruning job.
   Matches today's behavior (`.cast` files were never cleaned up either). `atuout.db` will grow
   monotonically with usage — acceptable per user, revisit only if it becomes a real problem
   (SQLite handles millions of rows of this shape fine; `output` blobs are the only real size
   driver, bounded per-command by whatever cap atuin-pty-proxy already applies on its side).

3. **Race-window handling: Option D** (confirmed) — Option C's detached, short-retry harvest as
   the fast path (Phase 3), backed by a `TailHistory`-driven reconciler process as a
   self-healing safety net (Phase 3b). Full tradeoff analysis in `RACE-WINDOW-OPTIONS.md`.

## Remaining open question

4. **Proto stability** — still worth flagging that `semantic.proto` *and* `history.proto` are
   internal/unversioned; pinning to a specific atuin version or vendoring both `.proto` files
   (already done in `reference/`) is the safest bet. Low-stakes, can decide at implementation
   time (vendor + pin, revisit if atuin changes either schema upstream).
