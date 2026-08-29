import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.nosana.config import NosanaSettings
from shared.schemas import ContextBlock, ContextUnit

# Legacy standalone smoke scripts (run with `python tests/<file>.py`); they
# execute at import time, so keep them out of pytest collection.
collect_ignore = ["test_parser.py", "test_pipeline.py", "test_retrieval.py"]


def make_settings(**overrides) -> NosanaSettings:
    defaults = dict(
        enabled=True,
        scorer_url="http://scorer.test",
        scorer_token="test-token",
        timeout_seconds=1.0,
        max_retries=1,
        request_batch_size=128,
        fallback_enabled=True,
    )
    defaults.update(overrides)
    return NosanaSettings(**defaults)


def make_block(
    i: int,
    role: str = "user",
    content: str = "hello world",
    pinned: bool = False,
) -> ContextBlock:
    return ContextBlock(
        block_id=f"msg_{i}",
        role=role,
        content=content,
        message_index=i,
        type_hint=None,
        pinned=pinned,
    )


def make_unit(
    block: ContextBlock,
    unit_id: str | None = None,
    tokens: int | None = None,
    text: str | None = None,
    structural_priority: float = 0.5,
) -> ContextUnit:
    text = text if text is not None else block.content
    return ContextUnit(
        unit_id=unit_id or f"u_{block.block_id}",
        block_id=block.block_id,
        content_type="text",
        text=text,
        source_start=0,
        source_end=len(text),
        tokens=tokens if tokens is not None else max(1, len(text) // 4),
        structural_priority=structural_priority,
    )


@pytest.fixture
def settings_factory():
    return make_settings
