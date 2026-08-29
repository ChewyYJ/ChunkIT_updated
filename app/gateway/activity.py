"""In-memory feed of recent gateway requests for the dashboard's live view.

Entries are sanitised summaries (counters, ids, percentages, per-block
provenance) — never message content. Everything is process-local and resets
on restart: this is the "Session" view.
"""

from __future__ import annotations

import os
import time
from collections import deque
from threading import Lock

_MAX_ENTRIES = 50
_MAX_HISTORY = 600

_entries: deque[dict] = deque(maxlen=_MAX_ENTRIES)
_history: deque[tuple[float, int]] = deque(maxlen=_MAX_HISTORY)  # (ts, cumulative saved)
_totals = {
    "started": time.time(),
    "requests": 0,
    "claude_requests": 0,
    "openai_requests": 0,
    "tokens_saved": 0,
    "original_tokens": 0,
    "forwarded_tokens": 0,
    "pipeline_ms_sum": 0.0,
    "fail_opens": 0,
}
_lock = Lock()


def record(entry: dict) -> None:
    now = time.time()
    with _lock:
        _entries.append({"ts": now, **entry})
        _totals["requests"] += 1
        if entry.get("api") == "anthropic":
            _totals["claude_requests"] += 1
        else:
            _totals["openai_requests"] += 1
        _totals["tokens_saved"] += int(entry.get("saved") or 0)
        _totals["original_tokens"] += int(entry.get("original_tokens") or 0)
        _totals["forwarded_tokens"] += int(entry.get("forwarded_tokens") or 0)
        _totals["pipeline_ms_sum"] += float(entry.get("pipeline_ms") or 0.0)
        if entry.get("fail_open"):
            _totals["fail_opens"] += 1
        _history.append((now, _totals["tokens_saved"]))


def recent() -> list[dict]:
    with _lock:
        return list(_entries)[::-1]  # newest first


def totals() -> dict:
    with _lock:
        snapshot = dict(_totals)
        history = [{"ts": ts, "saved": saved} for ts, saved in _history]
    requests = max(1, snapshot["requests"])
    original = snapshot["original_tokens"]
    snapshot["savings_percent"] = round(
        snapshot["tokens_saved"] / original * 100, 1
    ) if original else 0.0
    snapshot["avg_pipeline_ms"] = round(snapshot.pop("pipeline_ms_sum") / requests, 1)
    # Estimated $ saved at a configurable input list price.
    price = float(os.getenv("CHUNKIT_PRICE_PER_MTOK", "15"))
    snapshot["price_per_mtok"] = price
    snapshot["dollars_saved"] = round(snapshot["tokens_saved"] / 1_000_000 * price, 2)
    snapshot["history"] = history
    return snapshot
