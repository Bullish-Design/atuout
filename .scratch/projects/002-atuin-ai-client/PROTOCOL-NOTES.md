# Atuin AI client protocol — notes from source

**Status:** derived from source, not from a live server. No spike run yet.
**Date:** 2026-08-20

## Provenance

Both repos are vendored under `reference/` (gitignored, same as `001`).

| Repo | Commit | Date | Language |
| --- | --- | --- | --- |
| `atuinsh/atuin-ai-core` | `565877e97ed3ff4840481b8d47e12eb2f9854039` | 2026-08-12 | Gleam (on BEAM) |
| `atuinsh/atuin-ai-server` | `4d582bc5ceea5b5edfdcf3abb49dc850400cda7c` | 2026-08-12 | Elixir (Plug) |

Correction to the concept report's assumption: the core is **Gleam**, not Rust.
Encoders and decoders are hand-written, so the wire shapes are explicit in
source. `http/request.gleam`, `http/streaming.gleam`, and `http/controller.gleam`
are the whole client-facing contract.

The core README's claim that event shapes "must match the Elixir `Streaming`
module until that module is deleted at cutover" (`http/streaming.gleam:1-3`)
means the wire format is **frozen by released CLI binaries**, not by choice.
That is good news for a third-party client: the server cannot break these
shapes unilaterally.

## Transport

Two routes, both under `/api/cli` (`atuin-ai-server/lib/atuin_ai/server/router.ex`).

Auth: `Authorization: Bearer <token>`, checked before parsing. If `AUTH_TOKEN`
is unset the server is fully open. Bad token → `401` with
`{"error":"unauthorized","message":"..."}`. Unknown route → `404` with
`{"error":"not_found","message":"No such route"}`.

The server parses the client version out of the `User-Agent` header by
splitting on the literal `atuin/` (`controller.gleam:866-876`). A third-party
client that wants version-conditional server behaviour must impersonate that
prefix; one that does not send it is treated as version-unknown.

## `GET /api/cli/models`

Response (`controller.gleam:63-101`):

```json
{
  "models": [
    {"alias": "fast", "name": "Display Name", "description": "..."}
  ],
  "default": "fast"
}
```

The standalone server calls this with `llm_selection_enabled: false`, so only
aliases with `visible_in_cli: true` are listed. `alias` is the value to send
back as `config.model` — never a provider model ID.

## `POST /api/cli/chat` — request body

Assembled from `controller.gleam:756-830`, `request.gleam`, `config.gleam`.

```jsonc
{
  "messages": [ /* required, non-empty */ ],
  "session_id": "<uuid>",        // optional; "" or absent → server generates uuidv7
  "invocation_id": "...",        // optional, opaque
  "context": {                   // optional, advisory only
    "os": "linux",
    "distro": "nixos",
    "shell": "zsh",
    "preferred_language": "...",
    "pwd": "/home/andrew/...",
    "last_command": "..."
  },
  "config": {                    // optional
    "capabilities": ["client_v1_atuin_output", "..."],
    "run_preference": "auto" | "suggest" | "run",
    "model": "<alias>",
    "prompt_fn": "...",
    "user_contexts": [...],
    "skills": [...],
    "skills_overflow": "..."
  },
  "capabilities": [...]          // legacy top-level; unioned with config.capabilities
}
```

Leniency is aggressive and deliberate. Every `config` field decays to its
default on a shape mismatch; a `context` that is not a map decays to empty;
unknown capability strings are dropped silently. Only two things are hard
errors: missing/empty `messages`, and a `session_id` that is present,
non-empty, and not a valid UUID.

**Size cap:** the server estimates tokens at 4 characters per token and
rejects conversations over **180,000 tokens** — roughly 720,000 characters
across all message content (`request.gleam:39-45`, `288-300`). This is a
client-relevant limit, and it is the SDK's job to fail early rather than
round-trip a 400.

### Message shape

Anthropic-style content blocks (`request.gleam:47-73`):

- `role`: `"user"` | `"assistant"` (nothing else; `"system"` is rejected)
- `content`: a plain string, **or** a list of blocks:
  - `{"type": "text", "text": "..."}`
  - `{"type": "tool_use", "id": "...", "name": "...", "input": {...}}`
  - `{"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": false}`
  - remote variant: `{"type": "tool_result", "tool_use_id": "...", "remote": true, "content_length": 1234, "is_error": false}`

`input` is arbitrary JSON, per-tool. A legacy top-level `tool_calls` array on
assistant messages is normalised into `tool_use` blocks at decode time; a new
client should not emit it.

## SSE response

Headers (`streaming.gleam:18-32`): `content-type: text/event-stream`,
`cache-control: no-cache`, `x-accel-buffering: no`, and
**`x-atuin-ai-session-id: <uuid>`**. That header is how a client learns the
server-assigned session id without waiting for `done`.

Framing is `event: <name>\ndata: <json>\n\n`. No `id:` field, no `retry:`, no
heartbeat/comment lines. **There is no SSE resume mechanism** — no Last-Event-ID
support anywhere in the source.

Six event types, and only six:

| Event | Data |
| --- | --- |
| `status` | `{"state": "processing" \| "thinking" \| "searching" \| "waiting_for_tools"}` |
| `text` | `{"content": "<delta>"}` |
| `tool_call` | `{"id": "...", "name": "...", "input": {...}}` |
| `tool_result` | `{"tool_use_id": "...", "content": "...", "is_error": false}` or remote form |
| `done` | `{"session_id": "...", "usage": {...}, "credits": {...}?}` |
| `error` | `{"message": "...", "code": "..."}` |

`usage` carries `input_tokens`, `output_tokens`, `total_tokens`,
`cached_tokens`, `cache_creation_tokens`, and nullable `input_cost`,
`output_cost`, `total_cost`, `provider_cost`.

Two loop commands exist with **no wire representation yet** —
`SendReasoningDelta` and `SendToolCallStarted` (`driver.gleam:876-878`). Tool
calls arrive only when complete; there is no partial-arguments streaming. A
client must not expect incremental tool input.

### `tool_result` events are server-side tools only

The `tool_result` event reports what the *server* executed (web search, web
scrape) — it is not an echo of client work. On the stateless OSS server the
result store always declines, so the full content is always sent **inline**
(`streaming.gleam:84-117`). The `remote: true` / `content_length` form only
appears on a deployment with persistence. An OSS-only client still needs to
parse it, but will not see it.

## The turn lifecycle — the central finding

The concept report flagged this as the one wire question that determines the
whole API. It is answered, and unambiguously.

**The server is stateless across turns. The client owns the entire
conversation. Every tool round trip is a new `POST /api/cli/chat` carrying the
full message history.** There is no side channel and no bidirectional stream.

The loop (`engine/loop.gleam:444-475`) classifies each model response
(`engine/turn.gleam:52-74`) and does one of:

| Disposition | Server behaviour | Stream |
| --- | --- | --- |
| `TextOnly` | complete | `done`, outcome `Success` |
| `FinalSuggest` | `suggest_command` is terminal | `tool_call` + `done`, outcome `Success` |
| `ServerToolsOnly` | run them, continue looping | `tool_call`, `status: searching`, `tool_result`, … |
| `NeedsClientTools` | run any server tools, then **stop** | `tool_call`(s), `status: waiting_for_tools`, `done` |
| `EmptyResponse` | retry same request | (nothing) |

`pause_for_client` (`loop.gleam:623-636`) sends `SendToolCalls`, then
`SendStatus(WaitingForTools)`, then **`SendDone`**, and finishes with outcome
`PausedForClientTools`.

So: **a `done` event does not mean the turn is over.** It means the HTTP
request is over. If the events preceding it included a `tool_call` for a tool
the server does not execute, the client must run it and start a new request
with the tool result appended. The disambiguator is `status: waiting_for_tools`
immediately before `done`, plus the pending `tool_call` ids.

Consequences for the SDK:

1. `session.stream()` cannot be a single HTTP request. It is a **loop of
   requests**, and the SDK's core job is accumulating the conversation
   correctly across them.
2. `submit_tool_result()` as a call *inside* the iteration is expressible, but
   only if the SDK buffers the results and re-POSTs when the generator is next
   advanced. That is a real design decision, not an incidental one.
3. `suggest_command` is **terminal** — it ends the turn even alongside other
   tool calls (`turn.gleam:68-72`, `has_suggest` wins). A client must not
   expect to answer a `suggest_command` as a tool.
4. Idempotency (concept-report risk #5) is **the client's problem entirely**.
   The server keeps nothing. If the connection drops after the client ran a
   side-effecting tool but before it posted the result, only the client knows.
   `session_id` is for tracing and analytics, not for resume.
5. The iteration cap is **50** server-side tool iterations
   (`loop.gleam:30`); exceeding it emits `error` with code
   `generation_failed` and message "Max tool execution limit reached".

### Cancellation

There is no cancel endpoint and no cancel message. Cancellation is
**closing the HTTP connection**. The server notices the failed write, flips a
`disconnected` flag, drains the in-flight generation for billing, then finishes
with outcome `Cancelled` (`loop.gleam:444-447`, `612-621`). Pre-stream, a
disconnect maps to status `499` / `client_disconnected`.

For the SDK this is simple and honest: cancel == drop the response. There is
nothing to confirm and nothing to await.

## Tools and capabilities

The client declares what it will execute; the server sends the model only
those tool definitions (`domain/tools.gleam:90-101`). Wire strings and their
tools (`domain/capabilities.gleam:48-61`):

| Capability string | Tool | Side effects |
| --- | --- | --- |
| `client_v1_read_file` | `read_file` | read |
| `client_v1_edit_file` | `edit_file` | **write** |
| `client_v1_write_file` | `write_file` | **write** |
| `client_v1_execute_shell_command` | `execute_shell_command` | **arbitrary** |
| `client_v1_atuin_history` | `atuin_history` | read |
| `client_v1_atuin_output` | `atuin_output` | read |
| `client_v1_load_skill` | `load_skill` | read (runs embedded shell) |
| `client_invocations` | (no tool; behavioural flag) | — |

Declared order is stable because it feeds the model's cached prompt prefix.

`suggest_command` is always present and is **server-owned structured output**,
not a client tool. Its input: `command`, `description`, `confidence`
(`high`/`med`/`low`), `danger` (`high`/`med`/`low`), and optional
`confidence_notes` / `danger_notes`.

### `atuin_output` — directly relevant to atuout

This is the finding that most changes the concept report. The protocol
**already specifies** a command-output lookup tool
(`domain/tools.gleam:342-389`):

```jsonc
{
  "history_id": "<atuin history entry id>",
  "ranges": [[0, 100], [-200, -1]]      // optional
}
```

- `ranges` are `[start, end]`, **0-based, inclusive**, negatives count from the
  end. Max 10 ranges per call. Default `[[0, 1000]]`.
- The return is expected to be line-numbered from 1, tab-separated, like
  `read_file`.
- `history_id` comes from `atuin_history` results or from the `last_command`
  value in the `turn_context` block.

atuout's store is keyed by `ATUIN_HISTORY_ID` and already exposes
`Recording.output_lines`. The contract fits the existing data almost exactly —
the only work is range resolution and line-number formatting.

Note `turn_context` is built server-side from the client's `context.last_command`
(`domain/prompt.gleam:291-304`) and is a **free-form string capped at 300
characters**. The convention that it embeds a History ID is a client-side
formatting choice, not something the server parses or validates.

### Server-side safety escalation

`suggest_command` input passes through a server keyword scan before it reaches
the client (`streaming.gleam:64-82`, `161-207`). If the scan flags the command,
the server overwrites `danger` to `"high"` and appends
`"[Server Warning] ..."` to `danger_notes`.

This does not weaken the concept report's posture — the report's rule ("danger
is UI metadata, never an authorization result") still holds. But it adds a
concrete detail: the `danger` field a client receives is **not** purely the
model's self-assessment, and a client that renders `danger_notes` should expect
the `[Server Warning]` marker.

## Errors

Pre-stream errors are plain JSON with an HTTP status. Post-stream errors are
`error` SSE events on a `200` response — the status is already committed.

| Condition | HTTP | `code` |
| --- | --- | --- |
| bad `session_id` | 400 | `invalid_request` |
| missing `messages` | 400 | `invalid_request` |
| over 180k tokens | 400 | `conversation_too_large` |
| unknown model alias | 400 | `invalid_request` |
| disabled | 403 | `feature_disabled` |
| rate limited | 429 | `rate_limit_exceeded` |
| client disconnected | 499 | `client_disconnected` |
| internal | 500 | `internal_error` |
| iteration cap hit | (200, in-stream) | `generation_failed` |

Rate-limit responses add a `retry-after` header and `limit` / `used` fields.
The stateless OSS server has no limiter configured, so 403/429 should not
appear there.

The default `code` when the server sends `error` without one is
`internal_error` (`streaming.gleam:155`).

## What still needs a live server

Source reading settled more than expected. Open items:

1. Exact SSE chunk boundaries — whether `text` deltas ever split mid-UTF-8.
   Source suggests no (`json.encode` per event), but worth confirming.
2. Whether any real deployment sends the `remote` tool-result form; OSS
   never will.
3. Behaviour of `EmptyResponse` retries as observed from the client — the
   client sees nothing, so a long silence is expected and must not trip a
   read timeout.
4. `load_skill` semantics ("embedded shell commands already executed and
   substituted") — this is a client-side execution surface worth a close look
   before ever enabling it.
