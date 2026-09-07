from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    CommitResult,
    HealthState,
    HealthStatus,
    AdapterContext,
    AdapterDescriptor,
    RuntimeEvent,
    RuntimeUnitOfWork,
    StoredTask,
    StoredProjectTrust,
    StoredProjectOnboarding,
    SessionEvent, StoredSession, SessionUnitOfWork, SessionTaskUnitOfWork,
    RuntimeCommandRecord,
)


class InMemoryRuntimeStore:
    descriptor = AdapterDescriptor(
        adapter_id="fixture.memory-runtime-store",
        adapter_version="0.1.0",
        port_name="RuntimeStorePort",
        port_version="1.0",
        capabilities=frozenset({"atomic-commit", "event-cursor"}),
    )

    def __init__(self) -> None:
        self._started = False
        self._tasks: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[RuntimeEvent]] = {}
        self._versions: dict[str, int] = {}
        self._project_trust: dict[tuple[str, str], StoredProjectTrust] = {}
        self._project_onboarding: dict[
            tuple[str, str], StoredProjectOnboarding
        ] = {}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._session_events: dict[str, list[SessionEvent]] = {}
        self._session_versions: dict[str, int] = {}
        self._session_task_commands: dict[str, tuple[str, str]] = {}
        self._runtime_commands: dict[str, RuntimeCommandRecord] = {}

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        state = HealthState.HEALTHY if self._started else HealthState.UNHEALTHY
        return HealthStatus(state, "in-memory store ready" if self._started else "not started")

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def commit(self, unit: RuntimeUnitOfWork) -> CommitResult:
        current = self._versions.get(unit.task_id, 0)
        if current != unit.expected_version:
            raise ValueError(
                f"version conflict for {unit.task_id}: expected {unit.expected_version}, got {current}"
            )
        existing_events = self._events.get(unit.task_id, [])
        last_sequence = existing_events[-1].sequence if existing_events else 0
        expected_sequence = last_sequence + 1
        for event in unit.events:
            if event.task_id != unit.task_id:
                raise ValueError("event task_id must match unit task_id")
            if event.sequence != expected_sequence:
                raise ValueError(
                    f"event sequence must be {expected_sequence}, got {event.sequence}"
                )
            expected_sequence += 1

        committed = current + 1
        session_id = str(unit.next_state.get("session_id") or "") or None
        events = tuple(
            event if event.session_id is not None else replace(
                event, session_id=session_id
            )
            for event in unit.events
        )
        self._tasks[unit.task_id] = dict(unit.next_state)
        self._events.setdefault(unit.task_id, []).extend(events)
        self._versions[unit.task_id] = committed
        return CommitResult(unit.task_id, committed)

    async def read_events(
        self, task_id: str, after_sequence: int = 0
    ) -> tuple[RuntimeEvent, ...]:
        return tuple(
            event for event in self._events.get(task_id, ())
            if event.sequence > after_sequence
        )

    async def load_task(self, task_id: str) -> StoredTask | None:
        state = self._tasks.get(task_id)
        if state is None:
            return None
        events = self._events.get(task_id, [])
        last_sequence = events[-1].sequence if events else 0
        return StoredTask(
            task_id=task_id,
            version=self._versions[task_id],
            last_event_sequence=last_sequence,
            data=dict(state),
        )

    async def commit_session(self, unit: SessionUnitOfWork) -> CommitResult:
        current = self._session_versions.get(unit.session_id, 0)
        if current != unit.expected_version:
            raise ValueError(
                f"session version conflict for {unit.session_id}: expected "
                f"{unit.expected_version}, got {current}"
            )
        events = self._session_events.get(unit.session_id, [])
        expected = (events[-1].sequence if events else 0) + 1
        for event in unit.events:
            if event.session_id != unit.session_id or event.sequence != expected:
                raise ValueError("invalid Session Event identity or sequence")
            expected += 1
        committed = current + 1
        self._sessions[unit.session_id] = dict(unit.next_state)
        self._session_events.setdefault(unit.session_id, []).extend(unit.events)
        self._session_versions[unit.session_id] = committed
        return CommitResult(unit.session_id, committed)

    async def commit_session_and_task(
        self, unit: SessionTaskUnitOfWork
    ) -> tuple[CommitResult, CommitResult]:
        prior = self._session_task_commands.get(unit.command_id)
        identity = (unit.session.session_id, unit.task.task_id)
        if prior is not None:
            if prior != identity:
                raise ValueError("session command_id was reused with different targets")
            session = await self.load_session(unit.session.session_id)
            task = await self.load_task(unit.task.task_id)
            assert session is not None and task is not None
            return (
                CommitResult(session.session_id, session.version),
                CommitResult(task.task_id, task.version),
            )
        # Validate on copies so a failure cannot partially mutate the fixture.
        if self._session_versions.get(unit.session.session_id, 0) != unit.session.expected_version:
            raise ValueError("session version conflict")
        if self._versions.get(unit.task.task_id, 0) != unit.task.expected_version:
            raise ValueError("version conflict")
        prior_session = self._sessions.get(unit.session.session_id)
        prior_session_copy = dict(prior_session) if prior_session is not None else None
        prior_version = self._session_versions.get(unit.session.session_id)
        prior_events = list(self._session_events.get(unit.session.session_id, ()))
        session_result = await self.commit_session(unit.session)
        try:
            task_result = await self.commit(unit.task)
        except Exception:
            # Fixture rollback for the just-committed Session UoW.
            if prior_session_copy is None:
                self._sessions.pop(unit.session.session_id, None)
                self._session_versions.pop(unit.session.session_id, None)
                self._session_events.pop(unit.session.session_id, None)
            else:
                self._sessions[unit.session.session_id] = prior_session_copy
                assert prior_version is not None
                self._session_versions[unit.session.session_id] = prior_version
                self._session_events[unit.session.session_id] = prior_events
            raise
        self._session_task_commands[unit.command_id] = identity
        return session_result, task_result

    async def load_session(self, session_id: str) -> StoredSession | None:
        state = self._sessions.get(session_id)
        if state is None:
            return None
        events = self._session_events.get(session_id, [])
        return StoredSession(
            session_id, self._session_versions[session_id],
            events[-1].sequence if events else 0, dict(state),
        )

    async def load_session_task_command(
        self, command_id: str
    ) -> tuple[str, str] | None:
        return self._session_task_commands.get(command_id)

    async def claim_runtime_command(
        self, command: RuntimeCommandRecord
    ) -> RuntimeCommandRecord:
        prior = self._runtime_commands.get(command.command_id)
        if prior is not None:
            if (
                prior.command_type != command.command_type
                or prior.request_hash != command.request_hash
            ):
                raise ValueError(
                    "command_id was reused with a different Runtime command"
                )
            return prior
        self._runtime_commands[command.command_id] = command
        return command

    async def complete_runtime_command(
        self, command_id: str, request_hash: str, result: Mapping[str, Any]
    ) -> RuntimeCommandRecord:
        prior = self._runtime_commands.get(command_id)
        if prior is None or prior.request_hash != request_hash:
            raise ValueError("Runtime command is missing or does not match")
        completed = RuntimeCommandRecord(
            prior.command_id, prior.command_type, prior.request_hash, "completed",
            dict(result), prior.created_at, datetime.now().astimezone(),
        )
        self._runtime_commands[command_id] = completed
        return completed

    async def list_sessions(
        self, subject: str, *, include_archived: bool = False
    ) -> tuple[StoredSession, ...]:
        result = []
        for session_id, state in self._sessions.items():
            if state.get("subject") != subject:
                continue
            if not include_archived and state.get("state") == "ARCHIVED":
                continue
            loaded = await self.load_session(session_id)
            assert loaded is not None
            result.append(loaded)
        return tuple(sorted(result, key=lambda item: item.session_id))

    async def read_session_events(
        self, session_id: str, after_sequence: int = 0
    ) -> tuple[SessionEvent, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        return tuple(
            event for event in self._session_events.get(session_id, ())
            if event.sequence > after_sequence
        )

    async def find_task_by_pending_approval(
        self, request_id: str
    ) -> StoredTask | None:
        if not request_id.strip():
            raise ValueError("approval request_id must not be empty")
        for task_id, state in self._tasks.items():
            pending = state.get("pending_approval")
            if isinstance(pending, dict) and pending.get("request_id") == request_id:
                return await self.load_task(task_id)
        return None

    async def find_task_by_pending_clarification(
        self, request_id: str
    ) -> StoredTask | None:
        if not request_id.strip():
            raise ValueError("clarification request_id must not be empty")
        for task_id, state in self._tasks.items():
            pending = state.get("pending_clarification")
            if isinstance(pending, dict) and pending.get("request_id") == request_id:
                return await self.load_task(task_id)
        return None

    async def load_project_trust(
        self, workspace: str, subject: str
    ) -> StoredProjectTrust | None:
        return self._project_trust.get((workspace, subject))

    async def save_project_trust(self, trust: StoredProjectTrust) -> None:
        self._project_trust[(trust.workspace, trust.subject)] = trust

    async def load_project_onboarding(
        self, workspace: str, subject: str
    ) -> StoredProjectOnboarding | None:
        return self._project_onboarding.get((workspace, subject))

    async def save_project_onboarding(
        self, onboarding: StoredProjectOnboarding
    ) -> None:
        self._project_onboarding[(onboarding.workspace, onboarding.subject)] = onboarding
