# Atuout

Durable archiver for [Atuin](https://atuin.sh/)'s native command-output captures. Atuin's
`atuin pty-proxy` captures each command's output (via OSC 133) into an ephemeral, in-memory
daemon buffer; atuout **harvests** those captures over gRPC into its own SQLite store, keyed by
`ATUIN_HISTORY_ID`, so they survive after Atuin's ring buffer evicts them.

## Requirements

atuout **requires** your shell to run inside `atuin pty-proxy` — there is no fallback. Atuin must
be built with the `daemon` + `pty-proxy` features, have `daemon.enabled = true`, and be new
enough to include the command-output capture service (atuin PR #3510, i.e. **≥ 18.18.0-beta.2**).

atuout talks to whatever atuin daemon is at `daemon.socket_path`, so on NixOS it integrates with
your **system** atuin. It doesn't compare version strings; instead it probes the daemon's
capability. `atuout status` reports it (`capture: yes/no`), and `init-zsh` runs a one-time
background `atuout check` that prints a warning if your atuin is too old to capture output.

## Quickstart

```bash
pip install -e ".[dev]"
```

Add to your `.zshrc`, in this order (pty-proxy first — it wraps the shell):

```zsh
eval "$(atuin pty-proxy init zsh)"   # must come first
eval "$(atuout init-zsh)"            # harvests captures via the daemon
```

`atuout init-zsh` installs `preexec`/`precmd` hooks that fire a detached `atuout harvest
<history_id>` after each command (never blocking your prompt) and start a background reconciler
that backfills any capture the fast path misses.

## Agent commands

Commands an AI agent runs never reach the pty-proxy, so the daemon holds no capture for them.
Atuin's `atuin hook install <agent>` records them as metadata-only history entries (command, exit
code, `author`). atuout recovers their output from the agent's own session transcript:

| Agent | `author` | Transcript |
| --- | --- | --- |
| pi | `pi` | `~/.pi/agent/sessions/**/*.jsonl` |
| Claude Code | `claude-code` | `~/.claude/projects/**/*.jsonl` |
| Codex | `codex` | `~/.codex/sessions/**/*.jsonl` |

An agent writes the result to its transcript *after* the atuin hook records the command, so the
first lookup usually misses. The reconciler queues each miss and retries it on a backoff, then
sweeps recent history every 15 minutes as a final net. Use `atuout ingest-agent` to backfill by
hand.

**Codex setup.** Codex names its shell tool `exec`, not `Bash`, so the matcher that
`atuin hook install codex` writes never fires. Set the matcher in `~/.codex/hooks.json` to
`^(exec|exec_command|shell|Bash)$`. Codex gates every hook on a `trusted_hash` recorded in
`~/.codex/config.toml`; after any edit to `hooks.json`, start `codex` interactively once and
accept the "Hooks need review" prompt, or the hook stays silently disabled.

## CLI

```bash
atuout list                 # list stored recordings (newest first)
atuout show <atuin_id>      # show a stored recording by Atuin history id
atuout status               # daemon/reconciler/store health
atuout reconcile status     # background reconciler state (also: ensure/stop/restart)
atuout harvest <atuin_id>   # fetch+store one capture (normally called by the hook)
atuout ingest-agent         # backfill agent command output from session transcripts
                            #   --agent pi|claude-code|codex   --since-hours N   --dry-run
```

## Python API

```python
from atuout import store
from atuout.recording import Recording

conn = store.connect()
rec = store.get_recording(conn, "abc123")   # -> Recording | None

rec.success       # True if exit code was 0
rec.exit_code     # Exit code of the command
rec.output        # Full captured output
rec.output_lines  # Output split into lines
rec.atuin_id      # Linked Atuin history ID
```
