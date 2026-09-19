# Background reconciler CPU regression — 2026-09-19

## Scope and acceptance criterion

Investigate the high-CPU `reconcile --daemonize` process without touching the
production database, then prove that repeated agent history events do not
reparse the transcript tree. The relevant runtime is the Python 3.13 Nix
package and the source checkout at this repository.

## Baseline evidence

The installed package was `/nix/store/95xiw6vccyc6warhs8wjrp8bndkyazmk-atuout-0.2.0`
(Python 3.13); a separate Python 3.14 store path contained the same source
shape. Its `agent_ingest.py` had:

* `ingest_entry()` call `build_index((author,))` for each agent event;
* `build_index()` call `rglob("*.jsonl")` and parse every discovered file;
* no parse cache, file signature check, or refresh interval;
* the reconciler call `ingest_entry()` directly from the history-tail loop.

The working tree already contained a partial mitigation: it filtered by
transcript mtime and added an mtime/size cache. It still walked the transcript
tree and rebuilt the command index for every event, and its cache eviction
removed entries outside the current event's narrow window. It therefore did
not address the repeated discovery/parsing workload or tail-thread latency.

The reported production shape is consistent with this complexity:

```
agent events × (recursive transcript discovery + parsing of eligible files)
```

With 58,000 agent-authored recordings and multi-gigabyte transcript trees,
this explains sustained CPU and read I/O without requiring a SQLite write
lock or database corruption.

## Implemented design

`TranscriptIndex` is a worker-owned rolling index:

* discovery is throttled to one refresh per second;
* file identity is `(st_mtime_ns, size)`, so new, appended, and replaced files
  are refreshed while unchanged files are reused;
* only the newest 512 live files and newest 4,096 calls per file are retained;
* entries older than the ten-minute live retention window are evicted;
* malformed/partially-written JSONL remains skippable, and parser failures are
  isolated to one file;
* the existing exact command and timestamp-window matching rules remain in
  place, including Pi FIFO pairing, Claude tool-id pairing, and Codex's
  supported/unsupported call shapes.

The history-tail thread now only deduplicates and bounds the retry queue. The
retry worker owns SQLite connection and transcript parsing. Duplicate history
IDs are rejected by the queue and the existing database primary key remains
the final idempotency guard. Parsing exceptions are caught at the worker
boundary, so a bad transcript cannot terminate reconciliation.

## Verification

Commands run inside `devenv shell`:

* `uv run ruff check src tests` — pass
* `uv run ty check` — pass
* `uv run pytest` — `107 passed, 1 skipped`
* `nix build .#atuout --no-link --print-build-logs` — pass

Synthetic fixture: 200 Pi transcript files and 100 matching lookups. Repeating
the old `build_index()` path five times caused 1,000 parser calls. One
`TranscriptIndex` refresh parsed 200 files and served all 100 lookups without
additional parses. Focused tests also cover unchanged-file reuse, changed/new
files, malformed JSONL, duplicate events, bounded queues, retry ingestion,
timestamp matching, and worker-side agent recovery.

## Limits and remaining operational checks

The index still performs a metadata-only recursive discovery during each
refresh period so new session files can be found without OS-specific watcher
dependencies. It does not read unchanged files. The 512-file and per-file
call limits are deliberately bounded for the live path; the explicit CLI
backfill remains a one-shot operation for historical recovery.

The package build produces a new content-addressed Nix store path, but this
session did not replace the active system/home-manager profile or restart the
production daemon. Deployment and post-restart checks are listed in the final
handoff.
