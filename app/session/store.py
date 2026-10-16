from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from shared.schemas import ContextBlock, ContextUnit

DB_DIR = os.getenv("CHUNKIT_DB_DIR", "/tmp/chunkit" if os.getenv("VERCEL") else "data")
DB_PATH = os.getenv("CHUNKIT_DB_PATH", os.path.join(DB_DIR, "sessions.db"))


def storage_status() -> dict:
    ephemeral = bool(os.getenv("VERCEL")) or os.path.abspath(DB_PATH).startswith("/tmp/")
    return {"mode": "ephemeral" if ephemeral else "local", "persistent": not ephemeral,
            "notice": ("Context is temporary on this server. It may reset between requests, restarts or deployments. "
                       "Session totals belong to the current server instance." if ephemeral else
                       "Context is stored in a local SQLite file. Session totals reset when the server restarts.")}


def _connect() -> sqlite3.Connection:
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return sqlite3.connect(DB_PATH, timeout=15)


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS context_blocks_v2 (
                storage_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                block_id TEXT NOT NULL,
                detected_content_type TEXT,
                schema_content_type TEXT,
                compressor TEXT,
                compressor_version TEXT,
                source_hash TEXT,
                original_content TEXT NOT NULL,
                original_tokens INTEGER,
                compressed_tokens INTEGER,
                metrics_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(session_id, block_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS context_units_v2 (
                unit_storage_id TEXT PRIMARY KEY,
                storage_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                block_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                detected_content_type TEXT,
                schema_content_type TEXT,
                compressor TEXT,
                compressor_version TEXT,
                source_hash TEXT,
                original_tokens INTEGER,
                compressed_tokens INTEGER,
                source_start INTEGER,
                source_end INTEGER,
                text TEXT NOT NULL,
                structural_priority REAL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def make_storage_id(session_id: str, block_id: str) -> str:
    return f"{session_id}:{block_id}"


def store_block(
    session_id: str,
    block: ContextBlock,
    tokens: int,
    *,
    metrics: dict[str, object] | None = None,
    units: list[ContextUnit] | None = None,
) -> str:
    created_at = datetime.now(timezone.utc).isoformat()
    storage_id = make_storage_id(session_id, block.block_id)
    metrics = metrics or {}
    units = units or []

    detected_content_type = str(metrics.get("detected_content_type", block.type_hint or "text"))
    schema_content_type = str(metrics.get("schema_content_type", block.type_hint or "text"))
    compressor = str(metrics.get("compressor", "unknown"))
    compressor_version = str(metrics.get("compressor_version", "unknown"))
    source_hash = str(metrics.get("source_hash", ""))
    compressed_tokens = int(metrics.get("compressed_tokens", sum(unit.tokens for unit in units) or tokens))

    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO context_blocks_v2
                (storage_id, session_id, block_id, detected_content_type, schema_content_type, compressor,
                 compressor_version, source_hash, original_content, original_tokens, compressed_tokens,
                 metrics_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                storage_id,
                session_id,
                block.block_id,
                detected_content_type,
                schema_content_type,
                compressor,
                compressor_version,
                source_hash,
                block.content,
                tokens,
                compressed_tokens,
                json.dumps(metrics, ensure_ascii=True, sort_keys=True),
                created_at,
            ),
        )
        conn.execute("DELETE FROM context_units_v2 WHERE storage_id = ?", (storage_id,))
        for unit in units:
            conn.execute(
                """
                INSERT INTO context_units_v2
                    (unit_storage_id, storage_id, session_id, block_id, unit_id, detected_content_type,
                     schema_content_type, compressor, compressor_version, source_hash, original_tokens,
                     compressed_tokens, source_start, source_end, text, structural_priority, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{storage_id}:{unit.unit_id}",
                    storage_id,
                    session_id,
                    block.block_id,
                    unit.unit_id,
                    detected_content_type,
                    unit.content_type,
                    compressor,
                    compressor_version,
                    source_hash,
                    tokens,
                    unit.tokens,
                    unit.source_start,
                    unit.source_end,
                    unit.text,
                    unit.structural_priority,
                    created_at,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return storage_id


def get_original(session_id: str, context_id: str) -> dict | None:
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT storage_id, session_id, block_id, detected_content_type, schema_content_type,
                   compressor, compressor_version, source_hash, original_content, original_tokens,
                   compressed_tokens, metrics_json
            FROM context_blocks_v2
            WHERE session_id = ? AND block_id = ?
            """,
            (session_id, context_id),
        )
        row = cur.fetchone()
        if row is None and ":" in context_id:
            cur = conn.execute(
                """
                SELECT storage_id, session_id, block_id, detected_content_type, schema_content_type,
                       compressor, compressor_version, source_hash, original_content, original_tokens,
                       compressed_tokens, metrics_json
                FROM context_blocks_v2
                WHERE storage_id = ? AND session_id = ?
                """,
                (context_id, session_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
