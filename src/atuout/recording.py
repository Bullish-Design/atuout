"""Lightweight wrapper around a single asciinema .cast recording."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Recording:
    """Represents one recorded shell command execution.

    Provides convenience helpers to inspect the recording without
    having to manually parse the asciicast v2 format.
    """

    cast_path: Path
    """Path to the ``.cast`` file on disk."""

    command: str
    """The shell command that was recorded."""

    atuin_id: str | None = None
    """The Atuin history ID this recording is associated with, if any."""

    recorder_exit_code: int | None = None
    """Exit code of the asciinema recorder process itself (not the recorded command)."""

    _header: dict | None = field(default=None, repr=False)  # type: ignore[type-arg]
    _events: list[tuple[float, str, str]] | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse(self) -> None:
        """Lazily parse the cast file into header + events."""
        if self._header is not None:
            return
        if not self.cast_path.exists():
            self._header = {}
            self._events = []
            return

        lines = self.cast_path.read_text().splitlines()
        if not lines:
            self._header = {}
            self._events = []
            return

        self._header = json.loads(lines[0])
        self._events = []
        for line in lines[1:]:
            if not line.strip():
                continue
            try:
                ts, etype, data = json.loads(line)
                self._events.append((float(ts), str(etype), str(data)))
            except (json.JSONDecodeError, ValueError):
                continue

    @property
    def header(self) -> dict:  # type: ignore[type-arg]
        """The asciicast v2 header dict."""
        self._parse()
        assert self._header is not None
        return self._header

    @property
    def events(self) -> list[tuple[float, str, str]]:
        """All events as ``(timestamp, event_type, data)`` tuples."""
        self._parse()
        assert self._events is not None
        return self._events

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def output(self) -> str:
        """Return the full captured output (``\"o\"`` events concatenated)."""
        return "".join(data for _, etype, data in self.events if etype == "o")

    @property
    def output_lines(self) -> list[str]:
        """Return the captured output split into lines."""
        return self.output.splitlines()

    @property
    def input_events(self) -> list[tuple[float, str]]:
        """Return only input (``\"i\"``) events as ``(timestamp, data)``."""
        return [(ts, data) for ts, etype, data in self.events if etype == "i"]

    @property
    def duration(self) -> float:
        """Total duration of the recording in seconds."""
        return float(self.header.get("duration", 0.0))

    @property
    def exit_code(self) -> int | None:
        """Exit code of the *recorded command* (from the cast header).

        Returns ``None`` when the cast file doesn't exist or the header
        doesn't include an exit code (older asciinema versions).
        """
        # asciinema >= 2.4 stores this as env.exit_code or in the header
        env = self.header.get("env", {})
        code = env.get("exit_code") or self.header.get("exit_code")
        if code is not None:
            return int(code)
        return None

    @property
    def success(self) -> bool:
        """Whether the recorded command exited successfully (code 0).

        Falls back to checking the asciinema recorder exit code when the
        cast file doesn't contain an explicit exit code.
        """
        code = self.exit_code
        if code is not None:
            return code == 0
        if self.recorder_exit_code is not None:
            return self.recorder_exit_code == 0
        return False

    def __str__(self) -> str:
        status = "ok" if self.success else "fail"
        atuin = f" atuin={self.atuin_id}" if self.atuin_id else ""
        return f"Recording({status}{atuin} {self.command!r})"
