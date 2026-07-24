# atuout native refactor — analysis of atuin PR #3510 (`atuin_output`)

Source: https://github.com/atuinsh/atuin/pull/3510
Merge-relevant commit pulled for reference: `3f08db6b84bd2ff151d9e6560bb057dd55e3bc53` (branch tip at time of research, 2026-07-23).

Reference files pulled into `reference/atuin-src/` (raw source at the above commit) and
`reference/pr-3510-full.diff` (full unified diff of the PR as merged/open).

## What the PR actually adds

Atuin gained a **built-in** command output capture pipeline, replacing the need for any
external recorder (like atuout's asciinema wrapper) *if* you run your shell through Atuin's
own PTY proxy:

1. **`atuin-pty-proxy`** (`atuin pty-proxy`, aka `hex`) now watches OSC 133 sequences
   (`crates/atuin-pty-proxy/src/{capture,osc133,pty_proxy,runtime,screen}.rs`). It:
   - Tracks prompt (`A`), input-start (`B`), command-executed (`C`), command-finished (`D`)
     markers per OSC 133.
   - Replays/cleans the terminal output between `C` and `D` for a single command into plain
     text (`capture.rs`).
   - Forwards a `CommandCapture { prompt, command, output, exit_code, history_id, session_id }`
     to the daemon via a new `Semantic` gRPC service, batched (`atuin/src/command/mod.rs`,
     `semantic_command_capture_sink`).
   - Is skipped entirely if `ATUIN_TERMINAL` env var is truthy (i.e. a terminal that already
     natively supports OSC 133, so it won't double-capture).

2. **`atuin-daemon`** gained a `semantic` component
   (`crates/atuin-daemon/src/components/semantic.rs`) that:
   - Accepts a stream of `CommandCapture`s via `RecordCommands` RPC and stores them in a
     bounded in-memory ring (`MAX_RECORDS = 512`, per-session caps, LRU eviction — see PR
     description "Bound in-memory command output captures" commit).
   - Associates each capture with a matching `History` record (matched via `HistoryEnded`
     daemon event, on command text + timing — **not** a persistent join; entirely in-memory,
     lost on daemon restart).
   - Exposes `CommandOutput(history_id, ranges: Vec<OutputRange{start,end}>) -> CommandOutputReply
     {found, output, total_bytes, total_lines, lines: Vec<OutputLine{line_number, content}>}`
     over gRPC (`crates/atuin-daemon/proto/semantic.proto`).

3. **`atuin-ai`** (the TUI/AI assistant) gained an `atuin_output` tool
   (`crates/atuin-ai/src/tools/mod.rs`, `AtuinOutputToolCall`) that calls
   `SemanticClient::command_output` and formats the result for the LLM with line numbers
   (`format_output_lines_for_llm`, similar to how file-read tool output is formatted). This
   tool is only advertised when the daemon feature is compiled in and `daemon.enabled = true`
   (`docs/docs/ai/settings.md` mentions `enable_history_output`-style capability toggle).

4. **Shell integration changes** (`atuin.bash`, `atuin.zsh`, `atuin.fish`, `atuin.nu`): each
   shell's OSC 133 hooks were extended so the `D` (command-finished) marker now carries
   metadata: `history_id=<id>;session=<session_id>` appended after the exit code, e.g. for zsh:
   ```
   printf '\033]133;D;%s;history_id=%s;session=%s\a' "$1" "$ATUIN_HISTORY_ID" "${ATUIN_SESSION:-}"
   ```
   and the `B` (input-start) marker for zsh moved into `RPROMPT` instead of `zle-line-init`,
   so it survives `zle reset-prompt` redraws.

## Key architectural facts that constrain a Python client

- **There is no new CLI subcommand.** Atuin does not expose `atuin history output <id>` or
  similar. The only consumers of `CommandOutput` are the in-process `atuin-ai` Rust tool and,
  in theory, any other gRPC client that speaks to the daemon's Unix socket.
- **Capture only happens inside `atuin pty-proxy`.** This is a *full terminal wrapper*
  (the user's whole shell session runs inside it), not a per-command wrapper. This is a much
  bigger commitment than atuout's current per-command `asciinema rec -c "<command>"` model —
  it means either:
  - (a) the user runs `atuin pty-proxy` (or their terminal sets `ATUIN_TERMINAL`) for their
    whole session, and atuout becomes a *reader* of already-captured output via the daemon's
    gRPC API, or
  - (b) atuout keeps doing its own OSC-133-based or asciinema-based capture and does **not**
    depend on this PR at all.
- **Storage is ephemeral and in-memory only** (explicitly called out as a "current
  limitation" in the PR body). Captures vanish on daemon restart, are capped at 512 records
  and per-session byte limits, and matching a capture to a history_id depends on recent
  history association — there is no durable/queryable history of past output beyond what's
  still in the daemon's ring buffer. This matters a lot for atuout, whose whole point today is
  a durable `.cast` file per command.
- **Transport is gRPC over a Unix domain socket** (`settings.daemon.socket_path`), using
  `tonic`. To speak to it from Python we'd need a generated gRPC client from
  `semantic.proto` (via `grpcio-tools` / `betterproto` / `grpclib`), not a CLI shell-out.
- **Feature-gated**: requires atuin built with `daemon` + `pty-proxy` features, and
  `daemon.enabled = true` in `config.toml`; the whole capture path is opt-in and best-effort
  (fire-and-forget `mpsc::try_send`, drops captures under backpressure).

## Implication for atuout

atuout's asciinema-based recorder and Atuin's `atuin_output` mechanism are two **independent,
non-overlapping** systems that happen to both want "give me the output of command X, keyed by
Atuin history id":

| | atuout today | atuin `atuin_output` (PR #3510) |
|---|---|---|
| Capture mechanism | `asciinema rec -c "<command>"` per command, from `preexec`/`precmd` hooks | OSC 133 watching inside a full-session PTY proxy |
| Storage | Durable `.cast` files on disk, one per command | In-memory ring buffer in the daemon, capped size, lost on restart |
| Access | Read `.cast` file directly (Python) | gRPC `CommandOutput` RPC to daemon's Unix socket |
| Requires | `asciinema` binary + zsh hooks | Running your whole shell inside `atuin pty-proxy`, `daemon.enabled=true` |
| Consumer today | atuout's own CLI/API | Atuin's own AI tool (Rust, in-process) |

A "native refactor" has two very different possible meanings, and the plan doc lays out both:

1. **Replace atuout's recording backend** with Atuin's own capture, i.e. stop shelling out to
   `asciinema` and instead read completed command output from the Atuin daemon over gRPC,
   using the `history_id` atuout already tracks. This drops the dependency on `asciinema` and
   on wrapping each command, but pulls in a new hard dependency: the user must run their shell
   through `atuin pty-proxy` (or a terminal with native OSC 133 + `ATUIN_TERMINAL` support),
   and accept that captured output is ephemeral/in-memory only.

2. **Expose atuout's own captures the way Atuin's tool expects**, i.e. make atuout's
   `.cast`-backed store queryable via something resembling the same
   `CommandOutput(history_id, ranges) -> lines w/ numbers` shape, so it could plug in as an
   alternative backend/source for a similar tool — but note Atuin's `atuin_output` tool is
   hard-wired to the daemon's gRPC client in-process; there's no plugin point for external
   output providers as of this PR.

See `PLAN.md` for a concrete recommendation and phased implementation approach.
