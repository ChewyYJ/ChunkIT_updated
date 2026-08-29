# Notes

## Phase 6 — Hardening
Phase 6 hardening complete — validation, fail-open, upstream error
handling (502), 30s timeout, run_id/timing, prompt_cache_key (kimi-only,
no-op on openai).

## Person 3 — Nosana scoring, selection, metrics (perynn/Nosana)
- app/nosana/: settings, wire schemas, async batch client (retries, strict
  ID validation), deterministic lexical fallback.
- app/scoring/ + app/selection/: 0.65/0.15/0.10/0.10 priority formula and
  rank_and_select wrapper over the existing budgeter.
- Budgeter hardened: deterministic tie-breaks, omission-marker token
  accounting, over-budget reporting.
- Session-scoped SQLite keys (fixes cross-session retrieval collision);
  retrieve_context advertised only when something was omitted.
- Debug adds score_source / nosana_ms / fallback_reason / token savings /
  sanitised unit scores; compression_enabled=false gives a fair baseline.
- nosana-service/: CrossEncoder reranker (GET /health, POST /score),
  Dockerfile + nosana/deployment.template.json; manual steps in
  nosana/DEPLOYMENT.md.
- web/dashboard.html (served at /dashboard) + evals/benchmark.py.
- Scorer URL/token empty -> deterministic fallback; fill NOSANA_SCORER_URL
  + NOSANA_SCORER_TOKEN in .env to flip to live Nosana scoring.

## Compact tier (post-merge, single-owner)
Budgeter/assembler now implement true KEEP / COMPACT / OMIT: pinned blocks
forward verbatim (charged original tokens), kept non-pinned blocks forward
their compressed unit text (charged compressed tokens), omitted blocks get
markers. Partial omissions append a retrievable note; retrieve_context is
advertised on any omission. Dashboard shows three-way decisions, per-block
compression provenance, a latency breakdown, and a live streaming mode.
