from __future__ import annotations

import asyncio
import os

from app.daytona.remote import DaytonaWorkerPool
from app.daytona.sandbox import LocalSandboxManager


async def bootstrap_daytona(worker_count: int = 2) -> dict:
    if os.getenv("DAYTONA_BACKEND", "local").lower() in {"remote", "daytona"} and os.getenv("DAYTONA_API_KEY"):
        pool = DaytonaWorkerPool()
        try:
            await pool.ensure_ready()
            return {
                "backend": "daytona",
                "workers": len(pool._workers),
                "parent_id": pool._parent.id,
                "worker_ids": [worker.sandbox.id for worker in pool._workers],
            }
        finally:
            await pool.close()

    manager = LocalSandboxManager()
    parent = await manager.create_parent()
    await manager.exec(parent.sandbox_id, "python -m workers.service")

    children = []
    for index in range(worker_count):
        child = await manager.create_child(parent.sandbox_id, f"daytona-child-{index}")
        await manager.exec(child.sandbox_id, "python -m workers.service")
        children.append(
            {
                "sandbox_id": child.sandbox_id,
                "running": child.running,
                "ephemeral": child.ephemeral,
                "healthy": await manager.healthcheck(child.sandbox_id),
            }
        )

    return {
        "backend": "local",
        "parent": {"sandbox_id": parent.sandbox_id, "running": parent.running},
        "children": children,
    }


def main() -> None:
    result = asyncio.run(bootstrap_daytona())
    print(result)


if __name__ == "__main__":
    main()
