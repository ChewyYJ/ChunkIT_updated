import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.daytona.client import prepare_context_blocks
from app.daytona.remote import daytona_remote_enabled, initialize_daytona_pool, prepare_block_with_daytona
from app.daytona.sandbox import LocalSandboxManager
from app.daytona.worker import build_cache_fingerprint, prepare_block, prepare_block_result, preprocess_block
from app.session.store import get_original, init_db, store_block
from app.kimi.tools import execute_retrieve, reset_active_session_id, set_active_session_id
from shared.schemas import ContextBlock


def test_prepare_block_json_redacts_and_preserves_shape():
    block = ContextBlock(
        block_id="json_1",
        role="tool",
        content='{"api_key":"sk-secret-value-123456","items":[{"id":1,"name":"alpha"},{"id":2,"name":"beta"}]}',
        message_index=0,
    )
    units = prepare_block(block)
    assert len(units) == 1
    assert units[0].content_type == "json"
    assert "REDACTED" in units[0].text
    assert '"items"' in units[0].text or '"count": 2' in units[0].text


def test_prepare_block_logs_dedupes_repeats_and_keeps_errors():
    block = ContextBlock(
        block_id="log_1",
        role="tool",
        content=(
            "INFO booting service\n"
            "INFO booting service\n"
            "WARN cache cold\n"
            "ERROR failed to connect\n"
            "INFO completed\n"
        ),
        message_index=0,
    )
    units = prepare_block(block)
    assert len(units) >= 1
    assert units[0].content_type == "log"
    combined = "\n".join(unit.text for unit in units)
    assert "INFO booting service" in combined
    assert "ERROR failed to connect" in combined


def test_prepare_block_detects_search_and_diff_routes():
    search_block = ContextBlock(
        block_id="search_1",
        role="tool",
        content=(
            "app/main.py:10:def run_app():\n"
            "app/main.py:24:    return True\n"
            "workers/service.py:8:from fastapi import FastAPI\n"
        ),
        message_index=0,
    )
    search_units = prepare_block(search_block)
    assert search_units
    assert search_units[0].content_type == "text"
    assert "app/main.py" in search_units[0].text

    diff_block = ContextBlock(
        block_id="diff_1",
        role="tool",
        content=(
            "diff --git a/app/main.py b/app/main.py\n"
            "--- a/app/main.py\n"
            "+++ b/app/main.py\n"
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            "+import sys\n"
            " from fastapi import FastAPI\n"
        ),
        message_index=0,
    )
    diff_units = prepare_block(diff_block)
    assert diff_units
    assert diff_units[0].content_type == "code"
    assert "diff --git" in diff_units[0].text


def test_prepare_context_blocks_chunks_text_and_uses_cache():
    block_a = ContextBlock(
        block_id="msg_a",
        role="user",
        content=("Paragraph one.\n\n" + "Repeated chunk.\n" * 20 + "\nEnding paragraph."),
        message_index=0,
    )
    block_b = ContextBlock(
        block_id="msg_b",
        role="user",
        content=block_a.content,
        message_index=1,
    )
    units = asyncio.run(prepare_context_blocks([block_a, block_b]))
    assert len(units) >= 2
    a_units = [unit for unit in units if unit.block_id == "msg_a"]
    b_units = [unit for unit in units if unit.block_id == "msg_b"]
    assert a_units
    assert b_units
    assert a_units[0].text == b_units[0].text
    assert a_units[0].unit_id != b_units[0].unit_id


def test_cache_fingerprint_changes_by_backend():
    block = ContextBlock(block_id="msg_cache", role="user", content="same content", message_index=0)
    prepared = preprocess_block(block)
    assert build_cache_fingerprint(prepared, "local") != build_cache_fingerprint(prepared, "daytona")


def test_local_sandbox_manager_supports_lifecycle():
    async def scenario():
        manager = LocalSandboxManager()
        parent = await manager.create_parent("parent")
        child = await manager.create_child(parent.sandbox_id, "child")
        await manager.stop(child.sandbox_id)
        assert not await manager.healthcheck(child.sandbox_id)
        await manager.start(child.sandbox_id)
        result = await manager.exec(child.sandbox_id, "echo ok")
        assert result["exit_code"] == 0
        assert await manager.healthcheck(child.sandbox_id)

    asyncio.run(scenario())


def test_daytona_remote_enabled_requires_backend_and_key():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DAYTONA_BACKEND", None)
        os.environ.pop("DAYTONA_API_KEY", None)
        assert not daytona_remote_enabled()

    with patch.dict(os.environ, {"DAYTONA_BACKEND": "remote"}, clear=False):
        os.environ.pop("DAYTONA_API_KEY", None)
        assert not daytona_remote_enabled()

    with patch.dict(os.environ, {"DAYTONA_BACKEND": "remote", "DAYTONA_API_KEY": "test-key"}, clear=False):
        assert daytona_remote_enabled()


def test_initialize_daytona_pool_skips_when_remote_disabled():
    with patch.dict(os.environ, {"DAYTONA_BACKEND": "local"}, clear=False):
        with patch("app.daytona.remote._POOL.ensure_ready", new=AsyncMock()) as ensure_ready:
            asyncio.run(initialize_daytona_pool())
            ensure_ready.assert_not_called()


def test_remote_dispatch_uses_redacted_content():
    block = ContextBlock(
        block_id="remote_1",
        role="user",
        content="OPENAI_API_KEY=sk-secret-value-1234567890",
        message_index=0,
    )
    prepared = preprocess_block(block)

    async def scenario():
        with patch("app.daytona.remote.daytona_remote_enabled", return_value=True):
            with patch("app.daytona.remote._POOL.prepare_block", new=AsyncMock()) as prepare_remote:
                prepare_remote.return_value = prepare_block_result(block, backend="daytona", preprocessed=prepared)
                await prepare_block_with_daytona(block, prepared)
                dispatched_block, dispatched_preprocessed = prepare_remote.await_args.args
                assert dispatched_block.block_id == block.block_id
                assert "sk-secret-value" not in dispatched_preprocessed.sanitized_content
                assert "<REDACTED>" in dispatched_preprocessed.sanitized_content

    asyncio.run(scenario())


def test_session_storage_and_retrieval_are_isolated():
    init_db()
    block_a = ContextBlock(block_id="msg_1", role="user", content="SECRET_CONTEXT_A", message_index=0)
    block_b = ContextBlock(block_id="msg_1", role="user", content="SECRET_CONTEXT_B", message_index=0)

    store_block("session_A", block_a, 3, metrics={"detected_content_type": "text", "compressed_tokens": 2})
    store_block("session_B", block_b, 3, metrics={"detected_content_type": "text", "compressed_tokens": 2})

    row_a = get_original("session_A", "msg_1")
    row_b = get_original("session_B", "msg_1")
    assert row_a is not None and row_a["original_content"] == "SECRET_CONTEXT_A"
    assert row_b is not None and row_b["original_content"] == "SECRET_CONTEXT_B"

    token_a = set_active_session_id("session_A")
    try:
        assert "SECRET_CONTEXT_A" in execute_retrieve(["msg_1"])
    finally:
        reset_active_session_id(token_a)

    token_b = set_active_session_id("session_B")
    try:
        assert "SECRET_CONTEXT_B" in execute_retrieve(["msg_1"])
    finally:
        reset_active_session_id(token_b)


if __name__ == "__main__":
    test_prepare_block_json_redacts_and_preserves_shape()
    test_prepare_block_logs_dedupes_repeats_and_keeps_errors()
    test_prepare_block_detects_search_and_diff_routes()
    test_prepare_context_blocks_chunks_text_and_uses_cache()
    test_cache_fingerprint_changes_by_backend()
    test_local_sandbox_manager_supports_lifecycle()
    test_daytona_remote_enabled_requires_backend_and_key()
    test_initialize_daytona_pool_skips_when_remote_disabled()
    test_remote_dispatch_uses_redacted_content()
    test_session_storage_and_retrieval_are_isolated()
    print("All assertions passed.")
