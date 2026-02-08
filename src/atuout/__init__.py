"""Atuout — shell session recorder that captures command I/O via asciinema, linked to Atuin history."""

from atuout.recorder import record_command
from atuout.recording import Recording

__all__ = ["record_command", "Recording"]
