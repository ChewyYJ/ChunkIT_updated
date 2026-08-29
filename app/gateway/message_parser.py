import re

from shared.schemas import ContextBlock

PINNED_KEYWORDS = ("do not", "don't change", "must not", "constraint:", "never")

# Word-boundary matching so "never" does not pin "nevertheless" and
# "do not" does not pin "do note".
_PINNED_KEYWORD_RE = re.compile(r"\b(?:do not|don't change|must not|never)\b|\bconstraint:")


def has_pinned_keyword(text: str) -> bool:
    return bool(_PINNED_KEYWORD_RE.search(text.lower()))


def _current_tool_round(messages: list[dict]) -> set[int]:
    """Indexes of the trailing tool-result run plus the assistant message
    whose tool_calls produced it. These results may never have been seen by
    the model yet, so they must not be compressed on this request."""
    indexes: set[int] = set()
    j = len(messages) - 1
    while j >= 0 and messages[j].get("role") == "tool":
        indexes.add(j)
        j -= 1
    if indexes and j >= 0 and messages[j].get("role") == "assistant" and messages[j].get("tool_calls"):
        indexes.add(j)
    return indexes


def parse_messages(messages: list[dict], block_prefix: str = "") -> list[ContextBlock]:
    n = len(messages)
    current_tool_round = _current_tool_round(messages)
    blocks = []
    for i, message in enumerate(messages):
        role = message["role"]
        raw_content = message.get("content")
        content = raw_content
        if content is None:  # e.g. assistant messages that only carry tool_calls
            content = ""
        # Multimodal (list-form) content can never be compacted or replaced
        # with a text marker without destroying its structure, so it is
        # pinned: charged original tokens and forwarded verbatim.
        multimodal = not isinstance(content, str)
        if multimodal:
            content = str(content)

        pinned = (
            role == "system"
            or i == n - 1
            or (i == n - 2 and role in ("user", "assistant"))
            or i in current_tool_round
            or multimodal
            or has_pinned_keyword(content)
        )

        blocks.append(
            ContextBlock(
                block_id=f"{block_prefix}msg_{i}",
                role=role,
                content=content,
                message_index=i,
                type_hint=None,
                pinned=pinned,
                original_message=dict(message),
            )
        )
    return blocks


def summarize_split(blocks: list[ContextBlock]) -> dict:
    pinned_ids = [b.block_id for b in blocks if b.pinned]
    total = len(blocks)
    pinned = len(pinned_ids)
    return {
        "total": total,
        "pinned": pinned,
        "compressible": total - pinned,
        "pinned_ids": pinned_ids,
    }
