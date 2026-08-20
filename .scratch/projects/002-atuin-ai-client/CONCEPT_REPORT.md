# Atuin AI Client Library — Concept Interrogation

**Status:** exploration; wire contract now captured from source, no
implementation decision yet
**Date:** 2026-07-24, revised 2026-08-20 after the source read
**Working name:** `atuin-ai-client` (avoid committing to a package name yet)

> **Revision note.** The first draft was written from READMEs and public docs.
> It correctly identified the shape of the problem but guessed wrong on several
> load-bearing facts. `PROTOCOL-NOTES.md` now records the wire contract read
> directly from pinned upstream source. This revision folds those findings in.
> Where the original was wrong, the section says so rather than quietly
> rewriting.

## Executive conclusion

There is a real, narrow product here: a typed, safe SDK for applications that
want to participate in the Atuin AI **client protocol** without embedding the
Atuin terminal UI. It should be framed as an *agent-protocol client*, not an
LLM SDK and not an OpenAI wrapper.

The original made the case conditional on a protocol spike. That condition is
now largely discharged. The wire contract is fully readable from source, it is
frozen by released CLI binaries rather than by upstream goodwill, and the one
question flagged as decisive — how a tool result returns — has a clear answer.

The remaining risk is not "can we read the protocol." It is "is a library the
right shape for the value we hold." That question now has a sharper answer than
the original could give, because the protocol turns out to already specify the
exact capability this repo is built around.

## What exists today

`atuin-ai-server` is an Apache-2.0, self-hosted implementation of the Atuin AI
protocol. It is stateless: no accounts, database, usage limits, or trace
recording. It accepts the Atuin CLI protocol, calls one configured
OpenAI-compatible *chat-completions* backend, and streams the resulting agent
turn to its caller.

Its public routes:

| Route | Purpose |
| --- | --- |
| `GET /api/cli/models` | Return the configured model catalogue. |
| `POST /api/cli/chat` | Run one streaming chat/agent turn. |

Corrections to the original:

- **Language.** `atuin-ai-core` is **Gleam** on the BEAM, not Rust. The
  standalone server is **Elixir**/Plug. The original assumed Rust and serde.
  This matters only in that encoders and decoders are hand-written, so the wire
  shape is explicit rather than derived — easier to read, not harder.
- **"The public documentation omits the wire schema"** was true of the docs and
  irrelevant in practice. `http/request.gleam`, `http/streaming.gleam`, and
  `http/controller.gleam` are the entire client-facing contract, and they are
  short.

The distinction the original drew still holds and is worth repeating: the
server is **not OpenAI-compatible to its callers**. It consumes an
OpenAI-compatible API upstream. A client must implement Atuin's protocol.

## Why a separate library could be useful

Atuin's terminal client is a polished interactive UI, but it is not a reusable
application SDK. A library can let another terminal app, editor extension,
desktop client, automation runner, or agent host:

1. discover model choices;
2. create and continue conversations;
3. render text and command suggestions as they stream;
4. receive and answer client-side tool requests; and
5. enforce its own permission and confirmation policy.

That is meaningfully different from "call an LLM." The Atuin protocol brings a
terminal-oriented agent loop, structured command suggestions, danger and
confidence signals, and a boundary between the remote LLM/server and local
machine capabilities.

## The central design question, answered

The original asked which layer to own, and separately flagged one wire question
as decisive: does a tool result travel on the open stream, or does it start a
new request?

**It starts a new request.** The server is stateless across turns. The client
owns the entire conversation and re-POSTs the full message history on every
tool round trip. There is no side channel and no bidirectional stream.

The consequence the original missed: `pause_for_client`
(`engine/loop.gleam:623-636`) emits the client `tool_call`(s), then
`status: waiting_for_tools`, then **`done`**.

> **A `done` event does not mean the turn is over. It means the HTTP request is
> over.**

A client that treats `done` as end-of-turn silently truncates every
tool-using conversation. The disambiguator is the `waiting_for_tools` status
immediately before `done`, together with the pending `tool_call` ids.

This invalidates the original's sample API as written, and it relocates the
SDK's centre of gravity. The hard part is **not** SSE parsing. It is
accumulating conversation state correctly across a sequence of requests:
appending the assistant's `tool_use` blocks, appending the matching
`tool_result` blocks, and re-sending everything within the size cap.

Three further consequences:

- **`suggest_command` is terminal.** It ends the turn even when other tool calls
  are present (`engine/turn.gleam:68-72`). A client must never try to answer it
  as a tool.
- **Idempotency is entirely the client's problem** (original risk #5). The
  server retains nothing between requests. If the connection drops after the
  client ran a side-effecting tool but before it posted the result, only the
  client knows. `session_id` is for tracing, not resume.
- **Cancellation is closing the connection.** There is no cancel endpoint and no
  cancel message. The server notices the failed write, drains the generation for
  billing, and finishes as `Cancelled`. For the SDK this is simple and honest:
  cancel means drop the response; there is nothing to await.

The layering itself stands unchanged:

```text
Application / TUI / editor
        |
        | typed events + policy decisions
        v
Atuin AI SDK  <---->  Atuin AI Server  <---->  OpenAI-compatible LLM endpoint
        |
        | explicit local tool calls only
        v
Local tools: history, files, command runner, custom application tools
```

The SDK owns transport, validation, **conversation accumulation**, event
dispatch, and tool continuation. The embedding application owns rendering,
credential provisioning, storage policy, and whether any requested action is
allowed.

The SDK must *not* silently execute a command, mutate a file, expose shell
history, or read arbitrary files just because the model asks.

## Proposed v0 public surface

Use Python first because the existing surrounding work is Python, `pydantic`
already fits the project style, and atuout — the likely first consumer — is
Python. Keep the architecture language-neutral so another implementation can
conform later.

The original's sample implied a single streaming request. Corrected to reflect
the request loop:

```python
client = AtuinAIClient(base_url="http://localhost:8080", token=token)

models = client.list_models()
session = client.new_session(
    model="fast",
    capabilities=["client_v1_atuin_output"],   # opt in explicitly
)

# `converse` drives the *whole* agent turn: it re-POSTs the accumulated
# conversation each time the server pauses for client tools, and only
# stops when the server completes without pending tool calls.
for event in session.converse("why did that deployment fail?"):
    match event:
        case TextDelta(text=text):
            ui.append(text)
        case Status(state=state):
            ui.set_status(state)
        case CommandSuggestion(command=cmd, confidence=conf, danger=danger):
            ui.show_command(cmd, conf, danger)      # terminal — turn is done
        case ToolRequest(id=request_id, name=name, arguments=arguments):
            result = policy.dispatch(name, arguments)
            session.provide_tool_result(request_id, result)   # buffered
        case TurnComplete(usage=usage):
            ui.show_usage(usage)
        case ErrorEvent(error=error):
            ui.show_error(error)
```

`provide_tool_result` buffers rather than sends. When the generator is next
advanced and every pending tool call has a result, the SDK issues the follow-up
request. This keeps the single-loop ergonomics the original wanted while being
honest about the transport.

An application that wants the raw per-request stream should be able to get it —
`session.stream_once(...)` — but `converse` is the ergonomic default, because
driving the loop by hand is exactly the error-prone part.

### Modules

Reduced from the original's seven. The wire contract is small, and
`transport`/`events`/`session` will not divide the way a guess suggests until
the shapes are real.

| Module | Responsibility |
| --- | --- |
| `client` | Base URL, bearer auth, model catalogue, session creation. |
| `session` | **Conversation accumulation**, the request loop, turn completion, size-cap enforcement. |
| `wire` | HTTP, SSE framing, request/response models. Preserve unknown fields. |
| `tools` | Typed tool contracts and a dispatcher interface; no privileged defaults. |
| `errors` | Stable errors that retain HTTP/SSE context. |

`policy` folds into the application's dispatcher until a second consumer proves
it needs to be shared.

### First-class capabilities

- Synchronous iterator first. **Revised from the original**, which recommended
  async-first while showing a sync sample. atuout is fully synchronous — no
  `httpx`, no `asyncio`, deps are `pydantic`/`grpcio`/`protobuf` — and the
  likely consumers are hook-driven CLI paths. Add async when a consumer needs it.
- Transport injection so applications can supply their own client, proxy,
  retries, tracing, or test transport.
- Per-request deadline and cancellation, implemented as connection close.
- **Client-side size-cap enforcement.** The server rejects conversations over
  180,000 estimated tokens (4 chars/token) with a `400`. The SDK should fail
  before the round trip and should surface how close a conversation is running.
- Bounded event/message sizes to avoid a hostile or broken server exhausting
  client memory.
- Explicit `UnknownEvent` handling. This is cheap to honour, and it matches how
  the server already behaves — decoding upstream is aggressively lenient, with
  only empty `messages` and a malformed `session_id` as hard errors.

## Tool model and security posture

The agent loop is the reason this library is interesting and the main source of
risk. Tool requests cross a trust boundary: an upstream model may be
misconfigured, prompted maliciously, or compromised; the self-hosted server
operator can also see prompts and tool results.

Capabilities are declared per request, and the server sends the model only the
tools the client declared (`domain/tools.gleam:90-101`). **The capability list
is the primary security control**, and it is one JSON array. An SDK that
defaults it to empty is safe by construction.

| Capability string | Tool | Side effects |
| --- | --- | --- |
| `client_v1_read_file` | `read_file` | read |
| `client_v1_edit_file` | `edit_file` | **write** |
| `client_v1_write_file` | `write_file` | **write** |
| `client_v1_execute_shell_command` | `execute_shell_command` | **arbitrary** |
| `client_v1_atuin_history` | `atuin_history` | read |
| `client_v1_atuin_output` | `atuin_output` | read |
| `client_v1_load_skill` | `load_skill` | read, **runs embedded shell** |

### Required defaults

| Concern | SDK default |
| --- | --- |
| Declared capabilities | Empty. The application opts in per capability. |
| Tool execution | Disabled until the application registers a dispatcher. |
| Command execution | Never provided by the base package. A separate opt-in adapter may exist later. |
| File writes | Never provided by the base package. |
| File reads / history | No broad adapters by default; apps supply narrow ones. |
| Confirmation | Policy decision is required for side effects. |
| Secrets | Redact auth headers and configured sensitive argument paths in logs. |
| Server identity | HTTPS by default for non-loopback endpoints; expose TLS configuration rather than disabling verification silently. |

### Danger signals

The original's rule holds: treat confidence/danger as UI metadata, never as an
authorization result. A command rated "safe" is still untrusted input.

One detail the original could not know: the `danger` value is **not purely the
model's self-assessment**. The server keyword-scans `suggest_command` input and,
on a hit, overwrites `danger` to `"high"` and appends `"[Server Warning] ..."`
to `danger_notes` (`http/streaming.gleam:64-82`, `161-207`). A client rendering
`danger_notes` should expect that marker. This strengthens the display but
changes nothing about authorization.

### Tool output is untrusted input — a threat the original missed

The original modelled one boundary: model → client. There is a second, and it
is sharper for this repo.

**Command output is attacker-controllable text.** A command fetches a remote
page; its output lands in a durable store; the store later feeds an agent as
context. That is prompt injection with persistence and an arbitrary delay
between ingestion and use. atuout's `agent_ingest` widens it further: output
from one agent's session becomes context for another's.

Any retrieval surface over stored output must mark returned content as data,
never instruction, and must delimit and bound it. This belongs at the top of
`docs/threat-model.md`, not in a footnote.

## Compatibility and product risks

Reassessed against source. Several drop sharply.

1. ~~**Protocol stability is unproven.**~~ **Downgraded.** The event shapes are
   frozen by released CLI binaries that upstream does not control —
   `http/streaming.gleam:1-3` states the wire contract "must match the Elixir
   `Streaming` module until that module is deleted at cutover." The request
   decoder carries explicit forever-support for a legacy `capabilities` shape.
   Upstream cannot break these unilaterally. Still pin a commit and publish a
   compatibility matrix, but this is no longer a gating risk.
2. ~~**The public documentation omits the wire schema.**~~ **Resolved.** See
   `PROTOCOL-NOTES.md`.
3. **Atuin CLI may remain the only intended client.** *Unchanged, and still the
   real risk.* Nothing in the source is hostile to third-party clients, but
   nothing invites them either. The `User-Agent` version sniff on the literal
   `atuin/` prefix (`controller.gleam:866-876`) is a small sign that upstream
   models exactly one client. Worth asking upstream before a broad release.
4. ~~**Tool semantics may be terminal-specific.**~~ **Partly resolved, and
   inverted.** The tools are specified precisely enough to implement, and one of
   them — `atuin_output` — maps onto atuout's existing schema almost exactly.
   See `ATUIN-OUTPUT-FIT.md`. What remains unspecified is *rendering* detail
   (multi-range separators), which is client-side convention, not protocol.
5. **SSE is a long-lived, failure-prone interface.** *Sharpened.* There is no
   resume: no `id:` field, no `Last-Event-ID`, no heartbeat. Confirmed by
   absence. The original said "if the protocol lacks idempotency rules, expose
   the ambiguity rather than auto-retrying." That is now the definite
   requirement, not a contingency.
6. **"Wrapper" can become featureless.** *Still live, but now answerable.* The
   library wins only if it carries something the Atuin CLI cannot. It does —
   durable output behind an already-specified tool — but that value lives in
   atuout's store, not in the SDK. Which is the argument for weighing the MCP
   path seriously; see below.

## Decisions

Several are now answerable.

1. **First consumer.** **atuout.** It is the surrounding project, it is Python,
   and it holds data the protocol already has a tool for. This was Decision #1
   and is no longer open.
2. **Python-only, or protocol document plus fixtures?** `PROTOCOL-NOTES.md` is
   already the protocol document. Publishing fixtures costs little once a spike
   runs. Do both; the document is the durable artifact even if the library is
   not.
3. **Session persistence in scope?** No — and the reason is now stronger than
   "keep v0 small." atuout already owns persistence. An SDK holding conversation
   state would duplicate a responsibility this repo has solved. Memory-only.
4. **Atuin Hub as well as self-hosted?** Self-hosted OSS only in v0. Unchanged.
   Hub adds login and usage semantics deliberately absent from the OSS server.
5. **MCP bridge as a package feature?** **Reversed.** The original deferred it.
   For this repo it should be evaluated *first*, because the asset being
   delivered is the same in both cases — atuout's durable output — while the
   costs differ sharply:

   | | Atuin AI SDK | MCP server over atuout |
   | --- | --- | --- |
   | Protocol stability | frozen, but no release, no versioning story | stable, documented, versioned |
   | Upstream goodwill needed | yes (risk #3) | no |
   | Clients reachable | Atuin AI users on self-hosted OSS servers | any MCP host |
   | Work before value | request loop + schemas + dispatcher | read-only wrapper over existing SQLite |

   The two are not exclusive, and the range-resolution work is shared between
   them. Doing MCP first is the cheaper way to prove the data is useful before
   paying for the protocol.
6. **Minimum Python and HTTP stack?** Python 3.11+ and Pydantic v2 — both match
   atuout's `pyproject.toml`. **Sync-first**, revised from the original; atuout
   has no async anywhere.

## Remaining spike

The original proposed a nine-item spike as the gate before any coding. Source
reading discharged most of it. What is left needs a live server, and it is
small:

1. UTF-8 chunk boundaries — whether `text` deltas ever split a codepoint.
   Source suggests not (one `json.encode` per event), but confirm.
2. Client-visible behaviour of `EmptyResponse` retries. The client sees nothing
   during them, so a long silence is expected and must not trip a read timeout.
3. `load_skill` semantics. "Embedded shell commands already executed and
   substituted" describes a client-side execution surface. Understand it before
   ever enabling that capability.
4. A recorded end-to-end tool round trip, to become the first replay fixture.

Items 1–4 of the original spike (model list, conversational turn,
command-generation turn, tool continuation) are now specified well enough to
write fixtures by hand and validate them against a live server later, rather
than blocking on one.

### Go / no-go gates, evaluated

| Gate | Status |
| --- | --- |
| Wire contract capturable and replayable deterministically | **Met.** Fully specified in source; no hidden state. |
| Tool request has stable correlation and a clear continuation | **Met.** `tool_use_id` correlation, continuation is a full re-POST. |
| Cancellation and dropped connections have understandable semantics | **Met, and simple.** Cancel is connection close. No resume exists — the ambiguity is bounded and documentable. |
| One concrete external consumer benefits over `atuin ai` directly | **Met.** atuout serves `atuin_output` durably where the CLI serves it from an evicting buffer. |
| Upstream has a viable pinning/versioning story | **Partly.** No releases or version header, but the format is frozen by released binaries. Accept maintaining against a pinned commit. |

Four of five clean; the fifth is an accepted cost rather than a blocker. The
original's fallback — "stop at a protocol-spec/fixtures repository" — is worth
keeping as the *first deliverable* rather than the failure mode.

## Suggested repository bootstrap

```text
atuin-ai-client/
  pyproject.toml
  README.md
  src/atuin_ai_client/
    __init__.py
    client.py
    session.py       # conversation accumulation + request loop
    wire.py          # HTTP, SSE, request/response models
    tools.py
    errors.py
  tests/
    fixtures/
    test_models.py
    test_sse.py
    test_conversation.py   # accumulation across tool round trips
    test_tool_loop.py
    test_compatibility.py
  docs/
    protocol-notes.md
    threat-model.md
    compatibility.md
```

## Sources

Primary — pinned source, vendored under `reference/` (gitignored):

- `atuinsh/atuin-ai-core` @ `565877e9` (2026-08-12), Gleam. The client-facing
  contract is `http/request.gleam`, `http/streaming.gleam`,
  `http/controller.gleam`, `engine/loop.gleam`, `engine/turn.gleam`,
  `domain/tools.gleam`, `domain/capabilities.gleam`, `domain/config.gleam`.
- `atuinsh/atuin-ai-server` @ `4d582bc5` (2026-08-12), Elixir.
  `lib/atuin_ai/server/router.ex` is the routes and auth.

Secondary — consulted for the first draft:

- <https://docs.atuin.sh/main/ai/settings/>
- <https://docs.atuin.sh/main/ai/introduction/>

Derived: `PROTOCOL-NOTES.md`, `ATUIN-OUTPUT-FIT.md`.

## Bottom line

Revised. The original said: build this only after a protocol-fixture spike. The
spike has largely been replaced by reading the source, and the protocol is in
better shape than feared — small, frozen by released clients, and already
carrying a tool that fits this repo's data.

The open question is no longer *can we speak this protocol*. It is *is a
protocol client the best way to deliver what atuout holds*. The durable value is
the store, not the transport. Ship the data over MCP first, where it reaches
every agent host with no upstream dependency; treat the Atuin SDK as the second
delivery path, built on the range-resolution work the first one already needs.
