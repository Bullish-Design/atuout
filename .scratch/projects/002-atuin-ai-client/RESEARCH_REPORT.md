# Line-count contract research report

**Date:** 2026-08-20
**Target:** Agent-transcript ingestion stores the same line count as
`Recording.output_lines`.

## Evidence

`Recording.output_lines` uses `str.splitlines()` in
`src/atuout/recording.py`. Before this fix, both agent-ingest writes used
`output.count("\\n") + 1`. Those rules differ when output ends in a newline.

The focused regression test used `"first\\nsecond\\n"`. Before the fix, it
stored `total_lines == 3` while `output_lines == ["first", "second"]`.

Daemon harvesting and reconciliation use the daemon-provided line count. This
change affects only agent-transcript ingestion.

## Fix

`_output_line_count()` now returns `len(output.splitlines())`. Both
agent-ingest paths call it. The helper makes the stored count use the same
line model as the public accessor.

## Scope and limits

The change does not alter stored historic rows. It does not add range
resolution or an MCP tool. The test proves the newline-terminated regression
for direct agent ingestion. The full test suite verifies the surrounding
repository behaviour.
