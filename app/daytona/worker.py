from __future__ import annotations

import time
from dataclasses import dataclass

from shared.schemas import ContextBlock, ContextUnit

from compressors.common import COMPRESSION_CONFIG_VERSION, REDACTION_VERSION, estimate_tokens, redact_secrets, stable_hash
from compressors.detect import detect_processing_route, route_to_schema_content_type
from compressors.diffs import COMPRESSOR_VERSION as DIFF_COMPRESSOR_VERSION
from compressors.diffs import compress_diff_content
from compressors.json_compactor import COMPRESSOR_VERSION as JSON_COMPRESSOR_VERSION
from compressors.json_compactor import compress_json_content
from compressors.logs import COMPRESSOR_VERSION as LOG_COMPRESSOR_VERSION
from compressors.logs import compress_log_content
from compressors.search import COMPRESSOR_VERSION as SEARCH_COMPRESSOR_VERSION
from compressors.search import compress_search_content
from compressors.text_chunker import chunk_text_content

SHORT_CONTENT_TOKENS = 48
TEXT_COMPRESSOR_VERSION = "text_v2"
PASSTHROUGH_VERSION = "passthrough_v1"


@dataclass
class PreprocessedBlock:
    block: ContextBlock
    sanitized_content: str
    detected_route: str
    schema_content_type: str
    redaction_applied: bool
    source_hash: str
    original_tokens: int


@dataclass
class ProcessedBlockResult:
    units: list[ContextUnit]
    metrics: dict[str, object]


def preprocess_block(block: ContextBlock) -> PreprocessedBlock:
    sanitized = redact_secrets(block.content)
    detected_route = detect_processing_route(sanitized, block.type_hint)
    return PreprocessedBlock(
        block=block,
        sanitized_content=sanitized,
        detected_route=detected_route,
        schema_content_type=route_to_schema_content_type(detected_route),
        redaction_applied=sanitized != block.content,
        source_hash=stable_hash(block.content),
        original_tokens=estimate_tokens(block.content),
    )


def prepare_block(block: ContextBlock) -> list[ContextUnit]:
    return prepare_block_result(block).units


def prepare_block_result(
    block: ContextBlock,
    *,
    backend: str = "local",
    preprocessed: PreprocessedBlock | None = None,
    cache_hit: bool = False,
    fallback_used: bool = False,
) -> ProcessedBlockResult:
    started = time.perf_counter()
    prepared = preprocessed or preprocess_block(block)
    units, compressor_name, compressor_version = _route_block(prepared)
    final_units = _apply_safety_guards(prepared, units, compressor_name, compressor_version)
    compressed_tokens = sum(unit.tokens for unit in final_units)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    metrics = {
        "block_id": block.block_id,
        "backend": backend,
        "detected_content_type": prepared.detected_route,
        "schema_content_type": prepared.schema_content_type,
        "compressor": compressor_name,
        "compressor_version": compressor_version,
        "config_version": COMPRESSION_CONFIG_VERSION,
        "redaction_version": REDACTION_VERSION,
        "source_hash": prepared.source_hash,
        "original_tokens": prepared.original_tokens,
        "compressed_tokens": compressed_tokens,
        "tokens_saved": max(0, prepared.original_tokens - compressed_tokens),
        "reduction_percent": round(
            100 * max(0, prepared.original_tokens - compressed_tokens) / max(1, prepared.original_tokens), 2
        ),
        "latency_ms": latency_ms,
        "cache_hit": cache_hit,
        "units_created": len(final_units),
        "redaction_applied": prepared.redaction_applied,
        "fallback_used": fallback_used,
    }
    return ProcessedBlockResult(units=final_units, metrics=metrics)


def build_cache_fingerprint(prepared: PreprocessedBlock, backend: str) -> str:
    compressor_name, compressor_version = _compressor_for_route(prepared.detected_route)
    return stable_hash(
        "|".join(
            [
                prepared.source_hash,
                stable_hash(prepared.sanitized_content),
                prepared.detected_route,
                backend,
                compressor_name,
                compressor_version,
                COMPRESSION_CONFIG_VERSION,
                REDACTION_VERSION,
            ]
        )
    )


def _route_block(prepared: PreprocessedBlock) -> tuple[list[ContextUnit], str, str]:
    if prepared.original_tokens <= SHORT_CONTENT_TOKENS:
        return [_build_unit(prepared, prepared.sanitized_content, 0, len(prepared.block.content), 0.5)], "passthrough", PASSTHROUGH_VERSION

    route = prepared.detected_route
    compressor_name, compressor_version = _compressor_for_route(route)

    if route == "json":
        units = _build_units_from_segments(prepared, compress_json_content(prepared.sanitized_content), 0.8)
        return units, compressor_name, compressor_version
    if route == "log":
        units = _build_units_from_segments(prepared, compress_log_content(prepared.sanitized_content), 0.95)
        return units, compressor_name, compressor_version
    if route == "search":
        units = _build_units_from_segments(prepared, compress_search_content(prepared.sanitized_content), 0.75)
        return units, compressor_name, compressor_version
    if route == "diff":
        units = _build_units_from_segments(prepared, compress_diff_content(prepared.sanitized_content), 0.85)
        return units, compressor_name, compressor_version

    chunks = chunk_text_content(prepared.sanitized_content)
    priority = 0.75 if route == "code" else 0.6
    units = [
        _build_unit(prepared, text, start, end, priority)
        for start, end, text in chunks
    ]
    return units, compressor_name, compressor_version


def _apply_safety_guards(
    prepared: PreprocessedBlock,
    units: list[ContextUnit],
    compressor_name: str,
    compressor_version: str,
) -> list[ContextUnit]:
    if not units:
        return [_build_unit(prepared, prepared.sanitized_content, 0, len(prepared.block.content), 0.5)]

    compressed_tokens = sum(unit.tokens for unit in units)
    if compressed_tokens >= prepared.original_tokens:
        return [_build_unit(prepared, prepared.sanitized_content, 0, len(prepared.block.content), 0.5)]

    return units


def _build_units_from_segments(
    prepared: PreprocessedBlock,
    segments: list[dict],
    structural_priority: float,
) -> list[ContextUnit]:
    return [
        _build_unit(
            prepared,
            segment["text"],
            int(segment.get("source_start", 0)),
            int(segment.get("source_end", len(prepared.block.content))),
            structural_priority,
        )
        for segment in segments
        if segment.get("text")
    ]


def _build_unit(
    prepared: PreprocessedBlock,
    text: str,
    source_start: int,
    source_end: int,
    structural_priority: float,
) -> ContextUnit:
    unit_key = stable_hash(f"{prepared.block.block_id}:{source_start}:{source_end}:{text}")[:10]
    return ContextUnit(
        unit_id=f"ctx_{unit_key}",
        block_id=prepared.block.block_id,
        content_type=prepared.schema_content_type,
        text=text,
        source_start=source_start,
        source_end=source_end,
        tokens=estimate_tokens(text),
        structural_priority=structural_priority,
    )


def _compressor_for_route(route: str) -> tuple[str, str]:
    if route == "json":
        return "json", JSON_COMPRESSOR_VERSION
    if route == "log":
        return "log", LOG_COMPRESSOR_VERSION
    if route == "search":
        return "search", SEARCH_COMPRESSOR_VERSION
    if route == "diff":
        return "diff", DIFF_COMPRESSOR_VERSION
    if route == "code":
        return "code", TEXT_COMPRESSOR_VERSION
    return "text", TEXT_COMPRESSOR_VERSION
