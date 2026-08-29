from __future__ import annotations

from contextvars import ContextVar

from app.session.store import get_original

_ACTIVE_SESSION_ID: ContextVar[str | None] = ContextVar("active_session_id", default=None)

RETRIEVE_CONTEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "retrieve_context",
        "description": (
            "Retrieve the original, full content of one or more context blocks that "
            "were omitted from this conversation to save tokens. Use this when you "
            "need details that were replaced with a '[CONTEXT OMITTED ...]' marker."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "context_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The context_id values from the omission markers you want to retrieve.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional: what you're trying to find, for context.",
                },
            },
            "required": ["context_ids"],
        },
    },
}


def execute_retrieve(context_ids: list[str], session_id: str | None = None) -> str:
    """Fetch original blocks for the given ids, scoped to a session.

    An explicit session_id wins (Person 3 call sites pass it directly);
    otherwise the ambient session set via set_active_session_id() is used
    (Person 2 call sites). Without either, nothing can be retrieved.
    """
    active_session = session_id or _ACTIVE_SESSION_ID.get()
    parts = []
    for context_id in context_ids:
        row = get_original(active_session, context_id) if active_session else None
        if row is None:
            parts.append(f"[{context_id}]: not found.")
        else:
            parts.append(
                f"[{context_id}] (block {row['block_id']}, {row['detected_content_type']}, "
                f"compressor={row['compressor']} {row['compressor_version']}, "
                f"~{row['original_tokens']} original tokens, ~{row['compressed_tokens']} compressed tokens):\n"
                f"{row['original_content']}"
            )
    return "\n\n".join(parts)


def set_active_session_id(session_id: str | None):
    return _ACTIVE_SESSION_ID.set(session_id)


def reset_active_session_id(token) -> None:
    _ACTIVE_SESSION_ID.reset(token)
