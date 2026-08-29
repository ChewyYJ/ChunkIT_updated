from __future__ import annotations

import json
import re
from typing import Literal

from shared.schemas import ContentType

ProcessingRoute = Literal["log", "json", "search", "diff", "code", "text"]

LOG_LINE_RE = re.compile(
    r"(?im)^(?:\d{4}-\d{2}-\d{2}[ t]\d{2}:\d{2}:\d{2}|\[[A-Z]+\]|(?:INFO|WARN|WARNING|ERROR|DEBUG|TRACE)\b)"
)
CODE_HINT_RE = re.compile(
    r"(?m)^\s*(?:def |class |function |\{|\}|import |from |const |let |var |if __name__ == [\"'])"
)
SEARCH_LINE_RE = re.compile(r"(?m)^[^\s:][^:\n]*:\d+(?::\d+)?:.+$")
DIFF_RE = re.compile(r"(?ms)^(?:diff --git|---\s+[^\n]+\n\+\+\+\s+[^\n]+)")


def detect_content_type(content: str, hint: ContentType | None = None) -> ContentType:
    route = detect_processing_route(content, hint)
    return route_to_schema_content_type(route)

 
def detect_processing_route(content: str, hint: ContentType | None = None) -> ProcessingRoute:
    stripped = content.strip()
    if not stripped:
        return "text"

    if hint == "json" or _looks_like_json(stripped):
        return "json"
    if DIFF_RE.search(content):
        return "diff"
    if LOG_LINE_RE.search(content):
        return "log"
    if SEARCH_LINE_RE.search(content):
        return "search"
    if hint == "code" or CODE_HINT_RE.search(content):
        return "code"
    return "text"


def route_to_schema_content_type(route: ProcessingRoute) -> ContentType:
    if route in {"log", "json", "code"}:
        return route
    if route == "diff":
        return "code"
    return "text"


def _looks_like_json(content: str) -> bool:
    if content[0] not in "[{":
        return False
    try:
        json.loads(content)
        return True
    except json.JSONDecodeError:
        return False
