from __future__ import annotations

import re
from collections import defaultdict

COMPRESSOR_VERSION = "search_v1"
SEARCH_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+)(?::(?P<column>\d+))?:(?P<text>.*)$")


def compress_search_content(content: str) -> list[dict]:
    groups: dict[str, list[tuple[int, str]]] = defaultdict(list)
    cursor = 0
    first_offset_by_file: dict[str, int] = {}

    for raw_line in content.splitlines(True):
        match = SEARCH_RE.match(raw_line.rstrip("\r\n"))
        if match:
            path = match.group("file")
            line_no = int(match.group("line"))
            snippet = match.group("text").strip()
            groups[path].append((line_no, snippet))
            first_offset_by_file.setdefault(path, cursor)
        cursor += len(raw_line)

    units: list[dict] = []
    for path, matches in sorted(groups.items()):
        deduped: list[str] = []
        seen: set[str] = set()
        for line_no, snippet in matches:
            entry = f"{path}:{line_no}: {snippet}"
            if entry not in seen:
                deduped.append(entry)
                seen.add(entry)
        preview = "\n".join(deduped[:8])
        if len(deduped) > 8:
            preview += f"\n... truncated {len(deduped) - 8} additional matches"
        units.append(
            {
                "text": f"search_results file={path} matches={len(matches)}\n{preview}",
                "source_start": first_offset_by_file[path],
                "source_end": first_offset_by_file[path] + len(preview),
            }
        )
    return units
