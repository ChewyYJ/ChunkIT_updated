from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from compressors.common import stable_hash

from app.daytona.worker import PreprocessedBlock, ProcessedBlockResult
from shared.schemas import ContextBlock, ContextUnit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT_PATH = REPO_ROOT / "workers" / "sandbox_prepare_server.py"
PROJECT_LABEL = "chunkit"
PARENT_ROLE = "context-parent"
WORKER_ROLE = "context-worker"
REMOTE_RUNTIME_ROOT = "workspace/chunkit_runtime"
REMOTE_WORKER_PATH = f"{REMOTE_RUNTIME_ROOT}/workers/sandbox_prepare_server.py"
RUNTIME_FILES = (
    "workers/sandbox_prepare_server.py",
    "app/daytona/worker.py",
    "shared/schemas.py",
    "compressors/__init__.py",
    "compressors/common.py",
    "compressors/detect.py",
    "compressors/diffs.py",
    "compressors/json_compactor.py",
    "compressors/logs.py",
    "compressors/search.py",
    "compressors/text_chunker.py",
)


def _load_daytona_sdk():
    from daytona_sdk import AsyncDaytona, CreateSandboxFromSnapshotParams, DaytonaConfig, ListSandboxesQuery, SessionExecuteRequest

    return AsyncDaytona, CreateSandboxFromSnapshotParams, DaytonaConfig, ListSandboxesQuery, SessionExecuteRequest


@dataclass
class _WorkerHandle:
    sandbox: Any
    url: str
    slot: int


class DaytonaWorkerPool:
    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._loop_id: int | None = None
        self._ready = False
        self._client: Any = None
        self._parent: Any = None
        self._workers: list[_WorkerHandle] = []
        self._cursor = 0
        self._owner = os.getenv("DAYTONA_OWNER_ID", f"{PROJECT_LABEL}-{socket.gethostname()}")
        self._pool_name = os.getenv("DAYTONA_SANDBOX_PREFIX", "chunkit")
        self._owner_slug = stable_hash(self._owner)[:8]

    async def prepare_block(self, block: ContextBlock, preprocessed: PreprocessedBlock) -> ProcessedBlockResult:
        await self.ensure_ready()
        worker = self._next_worker()
        try:
            return await self._dispatch(worker, block, preprocessed)
        except Exception:
            worker = await self._respawn_worker(worker.slot)
            return await self._dispatch(worker, block, preprocessed)

    async def ensure_ready(self) -> None:
        if not daytona_remote_enabled():
            raise RuntimeError("Daytona remote backend is not enabled")

        loop_id = id(asyncio.get_running_loop())
        if self._ready and self._loop_id == loop_id:
            return

        if self._lock is None or self._loop_id != loop_id:
            self._lock = asyncio.Lock()
            self._loop_id = loop_id

        async with self._lock:
            if self._ready and self._loop_id == loop_id:
                return

            AsyncDaytona, CreateSandboxFromSnapshotParams, DaytonaConfig, ListSandboxesQuery, SessionExecuteRequest = _load_daytona_sdk()
            config = DaytonaConfig(
                api_key=os.getenv("DAYTONA_API_KEY"),
                api_url=os.getenv("DAYTONA_API_URL", "https://app.daytona.io/api"),
                target=os.getenv("DAYTONA_TARGET") or None,
            )
            worker_count = int(os.getenv("DAYTONA_WORKER_COUNT", "2"))
            port = int(os.getenv("DAYTONA_WORKER_PORT", "8000"))

            self._client = AsyncDaytona(config)
            self._workers = []
            self._cursor = 0

            try:
                self._parent = await self._ensure_parent(CreateSandboxFromSnapshotParams, ListSandboxesQuery)
                for slot in range(worker_count):
                    worker = await self._ensure_worker(
                        slot, port, CreateSandboxFromSnapshotParams, ListSandboxesQuery, SessionExecuteRequest
                    )
                    self._workers.append(worker)
                self._ready = True
            except Exception:
                self._ready = False
                self._workers = []
                await self.close()
                raise

    async def _ensure_parent(self, params_cls: Any, query_cls: Any):
        candidates = await self._list_owned(query_cls, role=PARENT_ROLE)
        for candidate in candidates:
            healthy = await self._ensure_running(candidate)
            if healthy:
                return candidate

        existing = await self._get_named_owned_sandbox(self._parent_name())
        if existing is not None:
            healthy = await self._ensure_running(existing)
            if healthy:
                return healthy

        return await self._client.create(
            params_cls(
                name=self._parent_name(),
                language="python",
                auto_stop_interval=0,
                auto_archive_interval=60,
                labels=self._base_labels(PARENT_ROLE),
            )
        )

    async def _ensure_worker(self, slot: int, port: int, params_cls: Any, query_cls: Any, session_request_cls: Any):
        candidates = await self._list_owned(query_cls, role=WORKER_ROLE, worker_slot=str(slot))
        for candidate in candidates:
            candidate = await self._ensure_running(candidate)
            if candidate and await self._worker_healthy(candidate, port):
                signed = await candidate.create_signed_preview_url(port, expires_in_seconds=3600)
                return _WorkerHandle(sandbox=candidate, url=signed.url.rstrip("/"), slot=slot)
            await self._safe_delete(candidate)

        existing = await self._get_named_owned_sandbox(self._worker_name(slot))
        if existing is not None:
            existing = await self._ensure_running(existing)
            if existing and await self._worker_healthy(existing, port):
                signed = await existing.create_signed_preview_url(port, expires_in_seconds=3600)
                return _WorkerHandle(sandbox=existing, url=signed.url.rstrip("/"), slot=slot)
            await self._safe_delete(existing)

        sandbox = await self._client.create(
            params_cls(
                name=self._worker_name(slot),
                language="python",
                linked_sandbox=self._parent.id,
                ephemeral=True,
                auto_stop_interval=int(os.getenv("DAYTONA_CHILD_AUTO_STOP_MINUTES", "15")),
                labels=self._base_labels(WORKER_ROLE, worker_slot=str(slot), parent_id=self._parent.id),
            )
        )
        await self._install_and_start_worker(sandbox, port, session_request_cls)
        signed = await sandbox.create_signed_preview_url(port, expires_in_seconds=3600)
        return _WorkerHandle(sandbox=sandbox, url=signed.url.rstrip("/"), slot=slot)

    async def _dispatch(self, worker: _WorkerHandle, block: ContextBlock, preprocessed: PreprocessedBlock) -> ProcessedBlockResult:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{worker.url}/prepare",
                json={
                    "block_id": block.block_id,
                    "role": block.role,
                    "content": preprocessed.sanitized_content,
                    "message_index": block.message_index,
                    "detected_route": preprocessed.detected_route,
                    "schema_content_type": preprocessed.schema_content_type,
                    "source_hash": preprocessed.source_hash,
                },
            )
            response.raise_for_status()
            payload = response.json()
        units = [ContextUnit(**unit) for unit in payload["units"]]
        metrics = dict(payload["metrics"])
        metrics.update(
            {
                "block_id": block.block_id,
                "backend": "daytona",
                "detected_content_type": preprocessed.detected_route,
                "schema_content_type": preprocessed.schema_content_type,
                "source_hash": preprocessed.source_hash,
                "original_tokens": preprocessed.original_tokens,
                "redaction_applied": preprocessed.redaction_applied,
                "compressed_tokens": sum(unit.tokens for unit in units),
                "tokens_saved": max(0, preprocessed.original_tokens - sum(unit.tokens for unit in units)),
            }
        )
        metrics["reduction_percent"] = round(
            100 * metrics["tokens_saved"] / max(1, preprocessed.original_tokens),
            2,
        )
        return ProcessedBlockResult(units=units, metrics=metrics)

    async def _install_and_start_worker(self, sandbox: Any, port: int, session_request_cls: Any) -> None:
        await self._upload_worker_runtime(sandbox)

        session_id = f"chunkit-worker-{sandbox.id[:8]}"
        try:
            await sandbox.process.delete_session(session_id)
        except Exception:
            pass

        await sandbox.process.create_session(session_id)
        command = (
            f"export CHUNKIT_WORKER_PORT={port}; "
            f"export PYTHONPATH={REMOTE_RUNTIME_ROOT}; "
            f"nohup python3 {REMOTE_WORKER_PATH} >/tmp/chunkit-worker.log 2>&1 &"
        )
        await sandbox.process.execute_session_command(
            session_id,
            session_request_cls(command=command, run_async=True),
        )
        await self._wait_for_health(sandbox, port)

    async def _upload_worker_runtime(self, sandbox: Any) -> None:
        directories = (
            "workspace",
            REMOTE_RUNTIME_ROOT,
            f"{REMOTE_RUNTIME_ROOT}/workers",
            f"{REMOTE_RUNTIME_ROOT}/app",
            f"{REMOTE_RUNTIME_ROOT}/app/daytona",
            f"{REMOTE_RUNTIME_ROOT}/shared",
            f"{REMOTE_RUNTIME_ROOT}/compressors",
        )
        for directory in directories:
            await sandbox.fs.create_folder(directory, "755")

        for relative_path in RUNTIME_FILES:
            local_path = REPO_ROOT / relative_path
            remote_path = f"{REMOTE_RUNTIME_ROOT}/{relative_path}"
            await sandbox.fs.upload_file(str(local_path), remote_path)

    async def _respawn_worker(self, slot: int) -> _WorkerHandle:
        async with self._lock:
            port = int(os.getenv("DAYTONA_WORKER_PORT", "8000"))
            _, params_cls, _, query_cls, session_request_cls = _load_daytona_sdk()
            current = [worker for worker in self._workers if worker.slot == slot]
            for worker in current:
                await self._safe_delete(worker.sandbox)
            replacement = await self._ensure_worker(slot, port, params_cls, query_cls, session_request_cls)
            self._workers = [worker for worker in self._workers if worker.slot != slot] + [replacement]
            self._workers.sort(key=lambda worker: worker.slot)
            return replacement

    async def _wait_for_health(self, sandbox: Any, port: int) -> None:
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            if await self._worker_healthy(sandbox, port):
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"Timed out waiting for Daytona worker in sandbox {sandbox.id}")
            await asyncio.sleep(1)

    async def _worker_healthy(self, sandbox: Any, port: int) -> bool:
        try:
            signed = await sandbox.create_signed_preview_url(port, expires_in_seconds=60)
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{signed.url.rstrip('/')}/health")
                return response.status_code == 200
        except Exception:
            return False

    async def _ensure_running(self, sandbox: Any):
        try:
            state = str(getattr(sandbox, "state", "") or "").lower()
            if state and state != "started":
                await sandbox.start()
            return sandbox
        except Exception:
            return None

    async def _list_owned(self, query_cls: Any, **extra_labels: str) -> list[Any]:
        labels = self._base_labels(**extra_labels)
        results: list[Any] = []
        async for sandbox in self._client.list(query_cls(labels=labels)):
            results.append(sandbox)
        return results

    async def _get_named_owned_sandbox(self, name: str):
        try:
            sandbox = await self._client.get(name)
        except Exception:
            return None
        sandbox_labels = dict(getattr(sandbox, "labels", {}) or {})
        if sandbox_labels.get("project") != PROJECT_LABEL:
            return None
        if sandbox_labels.get("owner") != self._owner:
            return None
        return sandbox

    def _base_labels(self, role: str | None = None, **extra: str) -> dict[str, str]:
        labels = {"project": PROJECT_LABEL, "owner": self._owner}
        if role:
            labels["role"] = role
        labels.update(extra)
        return labels

    def _parent_name(self) -> str:
        return f"{self._pool_name}-{self._owner_slug}-parent"

    def _worker_name(self, slot: int) -> str:
        return f"{self._pool_name}-{self._owner_slug}-child-{slot}"

    def _next_worker(self) -> _WorkerHandle:
        if not self._workers:
            raise RuntimeError("No Daytona workers are available")
        worker = self._workers[self._cursor % len(self._workers)]
        self._cursor += 1
        return worker

    async def close(self) -> None:
        if self._client is not None:
            for worker in list(self._workers):
                await self._safe_delete(worker.sandbox)
            if self._parent is not None:
                await self._safe_delete(self._parent)
            try:
                await self._client.close()
            finally:
                self._client = None
        self._ready = False
        self._parent = None
        self._workers = []

    async def _safe_delete(self, sandbox: Any) -> None:
        try:
            await self._client.delete(sandbox, wait=True)
        except Exception:
            pass

    def status(self) -> dict[str, object]:
        return {
            "ready": self._ready,
            "worker_count": len(self._workers),
            "parent_id": getattr(self._parent, "id", None),
            "owner": self._owner,
        }


_POOL = DaytonaWorkerPool()


def daytona_remote_enabled() -> bool:
    backend = os.getenv("DAYTONA_BACKEND", "local").lower()
    return backend in {"remote", "daytona"} and bool(os.getenv("DAYTONA_API_KEY"))


async def prepare_block_with_daytona(block: ContextBlock, preprocessed: PreprocessedBlock):
    if not daytona_remote_enabled():
        raise RuntimeError("DAYTONA remote backend is not configured")
    return await _POOL.prepare_block(block, preprocessed)


async def initialize_daytona_pool() -> None:
    if not daytona_remote_enabled():
        return
    await _POOL.ensure_ready()


async def shutdown_daytona_pool() -> None:
    await _POOL.close()


def daytona_pool_status() -> dict[str, object]:
    return _POOL.status()
