"""Resolve and render Atuin command-output line ranges."""

from __future__ import annotations

from collections.abc import Sequence

DEFAULT_RANGES: tuple[tuple[int, int], ...] = ((0, 1000),)
MAX_RANGES = 10


def resolve_range(start: int, end: int, line_count: int) -> tuple[int, int] | None:
    """Convert one inclusive Atuin range to a Python half-open range.

    Atuin indices are zero-based. Negative values count from the output end.
    The returned bounds are clamped to the available lines.
    """
    lower = start if start >= 0 else line_count + start
    upper = end if end >= 0 else line_count + end
    lower = max(0, min(lower, line_count))
    upper = max(-1, min(upper, line_count - 1))
    if upper < lower:
        return None
    return lower, upper + 1


def resolve_ranges(
    ranges: Sequence[tuple[int, int]] | None, line_count: int
) -> list[tuple[int, int]]:
    """Resolve requested Atuin ranges, dropping ranges that select no lines."""
    requested = DEFAULT_RANGES if ranges is None else ranges
    if len(requested) > MAX_RANGES:
        raise ValueError(f"At most {MAX_RANGES} output ranges are allowed")

    resolved: list[tuple[int, int]] = []
    for start, end in requested:
        selected = resolve_range(start, end, line_count)
        if selected is not None:
            resolved.append(selected)
    return resolved


def render_ranges(lines: Sequence[str], ranges: Sequence[tuple[int, int]]) -> str:
    """Render resolved ranges with absolute one-based line numbers."""
    rendered: list[str] = []
    previous_end: int | None = None
    for start, end in ranges:
        if previous_end is not None and start > previous_end:
            rendered.append("[... output lines omitted ...]")
        rendered.extend(f"{index + 1}\t{lines[index]}" for index in range(start, end))
        previous_end = end
    return "\n".join(rendered)
