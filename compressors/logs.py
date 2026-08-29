from __future__ import annotations

from collections import Counter
import re

COMPRESSOR_VERSION = "log_v2"

IMPORTANT_MARKERS = ("error", "warn", "warning", "exception", "failed", "fatal", "traceback")
FINAL_MARKERS = ("completed", "finished", "success", "done", "exit", "status")
VOLATILE_PATTERNS = (
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"), "<TIME>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{27}\b", re.I), "<UUID>"),
    (re.compile(r"\b(?:request|job|run|pid|id)\s+[0-9a-f-]{3,}\b", re.I), lambda m: m.group(0).split()[0] + " <ID>"),
    (re.compile(r"\b[0-9]{4,}\b"), "<NUM>"),
)


def compress_log_content(content: str) -> list[dict]:
    lines = _line_ranges(content)
    if not lines:
        return []

    normalized = [_normalize_line(line["text"]) for line in lines]
    counts = Counter(normalized)
    seen_normalized: set[str] = set()
    units: list[dict] = []

    for item, normalized_line in zip(lines, normalized):
        line = item["text"]
        lowered = line.lower()
        if any(marker in lowered for marker in IMPORTANT_MARKERS):
            units.append(
                {
                    "text": line,
                    "source_start": item["start"],
                    "source_end": item["end"],
                }
            )
            continue

        if counts[normalized_line] > 1:
            if normalized_line not in seen_normalized:
                units.append(
                    {
                        "text": f"{normalized_line} x{counts[normalized_line]}",
                        "source_start": item["start"],
                        "source_end": item["end"],
                    }
                )
                seen_normalized.add(normalized_line)
            continue

        if any(marker in lowered for marker in FINAL_MARKERS):
            units.append(
                {
                    "text": line,
                    "source_start": item["start"],
                    "source_end": item["end"],
                }
            )

    if not units:
        units = [
            {"text": item["text"], "source_start": item["start"], "source_end": item["end"]}
            for item in lines[: min(8, len(lines))]
        ]

    return units


def _normalize_line(line: str) -> str:
    normalized = line
    for pattern, replacement in VOLATILE_PATTERNS:
        if callable(replacement):
            normalized = pattern.sub(replacement, normalized)
        else:
            normalized = pattern.sub(replacement, normalized)
    return normalized


def _line_ranges(content: str) -> list[dict]:
    lines: list[dict] = []
    cursor = 0
    for raw_line in content.splitlines(True):
        text = raw_line.rstrip("\r\n")
        if text.strip():
            lines.append({"text": text, "start": cursor, "end": cursor + len(raw_line)})
        cursor += len(raw_line)
    return lines
