from __future__ import annotations

import pytest

from atuout.output_ranges import MAX_RANGES, render_ranges, resolve_range, resolve_ranges


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ((0, -1), (0, 10)),
        ((0, 100), (0, 10)),
        ((-200, -1), (0, 10)),
        ((250, 275), None),
        ((0, 0), (0, 1)),
        ((-1, -1), (9, 10)),
        ((5, 2), None),
    ],
)
def test_resolve_range_matches_atuin_examples(
    requested: tuple[int, int], expected: tuple[int, int] | None
) -> None:
    assert resolve_range(*requested, line_count=10) == expected


def test_resolve_ranges_uses_default_and_drops_empty_ranges() -> None:
    assert resolve_ranges(None, line_count=2) == [(0, 2)]
    assert resolve_ranges([(250, 275), (-1, -1)], line_count=2) == [(1, 2)]


def test_resolve_ranges_rejects_more_than_protocol_limit() -> None:
    with pytest.raises(ValueError, match=str(MAX_RANGES)):
        resolve_ranges([(0, 0)] * (MAX_RANGES + 1), line_count=1)


def test_render_ranges_uses_absolute_line_numbers_and_marks_gaps() -> None:
    text = render_ranges(["zero", "one", "two", "three"], [(0, 1), (3, 4)])
    assert text == "1\tzero\n[... output lines omitted ...]\n4\tthree"
