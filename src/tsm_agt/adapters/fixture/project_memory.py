"""Deterministic in-memory ProjectMemoryPort fixture."""

from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
    MemoryOperationResult, StoredMemory,
)


class InMemoryProjectMemoryStore:
    descriptor = AdapterDescriptor(
        "fixture.memory-project-memory", "0.1.0", "ProjectMemoryPort",
        "1.0", frozenset({"persistent-contract", "idempotent-operations"}),
    )

    def __init__(self) -> None:
        self._started = False
        self._memories: dict[str, StoredMemory] = {}
        self._operations: dict[str, tuple[str, str, StoredMemory | None]] = {}

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "in-memory project memory ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def load_memory(self, memory_id: str) -> StoredMemory | None:
        return self._memories.get(memory_id)

    async def list_memories(
        self, *, subject: str, workspace: str, task_id: str, session_id: str
    ) -> tuple[StoredMemory, ...]:
        return tuple(sorted((
            memory for memory in self._memories.values()
            if memory.subject == subject and (
                memory.scope == "USER"
                or (memory.scope == "PROJECT" and memory.workspace == workspace)
                or (memory.scope == "TASK" and memory.task_id == task_id)
                # Compatibility for records written before SESSION gained a
                # session_id and was renamed to TASK.
                or (memory.scope == "SESSION" and memory.session_id is None
                    and memory.task_id == task_id)
                or (memory.scope == "SESSION" and memory.session_id == session_id)
            )
        ), key=lambda item: item.memory_id))

    async def save_memory(
        self, memory: StoredMemory, operation_id: str
    ) -> MemoryOperationResult:
        prior = self._operations.get(operation_id)
        if prior is not None:
            if prior[:2] != ("save", memory.memory_id):
                raise ValueError("memory operation_id was reused with different input")
            return MemoryOperationResult(prior[2], replayed=True)
        self._memories[memory.memory_id] = memory
        self._operations[operation_id] = ("save", memory.memory_id, memory)
        return MemoryOperationResult(memory)

    async def delete_memory(
        self, memory_id: str, subject: str, operation_id: str
    ) -> MemoryOperationResult:
        prior = self._operations.get(operation_id)
        if prior is not None:
            if prior[:2] != ("delete", memory_id):
                raise ValueError("memory operation_id was reused with different input")
            return MemoryOperationResult(prior[2], replayed=True)
        memory = self._memories.get(memory_id)
        if memory is not None and memory.subject != subject:
            raise PermissionError("memory belongs to another local subject")
        if memory is not None:
            del self._memories[memory_id]
        self._operations[operation_id] = ("delete", memory_id, memory)
        return MemoryOperationResult(memory)
