"""Atomic runtime state and event storage port."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .adapter import RuntimeAdapter


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_id: str
    task_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: int = 1
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class StoredTask:
    """Provider-neutral persisted task record with concurrency metadata."""

    task_id: str
    version: int
    last_event_sequence: int
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SessionEvent:
    event_id: str
    session_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class StoredSession:
    session_id: str
    version: int
    last_event_sequence: int
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RuntimeUnitOfWork:
    task_id: str
    expected_version: int
    next_state: Mapping[str, Any]
    events: tuple[RuntimeEvent, ...]


@dataclass(frozen=True, slots=True)
class CommitResult:
    task_id: str
    committed_version: int


@dataclass(frozen=True, slots=True)
class SessionUnitOfWork:
    session_id: str
    expected_version: int
    next_state: Mapping[str, Any]
    events: tuple[SessionEvent, ...]


@dataclass(frozen=True, slots=True)
class SessionTaskUnitOfWork:
    session: SessionUnitOfWork
    task: RuntimeUnitOfWork
    command_id: str


@dataclass(frozen=True, slots=True)
class StoredProjectTrust:
    workspace: str
    subject: str
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class StoredProjectOnboarding:
    workspace: str
    subject: str
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RuntimeCommandRecord:
    """Durable idempotency receipt for an external Runtime command."""

    command_id: str
    command_type: str
    request_hash: str
    status: str
    result: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime


class RuntimeStorePort(RuntimeAdapter, Protocol):
    async def commit(self, unit: RuntimeUnitOfWork) -> CommitResult: ...

    async def read_events(self, task_id: str, after_sequence: int = 0) -> tuple[RuntimeEvent, ...]: ...

    async def load_task(self, task_id: str) -> StoredTask | None: ...

    async def commit_session(self, unit: SessionUnitOfWork) -> CommitResult: ...

    async def commit_session_and_task(
        self, unit: SessionTaskUnitOfWork
    ) -> tuple[CommitResult, CommitResult]: ...

    async def load_session_task_command(
        self, command_id: str
    ) -> tuple[str, str] | None: ...

    async def claim_runtime_command(
        self, command: RuntimeCommandRecord
    ) -> RuntimeCommandRecord: ...

    async def complete_runtime_command(
        self, command_id: str, request_hash: str, result: Mapping[str, Any]
    ) -> RuntimeCommandRecord: ...

    async def load_session(self, session_id: str) -> StoredSession | None: ...

    async def list_sessions(
        self, subject: str, *, include_archived: bool = False
    ) -> tuple[StoredSession, ...]: ...

    async def read_session_events(
        self, session_id: str, after_sequence: int = 0
    ) -> tuple[SessionEvent, ...]: ...

    async def find_task_by_pending_approval(
        self, request_id: str
    ) -> StoredTask | None: ...

    async def find_task_by_pending_clarification(
        self, request_id: str
    ) -> StoredTask | None: ...

    async def load_project_trust(
        self, workspace: str, subject: str
    ) -> StoredProjectTrust | None: ...

    async def save_project_trust(
        self, trust: StoredProjectTrust
    ) -> None: ...

    async def load_project_onboarding(
        self, workspace: str, subject: str
    ) -> StoredProjectOnboarding | None: ...

    async def save_project_onboarding(
        self, onboarding: StoredProjectOnboarding
    ) -> None: ...
