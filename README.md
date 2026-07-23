# Atuout

Durable archiver for [Atuin](https://atuin.sh/)'s native command-output captures. Atuin's
`atuin pty-proxy` captures each command's output (via OSC 133) into an ephemeral, in-memory
daemon buffer; atuout **harvests** those captures over gRPC into its own SQLite store, keyed by
`ATUIN_HISTORY_ID`, so they survive after Atuin's ring buffer evicts them.

## Requirements

atuout **requires** your shell to run inside `atuin pty-proxy` — there is no fallback. Atuin must
be built with the `daemon` + `pty-proxy` features and have `daemon.enabled = true`.

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

## CLI

```bash
atuout list                 # list stored recordings (newest first)
atuout show <atuin_id>      # show a stored recording by Atuin history id
atuout status               # daemon/reconciler/store health
atuout reconcile status     # background reconciler state (also: ensure/stop/restart)
atuout harvest <atuin_id>   # fetch+store one capture (normally called by the hook)
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
