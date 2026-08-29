from __future__ import annotations

import asyncio
from contextvars import ContextVar

from app.daytona.cache import ContextCache
from app.daytona.remote import daytona_remote_enabled, prepare_block_with_daytona
from app.daytona.worker import ProcessedBlockResult, build_cache_fingerprint, prepare_block_result, preprocess_block
from shared.schemas import ContextBlock, ContextUnit

_CACHE = ContextCache()
_LAST_PREPARE_REPORT: ContextVar[list[dict[str, object]]] = ContextVar("last_prepare_report", default=[])


async def prepare_context_blocks(blocks: list[ContextBlock]) -> list[ContextUnit]:
    _LAST_PREPARE_REPORT.set([])
    processed = await asyncio.gather(*[_prepare_single_block(block) for block in blocks])
    _LAST_PREPARE_REPORT.set([result.metrics for result in processed])
    return [unit for result in processed for unit in result.units]


async def _prepare_single_block(block: ContextBlock) -> ProcessedBlockResult:
    backend = "daytona" if daytona_remote_enabled() else "local"
    prepared = preprocess_block(block)
    try:
        cache_key = build_cache_fingerprint(prepared, backend)
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return _with_block_override(block.block_id, cached, cache_hit=True)

        result = await _prepare_with_selected_backend(block, prepared)
        if result.metrics["backend"] == backend:
            _CACHE.set(cache_key, result)
        return result
    except Exception as exc:
        print(f"[daytona] block preparation failed for {block.block_id}: {exc}. Using safe passthrough.")
        return await asyncio.to_thread(prepare_block_result, block, backend="local", preprocessed=prepared, fallback_used=True)


async def _prepare_with_selected_backend(block: ContextBlock, prepared) -> ProcessedBlockResult:
    if daytona_remote_enabled():
        try:
            return await prepare_block_with_daytona(block, prepared)
        except Exception as exc:
            print(f"[daytona] remote backend failed for {block.block_id}: {exc}. Falling back to local worker.")
            local_result = await asyncio.to_thread(
                prepare_block_result, block, backend="local", preprocessed=prepared, fallback_used=True
            )
            return local_result
    return await asyncio.to_thread(prepare_block_result, block, backend="local", preprocessed=prepared)


def _with_block_override(block_id: str, result: ProcessedBlockResult, *, cache_hit: bool) -> ProcessedBlockResult:
    copied_units: list[ContextUnit] = []
    for unit in result.units:
        copied_units.append(
            ContextUnit(
                unit_id=f"{unit.unit_id}_{block_id}",
                block_id=block_id,
                content_type=unit.content_type,
                text=unit.text,
                source_start=unit.source_start,
                source_end=unit.source_end,
                tokens=unit.tokens,
                structural_priority=unit.structural_priority,
            )
        )
    metrics = dict(result.metrics)
    metrics["block_id"] = block_id
    metrics["cache_hit"] = cache_hit
    metrics["units_created"] = len(copied_units)
    return ProcessedBlockResult(units=copied_units, metrics=metrics)


def get_last_prepare_report() -> list[dict[str, object]]:
    return list(_LAST_PREPARE_REPORT.get())
