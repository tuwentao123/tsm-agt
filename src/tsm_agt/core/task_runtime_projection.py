"""Read-only projection of one Task's durable runtime facts.

This module deliberately owns no execution behaviour.  It converts an already
persisted ``TaskSnapshot`` plus ordered ``RuntimeEvent`` records into one
atomically applicable status snapshot for SDK, CLI and Web consumers.  In
particular, transient progress messages can never override a terminal task
state through this projection.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tsm_agt.ports import RuntimeEvent

from .flow import FlowNodeStatus, FlowProjector, FlowProjectionError
from .task import TaskSnapshot, TaskState


class TaskDisplayStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class TaskRuntimePhase(StrEnum):
    PREPARING = "preparing"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    FINALIZING = "finalizing"


class TaskExecutionStatus(StrEnum):
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    INTERRUPTED = "interrupted"
    STOPPED = "stopped"


class TaskVerificationStatus(StrEnum):
    NOT_STARTED = "not_started"
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class TaskRuntimeFailure:
    code: str
    stage: str
    summary: str
    event_sequence: int | None

    def to_data(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "summary": self.summary,
            "event_sequence": self.event_sequence,
        }


@dataclass(frozen=True, slots=True)
class TaskRuntimeTraceItem:
    id: str
    kind: str
    title: str
    status: str
    sequence_start: int
    sequence_end: int | None
    wait_reason: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "sequence_start": self.sequence_start,
            "sequence_end": self.sequence_end,
            "wait_reason": self.wait_reason,
        }


@dataclass(frozen=True, slots=True)
class TaskRuntimeProjection:
    """Complete, display-neutral snapshot of one Task's observable state."""

    task_id: str
    task_state: str
    display_status: TaskDisplayStatus
    phase: TaskRuntimePhase
    execution_status: TaskExecutionStatus
    verification_status: TaskVerificationStatus
    trace_cursor: int
    trace: tuple[TaskRuntimeTraceItem, ...]
    failure: TaskRuntimeFailure | None = None
    waiting_kind: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_state": self.task_state,
            "display_status": self.display_status.value,
            "phase": self.phase.value,
            "execution_status": self.execution_status.value,
            "verification_status": self.verification_status.value,
            "trace_cursor": self.trace_cursor,
            "trace": [item.to_data() for item in self.trace],
            "failure": self.failure.to_data() if self.failure else None,
            "waiting_kind": self.waiting_kind,
        }


class TaskRuntimeProjector:
    """Project durable Task/verification/Flow facts without executing work."""

    @classmethod
    def project(
        cls, task: TaskSnapshot, events: Sequence[RuntimeEvent],
    ) -> TaskRuntimeProjection:
        ordered = tuple(sorted(events, key=lambda item: item.sequence))
        verification = cls._verification_status(task, ordered)
        phase = cls._phase(task, verification, ordered)
        display = cls._display_status(task.state)
        execution, waiting_kind = cls._execution_status(task)
        failure = cls._failure(task, ordered, verification)
        trace = cls._trace(ordered)
        return TaskRuntimeProjection(
            task.task_id,
            task.state.value,
            display,
            phase,
            execution,
            verification,
            ordered[-1].sequence if ordered else 0,
            trace[-120:],
            failure,
            waiting_kind,
        )

    @staticmethod
    def _display_status(state: TaskState) -> TaskDisplayStatus:
        if state in {
            TaskState.CREATED,
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.PLANNING,
        }:
            return TaskDisplayStatus.PENDING
        if state in {TaskState.AWAITING_APPROVAL, TaskState.AWAITING_USER}:
            return TaskDisplayStatus.WAITING
        if state in {TaskState.INTERRUPTING, TaskState.INTERRUPTED, TaskState.CONFLICT}:
            return TaskDisplayStatus.INTERRUPTED
        if state is TaskState.SUCCEEDED:
            return TaskDisplayStatus.COMPLETED
        if state is TaskState.CANCELLED:
            return TaskDisplayStatus.CANCELLED
        if state is TaskState.FAILED:
            return TaskDisplayStatus.FAILED
        return TaskDisplayStatus.RUNNING

    @staticmethod
    def _execution_status(
        task: TaskSnapshot,
    ) -> tuple[TaskExecutionStatus, str | None]:
        # Terminal state always wins over stale event/progress state.
        if task.state.is_terminal:
            return TaskExecutionStatus.STOPPED, None
        if task.state is TaskState.AWAITING_APPROVAL or task.pending_approval:
            return TaskExecutionStatus.WAITING_APPROVAL, "approval"
        if task.state is TaskState.AWAITING_USER or task.pending_clarification:
            return TaskExecutionStatus.WAITING_INPUT, "input"
        if task.state in {TaskState.INTERRUPTING, TaskState.INTERRUPTED, TaskState.CONFLICT}:
            return TaskExecutionStatus.INTERRUPTED, None
        return TaskExecutionStatus.RUNNING, None

    @staticmethod
    def _verification_status(
        task: TaskSnapshot,
        events: Sequence[RuntimeEvent],
    ) -> TaskVerificationStatus:
        for event in reversed(events):
            if event.event_type != "verify.completed":
                continue
            raw = str(event.payload.get("status") or "blocked")
            return {
                "passed": TaskVerificationStatus.PASSED,
                "failed": TaskVerificationStatus.FAILED,
                "blocked": TaskVerificationStatus.BLOCKED,
            }.get(raw, TaskVerificationStatus.BLOCKED)
        if task.state is TaskState.VERIFYING:
            return TaskVerificationStatus.RUNNING
        if task.state is TaskState.AWAITING_APPROVAL:
            return TaskVerificationStatus.PENDING
        return TaskVerificationStatus.NOT_STARTED

    @staticmethod
    def _phase(
        task: TaskSnapshot,
        verification: TaskVerificationStatus,
        events: Sequence[RuntimeEvent],
    ) -> TaskRuntimePhase:
        if verification in {
            TaskVerificationStatus.RUNNING,
            TaskVerificationStatus.PASSED,
            TaskVerificationStatus.FAILED,
            TaskVerificationStatus.BLOCKED,
        }:
            return TaskRuntimePhase.VERIFYING
        if task.state is TaskState.FINALIZING:
            return TaskRuntimePhase.FINALIZING
        if task.state in {
            TaskState.CREATED,
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.PLANNING,
        }:
            return TaskRuntimePhase.PREPARING
        # A suspended approval can be a verification command.  If a verification
        # phase was already entered, preserve that fact rather than guessing from
        # human-readable progress text.
        if task.state is TaskState.AWAITING_APPROVAL:
            for event in reversed(events):
                if event.event_type == "task.state_changed":
                    if str(event.payload.get("previous_state")) == TaskState.VERIFYING.value:
                        return TaskRuntimePhase.VERIFYING
                    break
        return TaskRuntimePhase.EXECUTING

    @staticmethod
    def _failure(
        task: TaskSnapshot,
        events: Sequence[RuntimeEvent],
        verification: TaskVerificationStatus,
    ) -> TaskRuntimeFailure | None:
        if task.state is not TaskState.FAILED:
            return None
        for event in reversed(events):
            if event.event_type != "verify.criterion_completed":
                continue
            status = str(event.payload.get("status") or "")
            if status not in {"failed", "blocked"}:
                continue
            raw_evidence = event.payload.get("evidence")
            evidence = raw_evidence if isinstance(raw_evidence, list) else []
            observed = next((
                str(item.get("observed") or "").strip()
                for item in evidence if isinstance(item, Mapping)
                and str(item.get("observed") or "").strip()
            ), "verification criterion did not pass")
            return TaskRuntimeFailure(
                str(event.payload.get("criterion_id") or "verification"),
                "verification",
                "verification criterion did not pass",
                event.sequence,
            )
        for event in reversed(events):
            if not event.event_type.endswith(".failed"):
                continue
            return TaskRuntimeFailure(
                event.event_type,
                "execution",
                "execution failed; inspect the task trace for the safe diagnostic",
                event.sequence,
            )
        return TaskRuntimeFailure(
            "task_failed",
            "verification" if verification is TaskVerificationStatus.FAILED else "execution",
            "task entered FAILED without a more specific persisted failure",
            None,
        )

    @staticmethod
    def _trace(events: Sequence[RuntimeEvent]) -> tuple[TaskRuntimeTraceItem, ...]:
        if not events:
            return ()
        try:
            flow = FlowProjector().project(tuple(events))
        except FlowProjectionError:
            # A malformed historical stream must not make status unavailable.
            return ()
        status = {
            FlowNodeStatus.PENDING: "pending",
            FlowNodeStatus.RUNNING: "running",
            FlowNodeStatus.SUCCEEDED: "completed",
            FlowNodeStatus.FAILED: "failed",
            FlowNodeStatus.CANCELLED: "cancelled",
            FlowNodeStatus.UNKNOWN_OUTCOME: "unknown",
            FlowNodeStatus.WAITING_APPROVAL: "waiting_approval",
            FlowNodeStatus.WAITING_USER: "waiting_input",
            FlowNodeStatus.RETRYING: "running",
            FlowNodeStatus.SKIPPED: "skipped",
        }
        return tuple(
            TaskRuntimeTraceItem(
                node.node_id,
                node.kind.value,
                node.label,
                status[node.status],
                node.event_seq_start,
                node.event_seq_end,
                node.wait_reason,
            )
            for node in sorted(flow.nodes, key=lambda item: item.event_seq_start)
        )
