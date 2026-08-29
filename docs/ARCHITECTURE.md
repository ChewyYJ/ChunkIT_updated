# ChunkIT — Architecture Deep Dive

> The step-by-step request-flow documentation, kept from the original
> team README. The top-level [README](../README.md) is the overview;
> this file walks every stage in detail.


ChunkIT is a context-compression gateway for LLM chat requests. It accepts
an OpenAI-style chat payload, splits the conversation into context blocks,
compresses older or lower-priority context into smaller normalized units,
keeps the most important parts under a token budget, and forwards the result
to a downstream model. If the model later needs omitted context, it can call
back into the app and retrieve the original stored content.

Today, the project has:
- a working FastAPI gateway (OpenAI-compatible, streaming + tool passthrough)
- a working Person 2 context-processing pipeline
- local and Daytona-backed worker modes
- a working Person 3 relevance-scoring layer (Nosana-hosted reranker with a
  deterministic local fallback) feeding the budgeter
- a working retrieval loop for omitted context
- a demo dashboard at `GET /dashboard` and a baseline benchmark in `evals/`

## What Problem This Solves

Long chat histories are expensive to send to an LLM every turn. ChunkIT tries
to reduce prompt size without losing the ability to recover important details.

The basic idea is:
1. Break the conversation into blocks.
2. Keep the newest and most important blocks pinned.
3. Compress older blocks into typed `ContextUnit`s.
4. Decide which units to keep and which to omit under a token budget.
5. Replace omitted blocks with retrieval markers.
6. Let the model ask for omitted content only when it actually needs it.

## Architecture

The project is split into a few layers:

- `app/gateway/`
  - HTTP entrypoint
  - message parsing
  - budget selection
  - final message assembly
- `app/daytona/`
  - Person 2 processing pipeline
  - local and remote worker backends
  - Daytona sandbox lifecycle helpers
- `compressors/`
  - content-type detection
  - secret redaction
  - log, JSON, and text/code compression helpers
- `app/kimi/`
  - downstream LLM client
  - retrieval tool injection and tool-call loop
- `app/session/`
  - SQLite storage for original context
- `workers/`
  - worker API for local mode
  - standalone worker server for Daytona remote mode
- `shared/`
  - schema contract shared across team roles

## Shared Contract

The team contract lives in [`shared/schemas.py`](shared/schemas.py).

Important types:

- `ContextBlock`
  - one raw block extracted from the original incoming messages
- `ContextUnit`
  - one normalized unit produced by Person 2
- `ScoredUnit`
  - one unit plus its relevance score and final priority
- `SelectionPlan`
  - unit IDs to `keep`, `compact`, or `omit`
- `RunStats`
  - dashboard-oriented metrics for a run

The schemas file also still contains `mock_units_from_block(...)` and
`mock_score_units(...)` — these are TEST HELPERS only. The gateway uses the
real pipeline end to end: Person 2's `prepare_context_blocks(...)` for units
and Person 3's `rank_and_select(...)` for scoring (live Nosana reranker with
a deterministic local fallback).

## Full Request Flow

### 1. Request enters the gateway

[`app/main.py`](app/main.py)
registers the FastAPI app and includes the gateway router.

[`app/gateway/routes.py`](app/gateway/routes.py)
handles `POST /v1/chat/completions`.

The request shape is:

```json
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "target_tokens": 2000
}
```

`messages` is required. `target_tokens` is optional.

### 2. Messages are parsed into `ContextBlock`s

[`app/gateway/message_parser.py`](app/gateway/message_parser.py)
turns each incoming message into a `ContextBlock`.

Blocks are marked `pinned` if they are:
- a `system` message
- the newest message
- near the newest turn
- messages containing strong constraint keywords like `must not`, `never`, or `do not`

Pinned blocks are never candidates for omission.

### 3. Blocks are processed into `ContextUnit`s

[`app/daytona/client.py`](app/daytona/client.py)
is the main Person 2 entrypoint.

It exposes:

```python
await prepare_context_blocks(blocks)
```

This function:
- processes blocks concurrently with `asyncio.gather(...)`
- caches results by a backend-aware fingerprint that includes:
  - original source hash
  - sanitized content hash
  - detected route
  - backend
  - compressor version
  - compression/redaction config versions
- chooses either local mode or remote Daytona mode

### 4. Content is compressed by type

[`app/daytona/worker.py`](app/daytona/worker.py)
prepares one block at a time.

It does:
- secret redaction before any remote dispatch
- content-route detection
- compression or chunking based on detected type
- token estimation
- deterministic `structural_priority` assignment
- safety guards that fail open to sanitized passthrough if compression is not better
- per-block provenance metrics for storage and debug output

The helpers live in [`compressors/`](compressors):

- [`detect.py`](compressors/detect.py)
  - routes content as `log`, `json`, `search`, `diff`, `code`, or `text`
- [`common.py`](compressors/common.py)
  - secret redaction
  - token estimation
  - hashing
- [`logs.py`](compressors/logs.py)
  - dedupes repeated lines
  - keeps warnings, errors, exceptions, and final status lines
- [`json_compactor.py`](compressors/json_compactor.py)
  - preserves overall schema shape
  - keeps key fields
  - summarizes lists with counts and samples
- [`search.py`](compressors/search.py)
  - groups grep/ripgrep style hits by file
  - preserves line references and total match counts
- [`diffs.py`](compressors/diffs.py)
  - preserves per-file unified diff hunks
- [`text_chunker.py`](compressors/text_chunker.py)
  - splits long text/code into chunks
  - removes obvious repeated chunks

### 5. Local mode vs Daytona remote mode

#### Local mode

In local mode, `prepare_context_blocks(...)` runs the worker logic directly
inside the app process using `asyncio.to_thread(...)`.

This is the simplest mode and does not require Daytona credentials.

#### Remote Daytona mode

In remote mode, [`app/daytona/remote.py`](app/daytona/remote.py)
creates and manages a Daytona worker pool.

That pool:
- creates one parent sandbox
- creates linked child worker sandboxes
- uploads a worker script into each child
- starts a lightweight HTTP worker inside each child
- round-robins `POST /prepare` requests across the workers

The worker script used inside Daytona is:

- [`workers/sandbox_prepare_server.py`](workers/sandbox_prepare_server.py)

The local FastAPI worker shape is:

- [`workers/service.py`](workers/service.py)

Remote mode is enabled only when:
- `DAYTONA_BACKEND=remote` or `DAYTONA_BACKEND=daytona`
- `DAYTONA_API_KEY` is set

If remote processing fails during a request, the code currently falls back to
the local worker so the gateway can fail open.

The remote pool now also:
- labels owned parent/child sandboxes with stable ownership metadata
- reuses healthy owned sandboxes when appropriate
- health-checks workers before reuse
- respawns missing or unhealthy children
- deletes owned children and the owned parent on controlled shutdown

### 6. Original context is stored for retrieval

[`app/session/store.py`](app/session/store.py)
stores the original uncompressed block content in SQLite.

This is what powers later retrieval if the model needs omitted context.

Storage is scoped by `session_id + block_id`, so two sessions can both contain
`msg_1` without overwriting each other. Retrieval is also session-scoped inside
the `retrieve_context` tool loop to prevent cross-session leakage.

The database file is:

- `data/sessions.db`

### 7. Units are scored (Person 3)

[`app/selection/service.py`](app/selection/service.py) exposes
`rank_and_select(...)`, which the gateway calls with the blocks, units, the
latest user objective, and the token budget.

Scoring works like this:
- pinned blocks bypass scoring entirely and always survive
- non-pinned units go to the live Nosana scorer
  ([`app/nosana/client.py`](app/nosana/client.py)) when
  `NOSANA_SCORER_URL`/`NOSANA_SCORER_TOKEN` are set — batched `POST /score`
  with retries, strict unit-ID validation, and short timeouts
- on any remote failure (or when the URL is unset) the deterministic lexical
  fallback ([`app/nosana/fallback.py`](app/nosana/fallback.py)) scores locally
  — a Nosana outage never triggers gateway-wide fail-open
- [`app/scoring/priority.py`](app/scoring/priority.py) combines
  `0.65*relevance + 0.15*recency + 0.10*structural + 0.10*role`

`debug.score_source` reports `nosana`, `fallback`, or `disabled`, with
`fallback_reason` and `nosana_ms` alongside. The deployable scorer service
lives in [`nosana-service/`](nosana-service) with manual deployment steps in
[`nosana/DEPLOYMENT.md`](nosana/DEPLOYMENT.md).

### 8. A selection plan is built

[`app/gateway/budgeter.py`](app/gateway/budgeter.py)
creates a `SelectionPlan`.

Current behavior:
- pinned blocks are always kept verbatim (charged their original tokens)
- non-pinned units are sorted by `final_priority` (deterministic tie-breaks)
- kept non-pinned units go to the `compact` tier until the budget is reached
- remaining units are omitted (one marker charged per omitted block)
- the estimate matches what is actually forwarded: original tokens for
  pinned, compressed tokens for compacted, marker cost for omitted

### 9. Final outbound messages are assembled

[`app/gateway/assembler.py`](app/gateway/assembler.py)
rebuilds the chat payload sent to the downstream model.

Assembly follows the three selection tiers, always preserving message
structure (role, `tool_calls`, `tool_call_id`):
- pinned blocks forward their original content verbatim
- compacted blocks forward their kept units' compressed, secret-redacted
  text (in source order); if some units were omitted, a one-line note with
  the block's `context_id` is appended
- fully omitted blocks are replaced with a `[CONTEXT OMITTED …]` marker

Markers and notes mention `retrieve_context` only when the tool is actually
advertised (any omission occurred and the client brought no tools of its own).
Multimodal (non-string) content and blank tool_calls-only messages always
pass through verbatim.

### 10. The model call is made

[`app/kimi/client.py`](app/kimi/client.py)
calls the downstream LLM.

Supported providers:
- `openai`
- `kimi`

Provider selection is controlled by `LLM_PROVIDER`.

If provider is `kimi`, the client also forwards:
- `prompt_cache_key`

and reads back:
- `cached_tokens`

### 11. Retrieval loop can recover omitted context

[`app/kimi/tools.py`](app/kimi/tools.py)
defines the `retrieve_context` tool.

The client injects that tool into the model call. If the model requests one or
more omitted context IDs:

1. the tool looks up original content in SQLite
2. the retrieved content is appended as a `tool` message
3. the model call continues

This gives the system a reversible compression loop instead of a one-way
summary-only flow.

## Current Behavior Summary

What is real today:
- FastAPI app and `/v1/chat/completions`
- context splitting
- local compression pipeline
- Daytona remote worker pipeline
- session storage
- omitted-context retrieval
- OpenAI and Kimi client support

What is real from Person 3:
- live Nosana relevance scoring with deterministic local fallback
- priority formula + hardened budgeter (deterministic tie-breaks, marker
  token accounting, over-budget reporting)
- OpenAI-shaped responses, SSE streaming with invisible internal retrieval
  rounds, client tool passthrough, shared sessions via `session_id`
- demo dashboard (`GET /dashboard`) and baseline benchmark (`evals/benchmark.py`)

What is still simplified today:
- token counts use a `len/4` estimate rather than a real tokenizer

## Environment Variables

See [`.env.example`](.env.example).

### LLM configuration

```env
LLM_PROVIDER=openai

OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini

MOONSHOT_API_KEY=
KIMI_MODEL=kimi-k3
KIMI_BASE_URL=https://api.moonshot.ai/v1
```

### Daytona configuration

```env
DAYTONA_API_KEY=
DAYTONA_API_URL=https://app.daytona.io/api
DAYTONA_TARGET=
DAYTONA_BACKEND=local
DAYTONA_WORKER_COUNT=2
DAYTONA_WORKER_PORT=8000
DAYTONA_CHILD_AUTO_STOP_MINUTES=15
DAYTONA_SANDBOX_PREFIX=chunkit
```

Important notes:
- `DAYTONA_BACKEND=local` means run the processing pipeline inside the app
- `DAYTONA_BACKEND=remote` means run context preparation inside Daytona workers
- `DAYTONA_TARGET` can be set explicitly if the organization has no default region

### Other keys

```env
NOSANA_API_KEY=
```

`NOSANA_API_KEY` is present because Person 3's service is expected later, but
it is not used by the current pipeline.

## Installation

Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Running the App

### Local mode

```bash
uvicorn app.main:app --reload
```

### Remote Daytona mode

```bash
export DAYTONA_BACKEND=remote
uvicorn app.main:app --reload
```

When remote mode is enabled and credentials are valid, the app tries to warm
the Daytona worker pool on startup.

## Health Check

Run:

```bash
curl http://localhost:8000/health
```

Example response:

```json
{
  "status": "ok",
  "daytona": {
    "ready": false,
    "worker_count": 0,
    "parent_id": null,
    "prefix": "chunkit-12345-ab12cd"
  }
}
```

In remote mode, `ready` and `worker_count` should reflect the initialized pool.

## Example Request

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "system", "content": "You are a concise assistant."},
      {"role": "user", "content": "INFO boot\nINFO boot\nERROR failed to connect to redis\nstatus done"},
      {"role": "assistant", "content": "I will inspect the issue."},
      {"role": "user", "content": "In one sentence, summarize what happened."}
    ],
    "target_tokens": 25
  }'
```

The response includes:
- model content
- upstream usage
- raw provider payload
- `debug` metadata including token estimates, omission count, pipeline timing,
  retrieval count, cache info, and per-block Daytona preparation metrics

The per-block debug payload includes fields such as:
- detected route
- schema content type
- compressor and compressor version
- source hash
- original vs compressed token counts
- tokens saved
- redaction applied
- cache hit
- fallback used
- backend (`local` or `daytona`)

## Agent-Client Compatibility (Person 3)

`POST /v1/chat/completions` is OpenAI-compatible — point any OpenAI-SDK
client's base URL at the gateway. Beyond `messages` + `target_tokens`:

- `stream: true` returns OpenAI-style SSE. ChunkIT's internal
  `retrieve_context` rounds are handled invisibly mid-stream; `debug` rides a
  leading `: chunkit-debug {...}` SSE comment. Pre-stream upstream failures
  map to real HTTP errors.
- client `tools` / `tool_choice` / `temperature` / `max_tokens` pass through.
  When the client brings its own tools, `retrieve_context` steps aside and
  markers drop the tool instruction.
- assistant `tool_calls` / `tool`-role histories survive compression
  structurally intact — only message content is ever replaced by a marker,
  and a fresh trailing tool-result run is pinned so the model always sees
  new tool output at least once.
- `compression_enabled: false` gives a fair full-context baseline.
- `session_id`: agents sharing it share the original-context store and, on
  Kimi, the `prompt_cache_key` — one provider-side context cache for a team
  of agents (watch `debug.cached_tokens`).

Non-streaming responses are the upstream OpenAI response body plus `debug`
(score source, token savings, per-unit scores, latencies incl. `daytona_ms`,
`daytona_blocks` provenance, retrieval count, fail-open state).

## Dashboard and Benchmark

- `GET /dashboard` — ChunkIT-branded live dashboard: token savings, per-unit
  relevance and keep/omit decisions, latencies, reliability, and a
  full-vs-compressed baseline compare.
- `python evals/benchmark.py` (gateway running) — alternating
  full/compressed trials, warm-up excluded, provider-measured prompt tokens,
  results written to `evals/results/`.

## Testing

Current test and smoke-test coverage includes:

- [`tests/test_daytona.py`](tests/test_daytona.py)
  - redaction
  - JSON/log handling
  - caching
  - local sandbox lifecycle
  - remote enablement gating
- [`tests/test_pipeline.py`](tests/test_pipeline.py)
  - gateway budgeting and omission behavior
- [`tests/test_retrieval.py`](tests/test_retrieval.py)
  - omitted-context retrieval behavior

The main suite is pytest (unit + integration, no live sponsor access
needed — live Nosana tests are opt-in via `RUN_LIVE_NOSANA_TESTS=1`):

```bash
python -m pytest -q
```

You can also run the focused legacy scripts directly:

```bash
.venv/bin/python tests/test_daytona.py
.venv/bin/python tests/test_pipeline.py
.venv/bin/python tests/test_retrieval.py
```

You can also smoke-test Daytona worker setup with:

```bash
.venv/bin/python daytona_bootstrap.py
```

## Important Files

- [`app/main.py`](app/main.py)
  - app startup, shutdown, health check
- [`app/gateway/routes.py`](app/gateway/routes.py)
  - main `/v1/chat/completions` route
- [`app/gateway/message_parser.py`](app/gateway/message_parser.py)
  - parse incoming messages into blocks
- [`app/gateway/budgeter.py`](app/gateway/budgeter.py)
  - build `SelectionPlan`
- [`app/gateway/assembler.py`](app/gateway/assembler.py)
  - build final forwarded chat payload
- [`app/daytona/client.py`](app/daytona/client.py)
  - Person 2 pipeline entrypoint
- [`app/daytona/remote.py`](app/daytona/remote.py)
  - remote Daytona pool management
- [`app/daytona/worker.py`](app/daytona/worker.py)
  - block-to-units processing logic
- [`app/kimi/client.py`](app/kimi/client.py)
  - downstream LLM client and retrieval loop
- [`app/kimi/tools.py`](app/kimi/tools.py)
  - retrieval tool implementation
- [`app/session/store.py`](app/session/store.py)
  - SQLite storage layer

## Known Limitations

These are the biggest current limitations of the codebase:

- when a client supplies its own `tools`, `retrieve_context` is not injected,
  so omitted context is not recoverable in that mode (markers still say what
  was omitted)
- remote worker cleanup can still be improved further for long-running repeated demos
- compression heuristics are intentionally simple and hackathon-oriented
- token estimates use a `len/4` heuristic, not a real tokenizer

## Demo Narrative

If you are explaining the system during a demo, the shortest accurate summary is:

1. A chat request hits the gateway.
2. The gateway turns messages into context blocks.
3. Older blocks are compressed into normalized units locally or in Daytona workers.
4. The app keeps the most important units under a token budget.
5. Omitted context is replaced with retrieval markers.
6. The model answers using the smaller prompt.
7. If needed, the model asks for omitted context and the app restores it from SQLite.

That is the core ChunkIT loop.
