# ChunkIT

**Give agents less context without making them less capable.**

## Overview

AI agents re-send their entire conversation history to the model on every
turn, even though most of it is stale by the time it's sent again — and
that gets billed as tokens every single time. ChunkIT is a gateway that
sits between an AI agent and its LLM provider, compressing that history
before it reaches the model: it keeps what matters to the current question,
shrinks or hides the rest, and forwards a much smaller request. Nothing is
lost — anything left out can be retrieved back by the model if it's
actually needed.

Measured on our demo trace, provider-counted: **1,515 -> 801 prompt tokens
(~47% saved)** with the compressed answer matching the full-context answer.
Gateway overhead: **~80 ms** per request.

## Features

- **Drop-in gateway** - OpenAI-compatible and Anthropic-compatible endpoints, so any agent (Claude Code, an OpenAI-SDK client, etc.) can point at it with a one-line config change
- **Content-aware compression** - detects logs, JSON, diffs, and search output, and compresses each differently
- **Relevance-based selection** - scores context against the current question, not just age, to decide what to keep
- **Reversible** - omitted content is stored and can be retrieved back by the model mid-request, never deleted
- **Fail-open** - if any stage breaks, the original request still goes through unmodified
- **Live dashboard** - real-time view of every request, what got compressed, and how much was saved

## Tech stack

- **Backend:** Python, FastAPI
- **LLM clients:** OpenAI SDK (used for both OpenAI and Kimi, since Kimi is OpenAI-API-compatible)
- **Storage:** SQLite (original context store)
- **Frontend:** self-contained HTML dashboard, no build step
- **Infrastructure:** Daytona (sandboxed compression), Nosana (GPU-hosted relevance scoring), Kimi/OpenAI (reasoning)

## How each piece is used

| Layer | Provider | What it does |
|---|---|---|
| Context processing | **Daytona** | Detects content type, redacts secrets, compresses logs/JSON/diffs/search output into evidence-sized units - locally or in remote Daytona worker sandboxes |
| Relevance scoring | **Nosana** | A GPU-hosted cross-encoder reranker scores every unit against the current objective, with a deterministic local fallback so a scorer outage never breaks a request |
| Reasoning | **Kimi** (or OpenAI) | The compressed conversation is answered by the downstream model, with `prompt_cache_key` forwarding for cross-agent cache reuse |

---

## How it works

```
            agent request (OpenAI or Anthropic format)
                            |
   +------------------------v-------------------------+
   | 1 PARSE      messages -> ContextBlocks           |
   |              pin: system, current turn, fresh    |
   |              tool results, constraints, images   |
   +----------------------------------------------------+
   | 2 COMPRESS   Daytona workers (local or remote):  |
   |              detect type -> redact secrets ->    |
   |              dedupe logs / compact JSON / chunk  |
   |              text -> ContextUnits + provenance   |
   +----------------------------------------------------+
   | 3 SCORE      Nosana reranker (or deterministic   |
   |              fallback) scores units against the  |
   |              current question; final priority =  |
   |              0.65*relevance + 0.15*recency +     |
   |              0.10*structural + 0.10*role         |
   +----------------------------------------------------+
   | 4 BUDGET     three tiers, truthful accounting:   |
   |              KEEP (pinned, verbatim) - COMPACT   |
   |              (compressed text) - OMIT (marker)   |
   +----------------------------------------------------+
   | 5 ASSEMBLE   original message order & structure  |
   |              preserved; only content is rewritten|
   +----------------------+-----------------------------+
                          |  smaller request
                          v
              downstream model (Kimi / OpenAI / Anthropic)
                          |
              needs an omitted detail? -> retrieve_context
              tool fetches the original from SQLite,
              mid-request, and the model continues
```

Stage-by-stage deep dive: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Key properties:

- **Reversible.** Originals are stored (session-scoped) before compression.
  Omitted blocks leave a `[CONTEXT OMITTED - msg_N ...]` marker; the model can
  call the injected `retrieve_context` tool to recover them during the same
  request. Hidden, never deleted.
- **Pinned context is sacred.** The system prompt, the current turn,
  constraint messages ("must not..."), fresh tool results the model hasn't seen
  yet, and any multimodal/thinking content are always forwarded verbatim -
  even over budget (reported honestly as `over_budget`).
- **Truthful accounting.** The budgeter charges exactly what ships: original
  tokens for pinned blocks, compressed tokens for compacted ones, a fixed
  marker cost per omission. Estimated vs actual forwarded tokens match within
  a token on the demo trace (588 vs 589).
- **Fail-open everywhere.** Scorer down -> deterministic fallback scoring.
  Pipeline crash -> the original request passes through untouched, with
  `fail_open: true` in the diagnostics. Availability is never traded for
  compression.

---

## Quickstart

```powershell
git clone <this repo> && cd ChunkIT
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env        # fill in OPENAI_API_KEY or MOONSHOT_API_KEY
.\run-gateway.bat             # gateway on http://localhost:8000
```

Open **http://localhost:8000** - the live dashboard.

Works out of the box with no external credentials: Daytona processing runs in
local mode and scoring uses the deterministic fallback. Filling in the
Daytona/Nosana values upgrades those stages to the live services with zero
code changes (see [Configuration](#configuration)).

---

## Three ways to see it work

### 1. The dashboard (zero setup)

http://localhost:8000 -> **Load sample trace** -> **Send request**.

- **Session totals** - cumulative tokens/$ saved with a savings sparkline,
  request counts, average gateway overhead
- **Live traffic** - every request through the gateway, from any client,
  appearing in real time (all detail panels auto-fill from the newest request)
- **Compression provenance** - per-block: detected type, compressor used,
  token reduction, cache hits (the Daytona layer, visible)
- **Context decisions** - per-unit relevance scores and keep/compact/omit
  badges (the Nosana layer, visible)
- **Run baseline compare** - the same request full-context vs compressed,
  with *provider-measured* prompt tokens side by side

### 2. Any OpenAI-SDK agent (one line changed)

```powershell
.\.venv\Scripts\python.exe demo_agent.py               # streams through the gateway
.\.venv\Scripts\python.exe demo_agent.py --with-tools  # client tool calls pass through
```

`demo_agent.py` is an unmodified OpenAI SDK client - the only change is
`base_url`. The same applies to aider, Cursor custom endpoints, or any
OpenAI-compatible tool.

### 3. Claude Code itself

```powershell
$env:ANTHROPIC_BASE_URL = "http://localhost:8000"
claude
```

ChunkIT speaks the Anthropic Messages API natively: it compresses the session
history and forwards to the real Anthropic API **with Claude Code's own
login** - responses and streams relay back byte-for-byte. Real Claude models,
smaller bills. Watch the `[anthropic]` lines in the gateway terminal and the
dashboard's live panel while you chat.

---

## API

### `POST /v1/chat/completions` - OpenAI-compatible

Standard fields (`messages`, `stream`, `tools`, `tool_choice`, `temperature`,
`max_tokens`) plus ChunkIT extensions:

| Field | Default | Meaning |
|---|---|---|
| `target_tokens` | 2000 | context budget for the compressed request |
| `compression_enabled` | `true` | `false` = passthrough (fair baseline mode) |
| `session_id` | per-request | agents sharing an id share the context store and (on Kimi) the provider prompt cache |

Behavior guarantees for agent clients:

- `stream: true` returns OpenAI-style SSE; ChunkIT's internal
  `retrieve_context` rounds are handled invisibly mid-stream, and the tool is
  de-advertised once its round budget is spent so clients always receive a
  well-formed completion
- client-supplied `tools` pass through untouched; `retrieve_context` steps
  aside when they're present, and omission markers drop the tool instruction
- assistant `tool_calls` / `tool`-role histories survive compression
  structurally intact - only message *content* is ever rewritten
- responses are the upstream OpenAI body plus a `debug` object (score source,
  savings, latencies, per-unit decisions, per-block provenance - ids and
  counters only, never content)

### `POST /v1/messages` - Anthropic-compatible (Claude Code)

Format-preserving proxy: the request's message history is compressed, then
forwarded to `ANTHROPIC_UPSTREAM_URL` with the client's own auth headers;
responses and SSE streams return unmodified (diagnostics ride an SSE comment).
`system` and `tools` are forwarded byte-identical, markers are byte-stable
across turns (both protect the client's prompt cache), and messages carrying
`thinking`/`image`/`document`/`tool_use` blocks - including images nested in
tool results - are never rewritten. All other `/v1/*` paths (count_tokens,
models, ...) pass through verbatim.

### Observability

- `GET /dashboard` - the live dashboard
- `GET /activity` - sanitised recent-request feed + session totals (what the
  dashboard polls)
- `GET /health` - gateway + Daytona worker-pool status

---

## Configuration

Everything is environment-driven (`.env`, names in `.env.example`; no secrets
are ever committed):

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `openai` | downstream brain: `openai` or `kimi` |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | - / `gpt-4o-mini` | OpenAI provider |
| `MOONSHOT_API_KEY` / `KIMI_MODEL` / `KIMI_BASE_URL` | - | Kimi provider (`prompt_cache_key` enabled) |
| `DAYTONA_BACKEND` | `local` | `remote`/`daytona` + `DAYTONA_API_KEY` = sandboxed worker pool |
| `NOSANA_ENABLED` | `true` | try the live scorer first |
| `NOSANA_SCORER_URL` / `NOSANA_SCORER_TOKEN` | empty | live reranker endpoint; empty = deterministic fallback |
| `NOSANA_TIMEOUT_SECONDS` / `NOSANA_MAX_RETRIES` / `NOSANA_REQUEST_BATCH_SIZE` | 5 / 2 / 128 | scorer client behavior |
| `CHUNKIT_TARGET_TOKENS` | 2000 | default budget, OpenAI front |
| `CHUNKIT_ANTHROPIC_TARGET_TOKENS` | 6000 | default budget, Anthropic front |
| `CHUNKIT_MAX_RETRIEVAL_ROUNDS` | 2 | retrieve_context round cap |
| `CHUNKIT_UPSTREAM_TIMEOUT_SECONDS` | 30 | downstream model timeout |
| `CHUNKIT_PRICE_PER_MTOK` | 15 | list price used for the $-saved tile |
| `ANTHROPIC_UPSTREAM_URL` | `https://api.anthropic.com` | Anthropic front upstream |
| `CHUNKIT_DB_PATH` | `data/sessions.db` | SQLite context store |

**Deploying the Nosana scorer** (flips `score_source` from `fallback` to
`nosana`): build and push `nosana-service/` (a CrossEncoder reranker with
`GET /health` + batch `POST /score`), create the deployment from
`nosana/deployment.template.json`, and fill in `NOSANA_SCORER_URL` +
`NOSANA_SCORER_TOKEN`. Full manual runbook: [nosana/DEPLOYMENT.md](nosana/DEPLOYMENT.md).

---

## Benchmarks

With the gateway running:

```powershell
.\.venv\Scripts\python.exe evals\benchmark.py
```

Alternating full-context / compressed trials (warm-up excluded), reporting
ChunkIT's estimates **and** provider-measured prompt tokens, written to
`evals/results/`. Latest measured on the bundled 15-message coding-agent
trace (`target_tokens=400`, gpt-4o-mini):

| | full context | compressed |
|---|---|---|
| provider prompt tokens | 1,515 | **801 (-47%)** |
| est. forwarded tokens | 1,267 | 589 |
| answer correctness | correct - identifies the auth-refresh bug | correct - identifies the auth-refresh bug |

Numbers vary slightly per run; the benchmark never hardcodes results.

---

## Testing

```powershell
python -m pytest -q          # 112 tests, no external credentials needed
```

Coverage spans the scorer client (retries, strict ID validation, batching),
fallback determinism, the exact priority formula, tier selection and marker
accounting, structure preservation for tool histories, streaming (including
internal-retrieval invisibility and round exhaustion), the Anthropic front
(auth forwarding, byte-stable markers, nested-image safety), session
isolation, fail-open paths, and no-context-leak regressions. Live-endpoint
tests are opt-in via `RUN_LIVE_NOSANA_TESTS=1`. Legacy smoke scripts
(`tests/test_parser.py`, `test_pipeline.py`, `test_retrieval.py`) still run
standalone, plus `tests/test_daytona.py` for the compression pipeline.

---

## Project structure

```
app/
  gateway/        routes (OpenAI front), anthropic_routes (Claude Code front),
                  parser/pinning, budgeter, assembler, activity feed, theming
  daytona/        prepare_context_blocks, local + remote worker pool
  nosana/         scorer client, wire schemas, deterministic fallback
  scoring/        priority formula, remote->fallback orchestration
  selection/      rank_and_select - scoring glued to the budgeter
  kimi/           downstream client: streaming, tool passthrough, retrieval loop
  session/        SQLite store for original context (session-scoped)
compressors/      type detection, secret redaction, log/JSON/diff/search/text
workers/          worker API (local) and sandbox server (Daytona remote)
nosana-service/   deployable GPU reranker (FastAPI + CrossEncoder + Dockerfile)
web/              the dashboard (single self-contained page)
evals/            benchmark + sample agent trace
tests/            pytest suite + legacy smoke scripts
docs/             architecture and development log
demo_agent.py     OpenAI-SDK demo client
```
