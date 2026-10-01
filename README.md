# Atuout

atuout stores command output recovered from AI-agent session transcripts and links it to Atuin
history IDs. Interactive terminal output belongs to Atuin 18.22.0 and later; pytuin reads that
output through `atuin output search --style json`. atuout does not connect to the Atuin daemon.

## Agent output

Atuin's agent hooks record command metadata, but the commands do not run inside `atuin pty-proxy`.
The agent tools keep their output in session transcripts, which atuout correlates with recent
agent-authored rows in Atuin's `history.db`.

| Agent | Atuin author | Transcript files |
| --- | --- | --- |
| pi | `pi` | `~/.pi/agent/sessions/**/*.jsonl` |
| Claude Code | `claude-code` | `~/.claude/projects/**/*.jsonl` |
| Codex | `codex` | `~/.codex/sessions/**/*.jsonl` |

Transcript output may be written after Atuin records the command. The scheduled importer scans
the last six hours every five minutes. Each pass retries unmatched entries; `INSERT OR IGNORE`
keeps repeated imports idempotent. Run a full or bounded import manually with `atuout ingest-agent`.

Codex names its shell tool `exec`, not `Bash`, so the matcher that `atuin hook install codex`
writes may not fire. In `~/.codex/hooks.json`, use the matcher
`^(exec|exec_command|shell|Bash)$`. Codex gates hooks on a trusted configuration hash; after
editing the file, start Codex interactively and accept its hook review prompt.

## CLI

```bash
atuout list                 # list agent recordings (newest first)
atuout show <atuin_id>      # read one agent recording by history ID
atuout status               # show the agent-store path and record count
atuout ingest-agent         # import agent output from transcripts
                            # --agent pi|claude-code|codex --since-hours N --dry-run
```

The `programs.atuout` Home Manager module installs atuout and enables a systemd user timer by
default. Set `programs.atuout.agentIngest.enable = false` to disable it. The timer runs a bounded
six-hour backfill; change `agentIngest.interval` or `agentIngest.lookbackHours` to tune it.

## Python API

```python
from atuout import store

conn = store.connect()
try:
    recording = store.get_recording(conn, "abc123")
    recent = store.list_recordings(conn, limit=5)
finally:
    conn.close()
```

`Recording` exposes `command`, `output`, `output_lines`, `exit_code`, `success`, `atuin_id`,
`total_bytes`, `total_lines`, `captured_at_ms`, and `source`.
