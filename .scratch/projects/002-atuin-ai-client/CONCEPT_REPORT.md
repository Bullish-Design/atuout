# Atuin AI Client Library — Concept Interrogation

**Status:** exploration, no implementation decision yet  
**Date:** 2026-07-24  
**Working name:** `atuin-ai-client` (avoid committing to a package name yet)

## Executive conclusion

There is a real, narrow product here: a typed, safe SDK for applications that
want to participate in the Atuin AI **client protocol** without embedding the
Atuin terminal UI. It should be framed as an *agent-protocol client*, not an
LLM SDK and not an OpenAI wrapper.

The case to proceed is conditional. The protocol is newly public and has a
very small server implementation, so the first milestone must be an
interoperability spike against a pinned server revision. Do not promise a
stable general-purpose API until that spike has captured real request/SSE
schemas and tool-loop behaviour.

## What exists today

`atuin-ai-server` is an Apache-2.0, self-hosted implementation of the Atuin
AI protocol. It is stateless: no accounts, database, usage limits, or trace
recording. It accepts the Atuin CLI protocol, calls one configured
OpenAI-compatible *chat-completions* backend, and streams the resulting agent
turn to its caller.

Its documented public routes are:

| Route | Purpose |
| --- | --- |
| `GET /api/cli/models` | Return the configured model catalogue. |
| `POST /api/cli/chat` | Run a streaming chat/agent turn. |

The server’s upstream model must support tool calling. The core engine owns
request decoding, SSE output, a receive loop, and the LLM/tool turn loop. The
standalone server composes the core with stateless defaults; optional deployed
concerns include server tools, usage limits, trace recording, and persistence
of tool results.

Important distinction: the server is **not documented as OpenAI-compatible to
its callers**. It consumes an OpenAI-compatible API upstream. A client library
must implement Atuin’s protocol rather than send `POST /v1/chat/completions`.

## Why a separate library could be useful

Atuin’s terminal client is a polished interactive UI, but it is not a reusable
application SDK. A library can let another terminal app, editor extension,
desktop client, automation runner, or agent host:

1. discover model choices;
2. create and continue conversations;
3. render text and command suggestions as they stream;
4. receive and answer client-side tool requests; and
5. enforce its own permission and confirmation policy.

That is meaningfully different from “call an LLM.” The Atuin protocol brings a
terminal-oriented agent loop, structured command suggestions, danger and
confidence signals, and a boundary between the remote LLM/server and local
machine capabilities.

## The central design question: which layer do we own?

The viable library boundary is below application UX and above HTTP:

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

The SDK should own transport, validation, session state, event dispatch, and
tool continuation. The embedding application should own rendering, credential
provisioning, storage policy, and whether any requested action is allowed.

The SDK must *not* silently execute a command, mutate a file, expose shell
history, or read arbitrary files just because the model asks.

## Proposed v0 public surface

Use Python first because the existing surrounding work is Python, `pydantic`
already fits the project style, and Python has strong sync/async HTTP options.
Keep the architecture language-neutral so another implementation can conform
later.

```python
client = AtuinAIClient(base_url="http://localhost:8080", token=token)

models = client.list_models()
session = client.new_session(model="llama")

for event in session.stream("show the last failed deployment command"):
    match event:
        case TextDelta(text=text):
            ui.append(text)
        case CommandSuggestion(command=command, confidence=confidence, danger=danger):
            ui.show_command(command, confidence, danger)
        case ToolRequest(id=request_id, name=name, arguments=arguments):
            result = policy.dispatch(name, arguments)
            session.submit_tool_result(request_id, result)
        case ErrorEvent(error=error):
            ui.show_error(error)
```

The actual method and event names are intentionally provisional; they must be
derived from captured protocol fixtures, not invented from the UI description.

### Modules

| Module | Responsibility |
| --- | --- |
| `client` | Base URL handling, bearer auth, model catalogue, session creation. |
| `transport` | HTTP, SSE framing, timeouts, cancellation, protocol headers. |
| `events` | Discriminated Pydantic event/request/result models. Preserve unknown fields. |
| `session` | Conversation identity, turn continuation, ordering, cancellation. |
| `tools` | Typed tool contracts and a dispatcher interface; no privileged defaults. |
| `policy` | Allow/deny/confirm decisions, capability grants, audit hooks. |
| `errors` | Stable errors that retain HTTP/SSE context and retry guidance. |

### First-class capabilities

- Synchronous iterator and `async for` streaming interfaces.
- Transport injection so applications can supply their own `httpx` client,
  proxy, retries, tracing, or test transport.
- Per-request deadline and cancellation. Streaming UIs need stop-now behaviour.
- Bounded event/message sizes to avoid a hostile or broken server exhausting
  client memory.
- Protocol diagnostics: server version/headers when available, raw event type,
  and a redacted payload excerpt in errors.
- Explicit `UnknownEvent` handling, so a newer server does not crash or cause
  unsafe behaviour in an older client.

## Tool model and security posture

The agent loop is the reason this library is interesting and the main source
of risk. Tool requests cross a trust boundary: an upstream model may be
misconfigured, prompted maliciously, or compromised; the self-hosted server
operator can also see prompts and tool results.

### Required defaults

| Concern | SDK default |
| --- | --- |
| Tool execution | Disabled until the embedding app registers a dispatcher. |
| Command execution | Never provided by the base package. A separate opt-in adapter may exist later. |
| File writes | Never provided by the base package. |
| File reads / history | No broad adapters by default; apps supply narrow ones. |
| Confirmation | Policy decision is required for side effects. |
| Secrets | Redact auth headers and configured sensitive argument paths in logs. |
| Server identity | HTTPS by default for non-loopback endpoints; expose TLS configuration rather than disabling verification silently. |

The library should treat Atuin’s model confidence/danger signal as useful UI
metadata, never as an authorization result. A command rated “safe” is still
untrusted input.

## Compatibility and product risks

1. **Protocol stability is unproven.** The server and core are newly open
   sourced and the standalone server has no published release. Pin an upstream
   commit for supported versions, test against it, and publish an explicit
   compatibility matrix.
2. **The public documentation omits the wire schema.** Routes alone are not a
   sufficient SDK contract. We need request bodies, every SSE event, tool
   result continuation semantics, error cases, and cancellation behaviour.
3. **Atuin CLI may remain the only intended client.** Before a broad release,
   ask upstream whether they will accept protocol fixtures, a version header,
   and compatibility tests. A friendly answer changes maintenance cost
   materially.
4. **Tool semantics may be terminal-specific.** History, output, shell state,
   and command execution can rely on Atuin’s local client implementation.
   The SDK should provide extension interfaces, not claim parity prematurely.
5. **SSE is a long-lived, failure-prone interface.** Network drops must not
   create duplicate tool actions. Tool-request IDs and turn IDs need clear
   idempotency and resume rules; if the protocol lacks them, expose the
   ambiguity rather than auto-retrying.
6. **“Wrapper” can become featureless.** If consumers only need command
   generation, they may prefer the Atuin CLI, a subprocess integration, or a
   direct LLM client. The library must win on safe tool-loop integration and
   typed streaming ergonomics.

## Decisions to make before coding

1. Who is the first consumer: a Python TUI, an editor integration, a desktop
   client, or a library users embed in agents? Choose one; it determines the
   first ergonomic API.
2. Should the package be Python-only, or should we publish a protocol document
   and test fixtures designed for multi-language implementations?
3. Is conversation/session persistence in scope? The server is stateless;
   client persistence introduces encryption, retention, migration, and
   sensitive-data responsibilities. Recommendation: memory-only in v0.
4. Do we need to support Atuin Hub as well as self-hosted OSS servers?
   Recommendation: self-hosted `endpoint_protocol = "oss"` only in v0. Hub
   adds login and usage semantics that are deliberately absent from the OSS
   server.
5. Is an MCP bridge a package feature? Recommendation: not initially. It is a
   separate user-facing protocol and should consume the SDK only after the
   core tool loop is proven.
6. What is the minimum supported Python version and HTTP stack? Recommendation:
   Python 3.11+, `httpx`, Pydantic v2; provide async first with a thin sync
   facade only if the target application needs it.

## Validation spike (the next concrete work)

Create a throwaway, pinned test environment with the current upstream server
and a local tool-calling model. Capture sanitized fixtures for:

1. model-list response;
2. simple conversational turn;
3. command-generation turn;
4. each client-side tool request and the tool-result continuation;
5. server-side web tool activity, if configured;
6. malformed request and invalid token responses;
7. upstream model failure mid-stream;
8. cancellation before, during, and after a tool request; and
9. unknown event types / extra fields.

Only after this should we define Pydantic schemas and the client API. The
spike should produce fixture-based conformance tests that can be replayed
without an LLM or network connection.

### Go / no-go gates

Proceed to a library prototype only if all are true:

- The wire contract can be captured and replayed deterministically.
- A tool request has a stable correlation mechanism and a clear continuation
  request.
- Cancellation and dropped connections have understandable semantics.
- One concrete external consumer benefits over using `atuin ai` directly.
- Upstream has a viable pinning/versioning story, or we explicitly accept
  maintaining compatibility against a fixed commit.

Otherwise, stop at a protocol-spec/fixtures repository or use a subprocess
adapter around the Atuin CLI for the first consumer.

## Suggested repository bootstrap, if the spike passes

```text
atuin-ai-client/
  pyproject.toml
  README.md
  src/atuin_ai_client/
    __init__.py
    client.py
    session.py
    transport.py
    events.py
    tools.py
    policy.py
    errors.py
  tests/
    fixtures/
    test_models.py
    test_sse.py
    test_session.py
    test_tool_loop.py
    test_compatibility.py
  docs/
    protocol-notes.md
    threat-model.md
    compatibility.md
```

## Sources consulted

- Atuin AI Server README, current `main` branch (routes, stateless design,
  upstream requirements, configuration and authentication):
  <https://github.com/atuinsh/atuin-ai-server>
- Atuin AI Core README, current `main` branch (core responsibility, SSE and
  receive loop, deployment composition):
  <https://github.com/atuinsh/atuin-ai-core>
- Atuin AI settings (OSS endpoint mode and client capabilities):
  <https://docs.atuin.sh/main/ai/settings/>
- Atuin AI introduction (user-facing command generation, follow-up, and
  danger/confidence behaviour):
  <https://docs.atuin.sh/main/ai/introduction/>

## Bottom line

Build this as a small, security-conscious SDK only after a protocol-fixture
spike. Its durable value is a reliable client-side agent/tool loop for Atuin
AI—not HTTP convenience methods, OpenAI abstraction, or automatic terminal
control.
