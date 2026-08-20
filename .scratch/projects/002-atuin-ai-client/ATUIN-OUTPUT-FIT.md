# `atuin_output` against atuout's store — fit analysis

**Status:** analysis only, no code written
**Date:** 2026-08-20
**Inputs:** `PROTOCOL-NOTES.md`; `src/atuout/store.py`, `src/atuout/recording.py`,
`src/atuout/agent_ingest.py`, `src/atuout/harvest.py`

## The contract

From `atuin-ai-core/src/atuin_ai_core/domain/tools.gleam:342-389`. This is a
**client-side** tool, gated by the `client_v1_atuin_output` capability. The
server never executes it — the client does, and returns a `tool_result` block
on the next request.

```jsonc
{
  "history_id": "<atuin history entry id>",   // required, string
  "ranges": [[0, 100], [-200, -1]]            // optional, max 10 pairs
}
```

Specified behaviour:

- Ranges are `[start, end]`, **0-based, inclusive**. Negative indices count from
  the end; `-1` is the last line.
- Default when omitted: `[[0, 1000]]`.
- Maximum 10 ranges per call.
- Returned text is **line-numbered starting at 1, followed by a tab**, "just
  like `read_file`", explicitly so the model can "correlate it with the original
  command output and refer back to specific lines".
- `history_id` comes from `atuin_history` results or from the `last_command`
  value in the `turn_context` block.

## Column mapping

The fit is close to exact.

| Tool needs | atuout has | Notes |
| --- | --- | --- |
| lookup by `history_id` | `recordings.atuin_id` **PRIMARY KEY** | `store.get_recording(conn, history_id)`. Direct, indexed. |
| output text | `recordings.output` (`NOT NULL`) | `Recording.output` |
| line addressing | `Recording.output_lines` (`splitlines()`) | see the line-count discrepancy below |
| length for negative indices | `recordings.total_lines` | **do not use** — see below |
| size budgeting | `recordings.total_bytes` | usable for a pre-read cheap check |
| missing-capture signal | `get_recording` returns `None` | needs a defined tool-result shape |

No schema change is required. This is a read path over data already stored.

## Range resolution — the off-by-one that will bite

Atuin's `end` is **inclusive**. Python's slice end is **exclusive**. The two
coincide often enough to pass casual testing and diverge on the spec's own
"entire output" example.

```python
lines[0:-1]     # naive reading of [[0, -1]] — drops the last line
```

Verified resolver:

```python
def resolve(start: int, end: int, n: int) -> tuple[int, int] | None:
    """Atuin [start, end] (0-based, inclusive, negatives from end)
    -> Python half-open [lo, hi). None when the range selects nothing."""
    lo = start if start >= 0 else n + start
    hi = end if end >= 0 else n + end
    lo = max(0, min(lo, n))
    hi = max(-1, min(hi, n - 1))
    return (lo, hi + 1) if hi >= lo else None
```

Checked against every example in the tool description, with `n = 10`:

| Input | Result |
| --- | --- |
| `[0, -1]` entire output | all 10 lines |
| `[0, 100]` head, over-long | all 10 lines (clamped) |
| `[-200, -1]` tail, over-long | all 10 lines (clamped) |
| `[250, 275]` fully out of range | empty |
| `[0, 0]` | first line only |
| `[-1, -1]` | last line only |
| `[5, 2]` inverted | empty |

Clamping rather than erroring matches the spirit of the tool description, which
offers `[[0, 100], [-200, -1]]` as the standard "first look" on an output of
unknown length. Both ranges must survive a short output without failing.

## Line numbering: 0-based in, 1-based out

The tool takes 0-based ranges and returns 1-based line numbers. That is
inconsistent, it is what the description specifies, and an implementation must
not quietly normalise it.

The numbers must be **absolute**, not per-range. The description's stated
purpose — correlate with the original output, refer back to specific lines —
only works if a range of `[250, 275]` renders as lines `251`–`276`. Renumbering
each range from 1 would make the model's follow-up ranges wrong.

Concretely, for a resolved half-open `[lo, hi)`:

```python
"\n".join(f"{i + 1}\t{lines[i]}" for i in range(lo, hi))
```

## `total_lines` disagrees with `output_lines` — do not use it for indexing

`recordings.total_lines` is populated differently per source:

- `harvest.py:77` — `reply.total_lines`, straight from the daemon.
- `agent_ingest.py:440` and `:516` — `output.count("\n") + 1 if output else 0`.

`Recording.output_lines` uses `str.splitlines()`. For any output ending in a
newline — which is nearly all command output — the agent-home computation is
**one higher** than `len(output_lines)`:

| Output | `count("\n") + 1` | `len(splitlines())` |
| --- | --- | --- |
| `"a\nb\n"` | 3 | 2 |
| `"a\nb"` | 2 | 2 |

So for `source="agent-home"` records, `total_lines` is systematically off by
one. Resolving `[-1, -1]` against `total_lines` would return an empty range or
the wrong line.

**Rule: resolve negative indices against `len(recording.output_lines)`.** Treat
`total_lines` as a display statistic only. Whether the daemon's `total_lines`
agrees with `splitlines()` is unverified and does not matter if the rule holds.

This is a pre-existing inconsistency in atuout, not something the tool
introduces. It is only latent today because nothing indexes by line. Worth
fixing at the source regardless; the two paths should agree.

## Gaps the protocol does not specify

1. **Multi-range separators.** The tool permits `[[0, 50], [250, 275], [-100, -1]]`
   in one call and says nothing about how the gaps are marked. This is
   client-side rendering convention, invisible to the server, so the source read
   cannot settle it. Atuin's own CLI sets the de-facto standard — that is a
   question for `atuinsh/atuin`, not the AI repos. Until then, an explicit
   elision marker between non-contiguous ranges is the safe choice; silent
   concatenation would let the model read line 50 and line 250 as adjacent.

2. **Missing capture.** atuout's coverage is partial by design: the fast path
   can miss, the reconciler backfills, `agent-home` covers only agent-run
   commands. A `history_id` with no stored output is normal, not exceptional.
   The honest response is a successful tool result whose content says no output
   is stored for that id, letting the model fall back to `atuin_history`. Using
   `is_error: true` would be misleading — nothing failed.

3. **Byte budget.** Nothing caps the total volume a call can return. Ten ranges
   of `[0, -1]` against a large build log would blow the server's 180,000-token
   conversation cap in one tool result. The default `[[0, 1000]]` is already
   ~1000 lines. atuout stores `total_bytes`, so a cap plus an explicit
   truncation notice is cheap and should be non-optional.

4. **No discovery path.** atuout has no `atuin_history` equivalent — no
   full-text search over commands; `list_recordings` is time-ordered only. In
   the Atuin protocol this is fine: `atuin_history` is Atuin's job, and atuout
   serves `atuin_output` alone. **In an MCP context it is not** — with no Atuin
   client in the loop, an agent has no way to obtain a `history_id`. The MCP
   path therefore needs a search tool that the Atuin path does not. That is the
   one place the two delivery paths genuinely diverge.

## Shared work between the two delivery paths

Most of the implementation is common:

| Piece | MCP path | Atuin SDK path |
| --- | --- | --- |
| range resolution + clamping | shared | shared |
| absolute 1-based line rendering | shared | shared |
| byte cap + truncation notice | shared | shared |
| missing-capture result | shared | shared |
| untrusted-content delimiting | shared | shared |
| command search / discovery | **required** | not needed (Atuin serves it) |
| capability declaration, request loop, SSE | — | required |

The divergence is small and lands at the edges. Building the MCP path first
does not strand work; it produces the resolver, the renderer, and the safety
framing that the SDK path would need anyway.

## Untrusted content

Restating the threat from `CONCEPT_REPORT.md` because this is the surface where
it becomes concrete: **everything returned by this tool is attacker-controllable
text.** A `curl` in the user's history put remote bytes into the store; this tool
hands them to a model as context, potentially months later.

The 1-based line-number prefix helps slightly — it gives every line a consistent
frame that arbitrary content cannot forge without the numbering breaking — but
it is not containment. The returned block needs an explicit delimiter and a
statement that its content is recorded command output, to be treated as data
and never as instruction.

## Assessment

The contract fits atuout's existing schema with no migration and no new
capture work. The real content is three things, none of them large:

1. a correct inclusive/exclusive range resolver (written and checked above);
2. absolute 1-based rendering, plus a byte cap and truncation notice;
3. deciding the multi-range separator, which needs a look at Atuin's own client.

The one genuine defect this surfaced is atuout's own: `total_lines` disagrees
with `len(output_lines)` for `agent-home` records. That is worth fixing
independently of whether either delivery path gets built.
