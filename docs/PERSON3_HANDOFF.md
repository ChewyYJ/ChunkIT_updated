> **Historical snapshot (2026-08-29).** This is Person 2's handoff to
> Person 3, preserved as a team artifact. The work it describes as "still
> mocked" (relevance scoring, selection) has since been completed — the
> gateway now runs the full live pipeline. See the top-level README.

# Person 3 Handoff

Date: 2026-08-29
Owner: Person 2
Branch: `role2/context-compression-remote-workers`

## What Was Accomplished

Person 2's Daytona context-processing pipeline is now real, wired, and end-to-end tested.

Completed:
- `prepare_context_blocks(blocks)` is implemented and used by the gateway.
- Local context preparation works.
- Remote Daytona worker mode works.
- Remote mode safely falls back to local mode if Daytona fails.
- Secret redaction happens before remote dispatch.
- Session-scoped storage and retrieval are fixed.
- Retrieval no longer leaks context across sessions with the same `msg_*` IDs.
- Compression routing now supports `json`, `log`, `search`, `diff`, `code`, and `text` internal paths.
- Actual processed type is stored in metadata.
- Per-block provenance and debug metrics are exposed to the gateway.
- README documentation was expanded.
- Regression tests were added for the Person 2 guarantees.

## What Person 3 Can Assume

The shared contract in [shared/schemas.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/shared/schemas.py) was preserved.

Person 3 can continue to build against:
- `ContextBlock`
- `ContextUnit`
- `ScoredUnit`
- `SelectionPlan`
- `RunStats`

Current pipeline shape:

```text
messages
  -> ContextBlock[]
  -> Person 2 prepare_context_blocks(...)
  -> ContextUnit[]
  -> Person 3 scoring
  -> SelectionPlan
  -> assembler
  -> LLM
  -> optional retrieve_context loop
```

## Where Person 3 Plugs In

Right now the gateway still uses mock scoring:
- [app/gateway/routes.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/app/gateway/routes.py)
- [shared/schemas.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/shared/schemas.py)

Current placeholder:
- `mock_score_units(units)`

Person 3 should replace that mocked scoring stage with the real relevance service, while keeping the output shape compatible with:
- `list[ScoredUnit]`
- `SelectionPlan`

## Files Created By Person 2

New files added for Person 2:
- [compressors/diffs.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/compressors/diffs.py)
- [compressors/search.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/compressors/search.py)
- [PERSON3_HANDOFF.md](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/PERSON3_HANDOFF.md)

## Files Updated By Person 2

Core pipeline:
- [app/daytona/cache.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/app/daytona/cache.py)
- [app/daytona/client.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/app/daytona/client.py)
- [app/daytona/remote.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/app/daytona/remote.py)
- [app/daytona/worker.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/app/daytona/worker.py)

Gateway and retrieval wiring:
- [app/gateway/routes.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/app/gateway/routes.py)
- [app/kimi/client.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/app/kimi/client.py)
- [app/kimi/tools.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/app/kimi/tools.py)
- [app/session/store.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/app/session/store.py)

Compression and detection:
- [compressors/__init__.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/compressors/__init__.py)
- [compressors/common.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/compressors/common.py)
- [compressors/detect.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/compressors/detect.py)
- [compressors/json_compactor.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/compressors/json_compactor.py)
- [compressors/logs.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/compressors/logs.py)

Worker entrypoints:
- [workers/sandbox_prepare_server.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/workers/sandbox_prepare_server.py)
- [workers/service.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/workers/service.py)

Docs and tests:
- [README.md](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/README.md)
- [tests/test_daytona.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/tests/test_daytona.py)
- [tests/test_retrieval.py](/Users/oliviachew/Documents/ChatGPT/ChunkIT/ChunkIT/tests/test_retrieval.py)

## Important Behavior Person 3 Should Know

- `ContextUnit.content_type` still stays within the shared schema contract: `log | json | code | text`.
- Internal Person 2 routing is richer than the public schema:
  - `diff` is routed and stored internally but surfaced as `code`
  - `search` is routed and stored internally but surfaced as `text`
- Retrieval is session-scoped using the current run/session ID.
- Omitted blocks are reversible through the `retrieve_context` tool.
- Gateway debug payload now includes `daytona_blocks`, which contains per-block processing metadata.
- If Daytona remote fails, processing falls back to local mode instead of breaking the request.

## Known Boundary

Person 2 did not replace Person 3 logic.

Still mocked:
- relevance scoring
- advanced selection logic beyond the current simple budgeter

That is the intended handoff area for Person 3.

## Suggested First Steps For Person 3

1. Replace `mock_score_units(...)` with the real scoring service.
2. Keep returning `ScoredUnit` objects compatible with the shared schema.
3. Reuse Person 2 metadata such as:
   - `content_type`
   - `structural_priority`
   - block recency/message position
   - gateway debug/provenance data if helpful
4. Verify scoring against omitted-context retrieval so low-ranked but needed context can still be recovered.
5. Do not change Person 2 storage or retrieval behavior unless a real integration bug appears.
