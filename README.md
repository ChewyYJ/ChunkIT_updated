# ChunkIT

**Give agents less context without making them less capable.**

## Overview

AI agents re-send their entire conversation history to the model on every
turn, even though most of it is stale by the time it's sent again — and
that gets billed as tokens every single time. ChunkIT is a gateway that
sits between an AI agent and its LLM provider, compressing that history
before it reaches the model: it keeps what matters to the current question,
shrinks or hides the rest, and forwards a much smaller request. Omitted
content can be retrieved by the model during the request while its context
store is available. On Vercel that store is explicitly temporary.

Measured on our demo trace, provider-counted: **1,515 -> 801 prompt tokens
(~47% saved)** with the compressed answer matching the full-context answer.
Gateway overhead: **~80 ms** per request.

## Features

- **Drop-in gateway** - OpenAI-compatible and Anthropic-compatible endpoints, so any agent (Claude Code, an OpenAI-SDK client, etc.) can point at it with a one-line config change
- **Content-aware compression** - detects logs, JSON, diffs, and search output, and compresses each differently
- **Relevance-based selection** - scores context against the current question, not just age, to decide what to keep
- **Retrievable context** - omitted content is stored and can be retrieved by the model mid-request; Vercel storage is temporary
- **Compression fallback** - if a compression stage breaks, the authenticated request still goes through unmodified
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

- **Reversible within the available context store.** Originals are stored (session-scoped) before compression.
  Omitted blocks leave a `[CONTEXT OMITTED - msg_N ...]` marker; the model can
  call the injected `retrieve_context` tool to recover them during the same
  request. On Vercel this store is temporary; see the deployment limitations below.
- **Pinned context is sacred.** The system prompt, the current turn,
  constraint messages ("must not..."), fresh tool results the model hasn't seen
  yet, and any multimodal/thinking content are always forwarded verbatim -
  even over budget (reported honestly as `over_budget`).
- **Truthful accounting.** The budgeter charges exactly what ships: original
  tokens for pinned blocks, compressed tokens for compacted ones, a fixed
  marker cost per omission. Estimated vs actual forwarded tokens match within
  a token on the demo trace (588 vs 589).
- **Compression fails open; authentication fails closed.** Scorer down -> deterministic fallback scoring.
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
copy .env.example .env        # configure gateway access + one provider key
.\run-gateway.bat             # gateway on http://localhost:8000
```

Open **http://localhost:8000** - the live dashboard.

Set `CHUNKIT_GATEWAY_API_KEY` to a long random access key, and configure
`OPENAI_API_KEY` or `MOONSHOT_API_KEY` for real model responses. Sign into the
dashboard with the gateway access key; provider credentials remain server-side.
Local compression and deterministic scoring need no external service keys,
but sending requests and baseline comparisons do require the chosen LLM provider.
For deliberately unauthenticated local development only, set
`CHUNKIT_AUTH_DISABLED=true`; Vercel ignores this switch.

To use remote Daytona outside Vercel, install `requirements-daytona.txt` and
configure the Daytona environment variables. Nosana scoring is optional and
falls back locally when its endpoint is not configured.

---

## Three ways to see it work

### 1. The authenticated dashboard

http://localhost:8000 -> **Sign in** -> **Load sample trace** -> **Send request**.

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

### 2. An OpenAI-SDK agent

Set the SDK's `base_url` to the gateway's `/v1` URL and its `api_key` to your
gateway access key. The bundled demo below uses a fixed test key and therefore
requires the explicit local-only `CHUNKIT_AUTH_DISABLED=true` setting.

```powershell
.\.venv\Scripts\python.exe demo_agent.py               # streams through the gateway
.\.venv\Scripts\python.exe demo_agent.py --with-tools  # client tool calls pass through
```

`demo_agent.py` uses the standard OpenAI SDK. The same base URL and gateway
key configuration applies to compatible clients such as aider and Cursor.

### 3. Claude Code itself

Authenticated clients must also send `X-ChunkIT-API-Key` with the gateway
access key while retaining their Anthropic provider credentials. The minimal
local command below works only with the explicit local authentication opt-out.

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
- `GET /activity` - authenticated, sanitised recent-request feed + session totals (what the
  dashboard polls)
- `GET /health` - public minimal liveness response (`{"status":"ok"}`)
- `GET /config` - authenticated provider readiness and storage limitations, without credentials

### Authentication

All data/API routes, including `/activity`, `/config`, `/v1/*`, `/docs` and
`/openapi.json`, require the gateway access key. The dashboard HTML is a
public login shell; it fetches no gateway data before authentication.

- `GET /auth/status` returns `authenticated`, `required` and `configured` booleans.
- `POST /auth/login` accepts JSON `{ "api_key": "..." }` and creates an
  eight-hour signed `HttpOnly`, `SameSite=Strict` cookie. Cookies are `Secure`
  on HTTPS and Vercel. The access key is not returned or stored in browser storage.
- `POST /auth/logout` clears the browser cookie. Rotate the gateway key to
  invalidate **all** previously issued cookies, including copied cookies.
- API clients send `Authorization: Bearer <gateway access key>`.
- Anthropic clients should send `X-ChunkIT-API-Key: <gateway access key>`
  alongside their own Anthropic authorization. ChunkIT strips its gateway
  header and cookies before forwarding. A gateway bearer is also stripped
  from upstream requests; it is never an Anthropic credential.

For example, an OpenAI SDK client can set `api_key` to the gateway access
key and `base_url` to `https://your-deployment.vercel.app/v1`. The downstream
provider key comes only from server configuration. Local demo scripts that
do not send a gateway key require the explicit local-only auth opt-out.

This is a single-owner gateway. Everyone given the access key can see the
same activity feed and use the configured provider. It does not offer
individual accounts, per-user quotas, or persistent audit history.

## Vercel deployment

Deploy the repository root with the **FastAPI** framework. `app/main.py:app`
is the entrypoint, `.python-version` selects Python 3.12, and the dashboard
has no build command. Vercel installs `requirements.txt`; optional Daytona
and test packages are intentionally excluded. `vercel.json` includes the
dashboard file and sets a 300-second function duration.

Configure these server environment variables before deploying:

| Variable | Required value |
| --- | --- |
| `CHUNKIT_GATEWAY_API_KEY` | A randomly generated access key of at least 32 characters |
| `LLM_PROVIDER` | `openai` or `kimi` |
| `OPENAI_API_KEY` or `MOONSHOT_API_KEY` | Credential for the selected provider |
| `OPENAI_MODEL` or `KIMI_MODEL` | A model enabled for that provider account |
| `DAYTONA_BACKEND` | `local` (remote sandbox lifecycle is always disabled on Vercel) |
| `NOSANA_ENABLED` | `false`, unless a separate live scorer is configured |

Missing or short gateway keys return **503** for protected routes on Vercel.
They never enable anonymous access. Provider credentials and gateway keys
must be entered through Vercel's environment-variable settings, never committed.

**Storage is explicitly temporary on Vercel.** SQLite defaults to
`/tmp/chunkit/sessions.db`. It is not shared between instances, can disappear
between requests, and is not durable across restarts or deployments. Context
retrieval within one in-flight request uses that instance's store, but reuse
across requests is not guaranteed. Activity totals and the feed are also
instance-local. The dashboard displays this limitation after login. This
configuration is suitable for an owner's live demonstration, not a durable
multi-tenant agent service. Do not advertise persistent sessions.

Durable storage requires a separately provisioned database plus a tested
adapter. Turso's official Python SDK supports remote `libsql` or
`turso_serverless` connections, but neither is enabled in this release; no
database credentials were supplied. Merely pointing the SQLite path at `/tmp`
does not add persistence. [Turso Python documentation](https://docs.turso.tech/sdk/python/quickstart)

Before promotion, verify sign-in, rejection of unauthenticated API requests,
a real provider response, streaming, baseline comparison, and logout against
the target deployment. The automated test suite uses fake credentials and
stubbed providers; it does not prove live provider access or billing readiness.

---

## Configuration

Everything is environment-driven (`.env`, names in `.env.example`; no secrets
are ever committed):

| Variable | Default | Purpose |
|---|---|---|
| `CHUNKIT_GATEWAY_API_KEY` | empty (access denied) | Owner access key; minimum 32 characters on Vercel |
| `CHUNKIT_AUTH_DISABLED` | `false` | Explicit local-only authentication opt-out; ignored on Vercel |
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
| `CHUNKIT_DB_PATH` | `data/sessions.db` locally; `/tmp/chunkit/sessions.db` on Vercel | SQLite context store; ephemeral on Vercel |

**Deploying the Nosana scorer** (flips `score_source` from `fallback` to
`nosana`): build and push `nosana-service/` (a CrossEncoder reranker with
`GET /health` + batch `POST /score`), create the deployment from
`nosana/deployment.template.json`, and fill in `NOSANA_SCORER_URL` +
`NOSANA_SCORER_TOKEN`. Full manual runbook: [nosana/DEPLOYMENT.md](nosana/DEPLOYMENT.md).

---

## Benchmarks

With the gateway running locally with the explicit authentication opt-out
(the bundled benchmark does not supply a gateway access key):

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
pip install -r requirements-dev.txt
python -m pytest -q          # no external credentials needed
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
