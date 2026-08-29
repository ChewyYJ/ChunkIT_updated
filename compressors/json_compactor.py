from __future__ import annotations

import json
from collections import Counter

COMPRESSOR_VERSION = "json_v2"

KEY_PRIORITY = ("id", "name", "type", "status", "role", "timestamp", "message")


def compress_json_content(content: str) -> list[dict]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return []

    units: list[dict] = []
    overview = _build_overview(parsed)
    units.append({"text": overview, "source_start": 0, "source_end": len(content)})
    exception_unit = _extract_exceptional_record(parsed)
    if exception_unit:
        units.append({"text": exception_unit, "source_start": 0, "source_end": len(content)})
    return units


def _build_overview(value) -> str:
    compacted = _compact_value(value)
    return json.dumps(compacted, ensure_ascii=True, indent=2, sort_keys=True)


def _compact_value(value):
    if isinstance(value, dict):
        result = {"_kind": "object", "keys": list(value.keys())[:20]}
        for key in KEY_PRIORITY:
            if key in value:
                result[key] = _compact_value(value[key])
        return result
    if isinstance(value, list):
        sample = [_compact_value(item) for item in value[:3]]
        item_types = sorted({type(item).__name__ for item in value})
        result = {"_kind": "list", "count": len(value), "item_types": item_types, "sample": sample}
        if value and all(isinstance(item, dict) for item in value):
            status_counts = Counter(str(item.get("status")) for item in value if "status" in item)
            if status_counts:
                result["status_counts"] = dict(status_counts.most_common(10))
        return result
    if isinstance(value, str) and len(value) > 160:
        return value[:157] + "..."
    return value


def _extract_exceptional_record(value) -> str | None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                lowered = json.dumps(item).lower()
                if any(marker in lowered for marker in ("error", "failed", "exception")):
                    return "exception_record:\n" + json.dumps(item, ensure_ascii=True, indent=2, sort_keys=True)
        return None
    if isinstance(value, dict):
        lowered = json.dumps(value).lower()
        if any(marker in lowered for marker in ("error", "failed", "exception")):
            return "exception_record:\n" + json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)
    return None
