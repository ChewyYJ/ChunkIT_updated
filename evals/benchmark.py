"""Baseline-vs-compressed benchmark against a running ChunkIT gateway.

Sends the same trace with compression disabled (full context) and enabled,
in alternating order, and reports estimated + provider-measured token usage,
latencies and retrieval counts. The first run is a warm-up and is excluded.

Usage:
    python evals/benchmark.py [--gateway-url http://localhost:8000]
                              [--trials 2] [--target-tokens 400]
                              [--trace evals/sample_trace.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.gateway.termcolor import BOLD, BRIGHT_GREEN, DIM, GREEN, RESET, WHITE, YELLOW


def run_once(client: httpx.Client, gateway: str, messages: list[dict],
             target_tokens: int, compression: bool) -> dict:
    started = time.perf_counter()
    resp = client.post(
        f"{gateway}/v1/chat/completions",
        json={
            "messages": messages,
            "target_tokens": target_tokens,
            "compression_enabled": compression,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    debug = data.get("debug", {})
    usage = data.get("usage") or {}
    return {
        "mode": "compressed" if compression else "full",
        "wall_ms": round((time.perf_counter() - started) * 1000, 1),
        "score_source": debug.get("score_source"),
        "fallback_reason": debug.get("fallback_reason"),
        "fail_open": debug.get("fail_open"),
        "estimated_forwarded_tokens": debug.get("forwarded_tokens"),
        "estimated_original_tokens": debug.get("original_tokens"),
        "estimated_savings_percent": debug.get("savings_percent"),
        "provider_prompt_tokens": usage.get("prompt_tokens"),
        "provider_completion_tokens": usage.get("completion_tokens"),
        "cached_tokens": debug.get("cached_tokens"),
        "nosana_ms": debug.get("nosana_ms"),
        "kimi_ms": debug.get("kimi_ms"),
        "pipeline_ms": debug.get("pipeline_ms"),
        "retrieval_count": debug.get("retrieval_count"),
        "omitted_blocks": debug.get("omitted_blocks"),
        "answer_preview": (
            (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")[:200]
        ),
    }


def summarize(rows: list[dict], key: str) -> dict:
    values_by_mode: dict[str, list[float]] = {"full": [], "compressed": []}
    for row in rows:
        value = row.get(key)
        if isinstance(value, (int, float)):
            values_by_mode[row["mode"]].append(value)
    return {
        mode: round(statistics.mean(vals), 1) if vals else None
        for mode, vals in values_by_mode.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ChunkIT baseline benchmark")
    parser.add_argument("--gateway-url", default="http://localhost:8000")
    parser.add_argument("--trials", type=int, default=2,
                        help="paired trials per mode after the warm-up")
    parser.add_argument("--target-tokens", type=int, default=400)
    parser.add_argument("--trace", default=os.path.join(os.path.dirname(__file__), "sample_trace.json"))
    parser.add_argument("--out-dir", default=os.path.join(os.path.dirname(__file__), "results"))
    args = parser.parse_args()

    with open(args.trace, encoding="utf-8") as f:
        messages = json.load(f)

    print(f"{GREEN}{BOLD}[chunkit]{RESET} benchmark — {len(messages)} messages, "
          f"target={args.target_tokens}, trials={args.trials} per mode")

    rows: list[dict] = []
    with httpx.Client() as client:
        print(f"{DIM}warm-up run (excluded from results)…{RESET}")
        run_once(client, args.gateway_url, messages, args.target_tokens, compression=True)

        for trial in range(args.trials):
            # Alternate order so cache state never favours one mode.
            order = [False, True] if trial % 2 == 0 else [True, False]
            for compression in order:
                row = run_once(client, args.gateway_url, messages, args.target_tokens, compression)
                row["trial"] = trial
                rows.append(row)
                source = row["score_source"]
                tone = BRIGHT_GREEN if source in ("nosana", "disabled") else YELLOW
                print(f"  trial {trial} {WHITE}{row['mode']:<10}{RESET} "
                      f"est_fwd={row['estimated_forwarded_tokens']} "
                      f"prompt={row['provider_prompt_tokens']} "
                      f"kimi={row['kimi_ms']}ms source={tone}{source}{RESET}")

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = os.path.join(args.out_dir, f"benchmark_{stamp}.json")
    csv_path = os.path.join(args.out_dir, f"benchmark_{stamp}.csv")

    est = summarize(rows, "estimated_forwarded_tokens")
    prompt = summarize(rows, "provider_prompt_tokens")
    kimi = summarize(rows, "kimi_ms")
    wall = summarize(rows, "wall_ms")

    def pct_saved(pair: dict) -> float | None:
        if pair.get("full") and pair.get("compressed") is not None:
            return round((pair["full"] - pair["compressed"]) / pair["full"] * 100, 1)
        return None

    summary = {
        "trials_per_mode": args.trials,
        "target_tokens": args.target_tokens,
        "estimated_forwarded_tokens": est,
        "estimated_savings_percent": pct_saved(est),
        "provider_prompt_tokens": prompt,
        "provider_prompt_savings_percent": pct_saved(prompt),
        "kimi_ms": kimi,
        "wall_ms": wall,
        "score_sources": sorted({r["score_source"] for r in rows if r["mode"] == "compressed"}),
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "runs": rows}, f, indent=2)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print()
    print(f"{GREEN}{BOLD}[chunkit]{RESET} {BOLD}results (mean across trials){RESET}")
    print(f"  estimated forwarded tokens : full={est['full']}  compressed={est['compressed']}  "
          f"{BRIGHT_GREEN}saved={summary['estimated_savings_percent']}%{RESET}")
    if prompt["full"] is not None:
        print(f"  provider prompt tokens     : full={prompt['full']}  compressed={prompt['compressed']}  "
              f"{BRIGHT_GREEN}saved={summary['provider_prompt_savings_percent']}%{RESET}")
    print(f"  upstream latency (ms)      : full={kimi['full']}  compressed={kimi['compressed']}")
    print(f"  end-to-end wall (ms)       : full={wall['full']}  compressed={wall['compressed']}")
    print(f"  score sources used         : {summary['score_sources']}")
    print(f"{DIM}  written: {json_path}{RESET}")
    print(f"{DIM}  written: {csv_path}{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
