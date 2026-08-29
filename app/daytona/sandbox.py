from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class Sandbox:
    sandbox_id: str
    kind: str
    linked_to: str | None = None
    running: bool = False
    ephemeral: bool = False
    command_log: list[str] = field(default_factory=list)


class LocalSandboxManager:
    def __init__(self) -> None:
        self._sandboxes: dict[str, Sandbox] = {}

    async def create_parent(self, sandbox_id: str = "daytona-parent") -> Sandbox:
        sandbox = Sandbox(sandbox_id=sandbox_id, kind="parent", running=True)
        self._sandboxes[sandbox_id] = sandbox
        return sandbox

    async def create_child(self, parent_id: str, sandbox_id: str) -> Sandbox:
        sandbox = Sandbox(
            sandbox_id=sandbox_id,
            kind="child",
            linked_to=parent_id,
            running=True,
            ephemeral=True,
        )
        self._sandboxes[sandbox_id] = sandbox
        return sandbox

    async def start(self, sandbox_id: str) -> Sandbox:
        sandbox = self._sandboxes[sandbox_id]
        sandbox.running = True
        return sandbox

    async def stop(self, sandbox_id: str) -> Sandbox:
        sandbox = self._sandboxes[sandbox_id]
        sandbox.running = False
        return sandbox

    async def exec(self, sandbox_id: str, command: str) -> dict:
        sandbox = self._sandboxes[sandbox_id]
        sandbox.command_log.append(command)
        await asyncio.sleep(0)
        return {"sandbox_id": sandbox_id, "command": command, "exit_code": 0}

    async def healthcheck(self, sandbox_id: str) -> bool:
        sandbox = self._sandboxes.get(sandbox_id)
        return bool(sandbox and sandbox.running)
