from __future__ import annotations

import re

COMPRESSOR_VERSION = "diff_v1"
FILE_START_RE = re.compile(r"(?m)^diff --git ")


def compress_diff_content(content: str) -> list[dict]:
    matches = list(FILE_START_RE.finditer(content))
    if not matches:
        return [{"text": content, "source_start": 0, "source_end": len(content)}]

    units: list[dict] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        section = content[start:end].strip()
        if section:
            units.append({"text": section, "source_start": start, "source_end": end})
    return units
