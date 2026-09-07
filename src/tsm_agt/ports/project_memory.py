"""Provider-neutral persistence boundary for durable project memory."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .adapter import RuntimeAdapter


@dataclass(frozen=True, slots=True)
class StoredMemory:
    memory_id: str
    subject: str
    scope: str
    workspace: str | None
    task_id: str | None
    data: Mapping[str, Any]
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryOperationResult:
    memory: StoredMemory | None
    replayed: bool = False


class ProjectMemoryPort(RuntimeAdapter, Protocol):
    async def load_memory(self, memory_id: str) -> StoredMemory | None: ...

    async def list_memories(
        self, *, subject: str, workspace: str, task_id: str, session_id: str
    ) -> tuple[StoredMemory, ...]: ...

    async def save_memory(
        self, memory: StoredMemory, operation_id: str
    ) -> MemoryOperationResult: ...

    async def delete_memory(
        self, memory_id: str, subject: str, operation_id: str
    ) -> MemoryOperationResult: ...
