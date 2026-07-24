I want a detailed, step-by-step implementation guide for a refactor of `atuout` (this repo).
All research and design decisions are already done — read them first, don't re-derive them:

- `.scratch/projects/001-native-refactor/ANALYSIS.md` — how atuin's `atuin_output` capture
  mechanism (atuin PR #3510) works internally.
- `.scratch/projects/001-native-refactor/PLAN.md` — the finalized design for atuout.
- `.scratch/projects/001-native-refactor/RACE-WINDOW-OPTIONS.md` — tradeoff analysis for the
  chosen concurrency design (Option D).
- `.scratch/projects/001-native-refactor/reference/atuin-src/` — vendored atuin source
  (`semantic.proto`, `history.proto`, the daemon's semantic component, the daemon client, the
  pty-proxy capture/osc133/runtime modules, the `atuin_output` AI tool implementation, all shell
  integration scripts) and `pr-3510-full.diff` (the full PR diff), pulled from the atuin repo at
  commit `3f08db6b84bd2ff151d9e6560bb057dd55e3bc53`.

## What atuout is today (context, also read the live source, don't just take this summary)

A shell session recorder (`src/atuout/{cli,recorder,recording}.py`, `shell/atuout.zsh`) that
spawns `asciinema rec -c "<command>"` per command from zsh `preexec`/`precmd` hooks, storing a
durable `.cast` file per command, filename-keyed by Atuin's `ATUIN_HISTORY_ID`. The `Recording`
dataclass lazily parses the asciicast file to expose `output`, `output_lines`, `exit_code`,
`success`, `duration`, `atuin_id`.

## What it must become (the decided design — do not re-litigate these)

1. **Drop `asciinema` entirely.** No more spawning a recorder. Atuin's own `atuin pty-proxy`
   does the capturing (by watching OSC 133 markers); atuout's job becomes *harvesting* those
   captures out of Atuin's ephemeral in-memory daemon buffer into atuout's own durable storage.
2. **`atuin pty-proxy` is a hard, non-negotiable prerequisite.** No fallback path, no dual-mode.
   `atuout init-zsh` must fail loudly at shell-startup if `ATUIN_PTY_PROXY_ACTIVE` isn't set in
   the environment, rather than silently installing hooks that would only ever no-op.
3. **Storage: SQLite**, unbounded retention (no TTL/cap/pruning — matches today's "keep
   everything forever" behavior). Schema sketch is in `PLAN.md` Phase 2.
4. **Fetching a capture: gRPC to Atuin's daemon**, over its Unix domain socket
   (`settings.daemon.socket_path`), using the vendored `semantic.proto`'s `CommandOutput` RPC
   (point lookup by `history_id`, no subscribe/tail available on this service).
5. **Race-window handling: Option D**, as detailed in `RACE-WINDOW-OPTIONS.md`:
   - **Fast path** — `precmd` spawns a detached (`&`, disowned) `atuout harvest <atuin_id>`
     that retries `CommandOutput` a few times with a short backoff (absorbing the
     pty-proxy→daemon batching delay of up to ~25ms/64-item windows) before giving up. Never
     blocks the prompt.
   - **Safety net** — a separate, long-lived reconciler process (single system-wide instance,
     pidfile-guarded, lazily started from `init-zsh`) that opens the vendored `history.proto`'s
     `TailHistory()` streaming RPC (a *different* daemon service than `Semantic`, genuinely
     supports streaming) and, on every history `ENDED` event, checks if that id is already
     harvested; if not, retries `CommandOutput` itself with more patience, since it isn't
     blocking anything.
6. `Recording`'s public API (`output`, `output_lines`, `exit_code`, `success`, `atuin_id`, etc.)
   must stay stable — only its construction path changes (from a DB row / `CommandOutputReply`
   instead of parsing a `.cast` file).
7. `atuout record <command>` is removed (no per-command recording concept survives — capture is
   now purely a side effect of running inside `atuin pty-proxy`).
8. New required two-line shell setup (proxy first):
   ```zsh
   eval "$(atuin pty-proxy init zsh)"
   eval "$(atuout init-zsh)"
   ```

## What I want from you in this session

Produce a **detailed, step-by-step implementation guide** — not necessarily writing all the
code yet, but a concrete, ordered plan I can execute (or hand to you to execute) with enough
specificity that no design decisions are left implicit. For each step, include:

- Exact files to add/modify/delete.
- The gRPC/proto tooling choice for Python (e.g. `grpcio` + `grpcio-tools` codegen vs.
  `grpclib`/`betterproto`) and how the two vendored `.proto` files get compiled and where the
  generated code lives/is checked in vs. generated at build time.
- How to determine Atuin's daemon socket path from Python (mirror
  `atuin-client/src/settings.rs`'s default/config-driven `daemon.socket_path` — check the actual
  default in the vendored reference or by reading atuin's current source if not already covered
  here).
- The reconciler process's lifecycle mechanics in concrete terms: how it's spawned, the
  pidfile/lock format and location, how `init-zsh` decides whether to (re)start it, how it shuts
  down/is restarted, and how failures are surfaced to the user (logs, `atuout status`, etc.).
- Exact SQLite schema (finalize the sketch in `PLAN.md`), migration approach (there is no
  existing production data to migrate, so this can be a fresh schema — confirm no migration
  path is needed from the old `.cast`/`.meta` files).
- Updated `pyproject.toml` (drop `asciinema`, add whatever gRPC dependency is chosen).
- Updated `shell/atuout.zsh` (remove the asciinema `preexec` recording call and the `.meta`
  sidecar logic; add the `ATUIN_PTY_PROXY_ACTIVE` startup check; add the detached
  `atuout harvest` call in `precmd`).
- Updated `cli.py` subcommands (`harvest`, `list`, `show`, `init-zsh`, whatever reconciler
  management command is chosen; remove `record`).
- Test plan: what in `tests/` needs to change/be added (note the existing tests
  `test_cli.py`/`test_recorder.py`/`test_recording.py` currently assume the asciinema/`.cast`
  model and will need real rework, not just patching).
- A suggested commit/PR sequencing (e.g. land the gRPC client + SQLite store first behind a
  flag, then swap the shell hook, then remove asciinema, then add the reconciler) if you think
  incremental landing makes sense, or a rationale for why it doesn't.

Flag anywhere the vendored reference material is ambiguous or insufficient (e.g. exact daemon
socket path default, exact `daemon.enabled` config key nesting) and note what to verify against
a live `atuin` checkout or `atuin --version`/`atuin daemon` runtime output before writing code
that depends on it, rather than guessing.

Do not re-open the settled decisions (SQLite, unbounded retention, no asciinema fallback,
Option D) — treat them as fixed requirements to plan *around*, not choices to re-evaluate.
