# The batching race, and how to beat it

## The problem, precisely

`atuin pty-proxy` doesn't forward each finished command to the daemon immediately. From
`reference/atuin-src/crates_atuin_src_command_mod.rs` (`semantic_command_capture_sink`):

```rust
while batch.len() < 64 {
    match rx.recv_timeout(Duration::from_millis(25)) {
        Ok(capture) => batch.push(capture),
        Err(_) => break,
    }
}
runtime.block_on(send_semantic_command_captures(&settings, batch));
```

Each `CommandCapture` sits in an mpsc queue and gets flushed to the daemon either when 64 have
piled up or 25ms passes with nothing new — then it's sent over gRPC (`RecordCommands`), parsed,
and matched against a `History` record inside `SemanticComponentInner::record_capture` /
`record_history`, which itself depends on a `HistoryEnded` daemon event arriving via the normal
history-sync path. So there are actually *two* async hops after the command finishes, in series:

1. pty-proxy's local batching window (≤25ms, or however long until 64 captures queue up).
2. The daemon receiving the gRPC call, then matching the capture to a `History` row (itself
   dependent on when Atuin's own history-write pipeline fires `HistoryEnded`).

atuout's `precmd` hook fires essentially the instant the command exits — likely *before* either
of those hops completes. A naive single `CommandOutput` call from `precmd` will frequently see
`found=false` even though the capture is (or will shortly be) there.

## Option A — Short synchronous retry loop inside `harvest()`

Call `CommandOutput`, and on `found=false`, sleep briefly and retry a fixed number of times
(e.g. 3 attempts, 50ms apart) before giving up, all inside the same `atuout harvest <id>` process
invoked from `precmd`.

**Pros**
- Simplest possible implementation — a `for` loop with a `time.sleep`, no new process
  lifecycle, no new failure modes to reason about.
- Deterministic: harvesting is fully done (success or failure) by the time `precmd` returns, so
  `atuout list`/`show` immediately after a command will always reflect the outcome — no
  eventual-consistency window visible to the user.
- Easy to test — no timing/concurrency across processes, just a function with a retry loop.
- No extra long-running process to manage, restart, or leak.

**Cons**
- **Blocks the prompt.** Every single command you run now waits up to `attempts × delay`
  (e.g. 150ms) extra before your prompt comes back, even in the success case if it takes a
  couple retries. For a shell used interactively all day, this is a real, constant latency tax
  — the exact kind of overhead atuout's current design (fire-and-forget background asciinema)
  was careful to avoid.
- Worst case (genuine failure — daemon down, eviction, etc.) always pays the *full* retry
  budget before giving up, on every single command, since there's no way to distinguish "will
  never arrive" from "still in flight" ahead of time.
- Retry budget is a guess; under system load (the 25ms/64-item batching window can stretch if
  the daemon or pty-proxy's send path is slow) a fixed budget could still not be enough,
  forcing a choice between "sometimes misses captures" and "sometimes very slow prompts."

**Implications**
- Needs careful tuning of attempts/delay against real-world observed latency — probably wants
  to be user-configurable (env var) rather than hardcoded, since it directly trades user-facing
  latency against capture completeness.
- Because it's synchronous in the hook, any daemon hiccup (e.g. temporarily unreachable) creates
  an immediately-felt slowdown in the user's actual terminal, which is a worse failure mode than
  "harvesting silently lags a bit."

**Opportunities**
- Straightforward to make the retry adaptive later (e.g. start at 10ms and back off), still
  entirely local to one function, no architecture change needed.
- Because everything resolves synchronously, `atuout harvest` can return a clean exit code the
  hook could act on (e.g. print a one-line warning on repeated failure) without needing IPC.

## Option B — Background sweeper process that backfills recent history ids

A long-running (or periodically-cron'd) atuout process independently walks Atuin's *own*
history (via `atuin-client`'s history DB/daemon, which atuout would need to read anyway to know
which ids exist) looking for recent entries not yet present in atuout's `recordings` table, and
calls `CommandOutput` for each, on some interval (e.g. every 1-2s) or triggered by Atuin's
`tail_history` streaming RPC (which *does* exist, per `HistoryClient::tail_history` in
`reference/atuin-src/crates_atuin-daemon_src_client.rs` — unlike the semantic service, the
history service supports a genuine subscribe/tail stream).

**Pros**
- **Zero added latency in the interactive path.** `precmd` doesn't need to call anything
  synchronously (or can fire-and-forget a single non-blocking attempt) — the user's prompt
  returns immediately regardless of daemon state.
- Naturally self-healing: if the daemon was briefly unreachable, restarted, or slow, the
  sweeper just catches up on its next pass — no per-command retry budget to tune, no window
  where "one particular command's capture" is permanently missed due to bad timing.
  In particular it can subscribe to `tail_history` and react to `HistoryEnded`-adjacent events
  in near-real-time rather than guessing a fixed retry count.
- Decouples "did the shell hook run" from "did we successfully harvest" entirely — the shell
  hook becomes trivially simple and fast again (closer to how the original `.zshrc` hook worked
  before this refactor).
- More robust to the exact failure mode this PR's docs call out as a known limitation:
  "captures without a history ID are ignored rather than matched by command text" — a sweeper
  polling on an interval has more opportunities to catch a capture that arrives on a delayed
  daemon-side association.

**Cons**
- **New long-running process to manage.** Needs a supervised lifecycle (start on login,
  survive across terminal sessions since it's not tied to any one shell, restart on crash,
  avoid double-starting if the user opens multiple terminals). This is meaningfully more
  operational surface than a stdlib SQLite file and a CLI subcommand.
- More moving parts to debug: now there are two independent asynchronous systems (atuin's own
  daemon, plus atuout's sweeper) each with their own timing, instead of one linear call chain.
- Introduces a genuine eventual-consistency window: right after running a command, `atuout
  show <id>` might legitimately return "not yet harvested" for a brief period, which didn't
  happen in option A (fully resolved by the time the hook returns) or in atuout's current
  behavior today.
- Need a mechanism to know "what history ids exist that we haven't harvested yet" — either
  polling Atuin's history store directly (extra dependency on `atuin-client`'s DB/daemon
  surface beyond just the semantic service) or maintaining a local queue of "pending" ids
  written by the (now much lighter) `precmd` hook for the sweeper to drain.

**Implications**
- This shifts the design from "one gRPC client used synchronously" to "a small daemon of our
  own" — closer in shape to what Atuin's own daemon does, which is a legitimate but bigger
  scope increase for what was pitched as a thin harvester.
- Because it can use `tail_history`'s streaming RPC instead of polling, the sweeper could react
  in near-real-time (much faster than a synchronous fixed retry) once a `HistoryEnded` event
  fires — that's the strongest argument for this option's latency profile actually being
  *better* than Option A's on average, not just "off the critical path."

**Opportunities**
- A background process is also the natural place to eventually add things atuout might want
  independent of this refactor — e.g. periodic compaction/vacuuming of the SQLite file, richer
  matching logic to recover captures that arrive without a clean history-id join, or exposing a
  small local status/health check (`atuout status` — "daemon reachable: yes, N pending, last
  harvest: 2s ago").
- Could subsume the "distinguish real failure from race" problem entirely: since retries happen
  on the sweeper's own schedule, indefinitely, a `found=false` from `precmd`'s own optional
  fire-and-forget attempt is *never* a final answer — real errors (daemon never reachable) only
  need to be surfaced via the sweeper's own logging, not on every command.

## Option C — Hybrid: fire-and-forget detached retry, no persistent process

`precmd` spawns a short-lived detached background process (`atuout harvest <id> &` or
`nohup`/`setsid`-style disown, similar to how the *current* zsh hook already backgrounds
`asciinema rec`) that does Option A's retry loop, but off the critical path — the shell doesn't
wait on it at all.

**Pros**
- No latency added to the prompt (matches Option B's biggest win).
- No long-running daemon to manage — each harvest attempt is a one-shot process that exits
  when done, exactly like today's `asciinema rec &` pattern the zsh hook already uses, so it's a
  very small conceptual delta from what's there today.
- Keeps the simplicity of Option A's retry logic (no sweeper, no separate "what's pending"
  bookkeeping) while removing its latency cost.

**Cons**
- Still bounded by a fixed retry budget per command (same tuning problem as Option A), just
  no longer blocking — so genuine failures still eventually give up silently in the background,
  and surfacing that failure to the user requires them to notice a missing entry later (e.g. via
  `atuout list`) rather than an immediate prompt-side warning.
- Spawns one extra short-lived process per command (small overhead, though the current design
  already does this for asciinema, so it's not a new category of cost — just the same category
  serving a different purpose now).
- Zombie/orphan process bookkeeping: needs to make sure detached processes don't pile up if the
  daemon is down for a long stretch and every command spawns one that retries for a while.

**Implications / Opportunities**
- This is the pragmatic middle ground: same code as Option A's retry loop, wrapped in the same
  "background it" idiom the zsh hook already uses today for asciinema, so it's the smallest
  behavioral and architectural delta from what's already in `shell/atuout.zsh` while still
  removing the latency problem.
- Could be a good *starting point* that's later upgraded to Option B if the fixed-retry-budget
  failure mode (missed captures under sustained daemon slowness) turns out to matter in practice —
  the harvester function itself (`harvest()`) is identical between A and C; only how it's
  invoked from the hook changes.

## Option D (chosen) — Option C + a `TailHistory`-driven reconciler

Atuin's daemon exposes a *second*, separate service — `History` (not `Semantic`) — with a real
server-streaming RPC that the semantic/output-capture service itself lacks:

```proto
// crates/atuin-daemon/proto/history.proto
service History {
  rpc TailHistory(TailHistoryRequest) returns (stream TailHistoryReply);
}
message HistoryEntry { uint64 timestamp; string id; string command; ...; int64 exit; int64 duration; }
message TailHistoryReply { HistoryEventKind kind; HistoryEntry history; }  // STARTED or ENDED
```

`HistoryClient::tail_history()` (already implemented in `reference/atuin-src/crates_atuin-daemon_src_client.rs`)
opens this stream and pushes a `TailHistoryReply` every time a history entry starts or ends —
independently of whatever the semantic/capture pipeline is doing. Every `ENDED` event is an
authoritative "this history id just finished" signal atuout can cross-reference against its own
`recordings` table.

**Design:**
- **Fast path unchanged from Option C** — `precmd` fires a detached, short-retry
  `atuout harvest <id>`, off the critical path, zero added shell latency, handles the large
  majority of commands.
- **Add one long-lived reconciler process**, started once (e.g. lazily from `atuout init-zsh`,
  guarded by a pidfile/lock so only one instance ever runs system-wide), holding a
  `TailHistory()` stream open. On every `ENDED` event, it checks whether that `id` is already in
  `recordings`; if not (fast path hasn't finished, exhausted its retries, or never ran), it makes
  its own `CommandOutput(id)` attempt — with a more patient backoff since it isn't blocking
  anything — and persists it if found.

**Why this beats plain Option C:** it turns "did the fire-and-forget retry actually work?" from
an unverified hope into a monitored, self-correcting fact. Any capture that landed in Atuin's
daemon but was missed by the fast path's fixed retry budget gets a second, unhurried chance. If
it's *still* not found after that, that's now a real, distinguishable signal (Atuin never had it
— pty-proxy inactive, capture evicted, etc.) worth surfacing, e.g. via a future `atuout status`.

**Costs/limits, accepted as-is for now:**
- Still one persistent process to manage (same category of overhead as Option B, just scoped to
  reconciliation rather than primary harvesting) — needs start-on-login, crash-restart, and
  no-duplicate-instance handling.
- `TailHistoryRequest` has no cursor/replay parameter — the stream is forward-only. If the
  reconciler itself isn't running when a command finishes (not yet started, crashed, machine
  asleep), that `ENDED` event is simply gone; there's no "catch up from where I left off." This
  closes the *timing* race between pty-proxy→daemon and `precmd`, but not "the reconciler process
  itself had downtime." A further refinement (periodically diffing against Atuin's own persisted
  history rather than only listening live) could close that gap too, but is out of scope for now
  — treated as an accepted edge case.
- Cannot recover captures Atuin genuinely never had (pty-proxy not active for that shell, or
  evicted before either attempt) — no amount of reconciliation invents data that was never
  captured in the first place.

## Recommendation (final)

**Option D.** Option C's detached, short-retry harvest stays the hot path (zero added shell
latency, minimal delta from today's "background the recorder" idiom); the `TailHistory`
reconciler is the safety net that upgrades "we hope the retry worked" into a verified,
self-healing guarantee, using an RPC Atuin already provides for exactly this kind of purpose.
Plain Option C (no reconciler) or Option B (reconciler as the *only* path, no fast path) are
both strictly worse: C alone leaves missed captures silently unrecovered, B alone reintroduces
latency-hiding tricks the fast path already solves for free.
