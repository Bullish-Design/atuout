"""A single harvested command capture, keyed by Atuin history id.

Backed by atuout's SQLite store (a DB row) or built directly from a daemon
``CommandOutputReply``. The public accessors (``output``, ``output_lines``,
``exit_code``, ``success``, ``atuin_id``, ``command``) are stable regardless of source.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atuout._proto import semantic_pb2


def reply_output_text(reply: semantic_pb2.CommandOutputReply) -> str:
    """Reconstruct the captured output text from a CommandOutputReply.

    The daemon returns the content in ``lines`` (line_number + content) and leaves the top-level
    ``output`` field empty, so prefer ``output`` if present but fall back to joining ``lines``.
    """
    if reply.output:
        return str(reply.output)
    return "\n".join(str(line.content) for line in reply.lines)


@dataclass
class Recording:
    """Represents one captured shell command execution."""

    command: str
    """The shell command that was executed."""

    atuin_id: str | None = None
    """The Atuin history ID this recording is associated with, if any."""

    exit_code: int | None = None
    """Exit code of the command (None when unknown)."""

    total_bytes: int | None = None
    total_lines: int | None = None
    captured_at_ms: int | None = None
    source: str | None = None

    _output: str = ""

    # ------------------------------------------------------------------
    # Construction paths
    # ------------------------------------------------------------------

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Recording:
        return cls(
            command=row["command"] if row["command"] is not None else "<unknown>",
            atuin_id=row["atuin_id"],
            exit_code=row["exit_code"],
            total_bytes=row["total_bytes"],
            total_lines=row["total_lines"],
            captured_at_ms=row["captured_at"],
            source=row["source"],
            _output=row["output"],
        )

    @classmethod
    def from_reply(
        cls,
        reply: semantic_pb2.CommandOutputReply,
        *,
        atuin_id: str,
        command: str | None = None,
        exit_code: int | None = None,
    ) -> Recording:
        return cls(
            command=command if command is not None else "<unknown>",
            atuin_id=atuin_id,
            exit_code=exit_code,
            total_bytes=reply.total_bytes,
            total_lines=reply.total_lines,
            _output=reply_output_text(reply),
        )

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def output(self) -> str:
        """Return the full captured output."""
        return self._output

    @property
    def output_lines(self) -> list[str]:
        """Return the captured output split into lines."""
        return self._output.splitlines()

    @property
    def success(self) -> bool:
        """Whether the command exited successfully (code 0). None → False."""
        return self.exit_code == 0

    @property
    def duration(self) -> float:
        """Deprecated: no timing exists under the native-capture model. Always 0.0."""
        return 0.0

    def __str__(self) -> str:
        status = "ok" if self.success else "fail"
        atuin = f" atuin={self.atuin_id}" if self.atuin_id else ""
        return f"Recording({status}{atuin} {self.command!r})"
