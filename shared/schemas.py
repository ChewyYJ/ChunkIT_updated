"""
Shared schema contract — Daytona HackSprint context-compression project.

FREEZE THIS FILE before anyone starts building. After the initial freeze,
only Person 1 (integration lead) should modify it. Everyone else builds
against these types and mocks the others' outputs until real integration.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal, Optional


ContentType = Literal["log", "json", "code", "text"]


# ---------------------------------------------------------------------------
# Input: raw messages coming into the gateway, split into blocks
# ---------------------------------------------------------------------------

@dataclass
class ContextBlock:
    block_id: str              # e.g. "msg_18_tool_2"
    role: str                  # "system" | "user" | "assistant" | "tool"
    content: str
    message_index: int         # position in original message list
    type_hint: Optional[ContentType] = None
    pinned: bool = False       # True = never compress/drop (system, current turn, etc.)
    # Backward-compatible addition (guide §6.4): the verbatim incoming message,
    # so assembly can preserve tool_calls / tool_call_id / name and keep agent
    # tool histories structurally valid while only the content is compressed.
    original_message: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Output of Person 2's Daytona workers: one block -> many normalized units
# ---------------------------------------------------------------------------

@dataclass
class ContextUnit:
    unit_id: str                # e.g. "ctx_a91"
    block_id: str                # which ContextBlock this came from
    content_type: ContentType
    text: str
    source_start: int            # char offset in original block content
    source_end: int
    tokens: int                  # estimated token count
    structural_priority: float   # 0.0-1.0, deterministic (not from Nosana)


# ---------------------------------------------------------------------------
# Output of Person 3's Nosana scoring step
# ---------------------------------------------------------------------------

@dataclass
class ScoredUnit:
    unit_id: str
    relevance: float      # 0.0-1.0, raw Nosana score
    final_priority: float # weighted: 0.65*relevance + 0.15*recency + 0.10*structural + 0.10*role


# ---------------------------------------------------------------------------
# Output of Person 3's budgeter: what to actually send to Kimi
# ---------------------------------------------------------------------------

@dataclass
class SelectionPlan:
    keep: list[str] = field(default_factory=list)     # unit_ids sent in full
    compact: list[str] = field(default_factory=list)  # unit_ids sent summarized
    omit: list[str] = field(default_factory=list)      # unit_ids replaced with retrieval marker
    target_tokens: int = 0
    estimated_tokens: int = 0


# ---------------------------------------------------------------------------
# Metrics for the dashboard / demo (Person 3 renders, Person 1 populates)
# ---------------------------------------------------------------------------

@dataclass
class RunStats:
    run_id: str
    original_tokens: int = 0
    forwarded_tokens: int = 0
    tokens_saved: int = 0
    daytona_ms: float = 0.0
    nosana_ms: float = 0.0
    kimi_ms: float = 0.0
    cached_tokens: int = 0
    retrieval_count: int = 0
    task_result: Optional[str] = None  # e.g. "passed" / "failed" / None


# ---------------------------------------------------------------------------
# Mock helpers — Person 1 uses these to build the full flow without
# waiting on real Daytona/Nosana responses. Person 2 & 3 replace the
# underlying calls, not this contract.
# ---------------------------------------------------------------------------

def mock_units_from_block(block: ContextBlock) -> list[ContextUnit]:
    """Fake 'Daytona worker' response — one unit per block, no real splitting."""
    return [
        ContextUnit(
            unit_id=f"mock_{block.block_id}",
            block_id=block.block_id,
            content_type=block.type_hint or "text",
            text=block.content,
            source_start=0,
            source_end=len(block.content),
            tokens=max(1, len(block.content) // 4),
            structural_priority=0.5,
        )
    ]


def mock_score_units(units: list[ContextUnit]) -> list[ScoredUnit]:
    """Fake 'Nosana score' response — flat 0.5 relevance for everything."""
    return [
        ScoredUnit(unit_id=u.unit_id, relevance=0.5, final_priority=0.5)
        for u in units
    ]
