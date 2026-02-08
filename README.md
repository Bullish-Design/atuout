# Atuout

Shell session recorder that captures command input/output via [asciinema](https://asciinema.org/), linked to [Atuin](https://atuin.sh/) history.

## Quickstart

```bash
# Install
pip install -e ".[dev]"

# Record a command
atuout record "echo hello"

# Record with an Atuin history ID
atuout record --atuin-id abc123 "ls -la"

# List recordings
atuout list

# Show output from a recording
atuout show ~/.local/share/atuout/recordings/1700000000.cast
```

## Zsh Integration

Add to your `.zshrc`:

```zsh
eval "$(atuout init-zsh)"
```

This installs `preexec`/`precmd` hooks that automatically record every command you run using asciinema. If Atuin is active, each recording is linked to its Atuin history ID.

## Python API

```python
from atuout import record_command

rec = record_command("ls -la", atuin_id="abc123")

rec.success       # True if exit code was 0
rec.exit_code     # Exit code of the recorded command
rec.output        # Full captured stdout
rec.output_lines  # stdout split into lines
rec.atuin_id      # Linked Atuin history ID
rec.duration      # Recording duration in seconds
```
