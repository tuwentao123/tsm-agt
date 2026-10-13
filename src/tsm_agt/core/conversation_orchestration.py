"""Conversation-only task orchestration and deterministic input binding."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tsm_agt.ports import RuntimeEvent


class OrchestrationMode(StrEnum):
    """The sole executable orchestration path."""

    CONVERSATION_LOOP = "CONVERSATION_LOOP"


class TaskBindingKind(StrEnum):
    NEW = "NEW"
    EXPLICIT_REFERENCE = "EXPLICIT_REFERENCE"
    ACTIVE_CONTINUATION = "ACTIVE_CONTINUATION"
    EXPLICIT_CONTINUATION = "EXPLICIT_CONTINUATION"
    USER_SELECTED = "USER_SELECTED"
    USER_INTERACTION = "USER_INTERACTION"
    QUEUED_AFTER_APPROVAL = "QUEUED_AFTER_APPROVAL"
    CHOICE_REQUIRED = "CHOICE_REQUIRED"
    CONVERSATION_CONTINUATION = "CONVERSATION_CONTINUATION"


@dataclass(frozen=True, slots=True)
class TaskOrchestrationMetadata:
    schema_version: int
    orchestration_mode: OrchestrationMode
    root_goal: str
    root_input_event_ref: str = ""
    latest_input_event_ref: str = ""
    binding_kind: TaskBindingKind = TaskBindingKind.NEW

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported task orchestration schema version")
        if not self.root_goal.strip() or len(self.root_goal) > 10_000:
            raise ValueError("task root goal must contain 1..10000 characters")

    def to_data(self) -> dict[str, str | int]:
        return {
            "schema_version": self.schema_version,
            "orchestration_mode": self.orchestration_mode.value,
            "root_goal": self.root_goal,
            "root_input_event_ref": self.root_input_event_ref,
            "latest_input_event_ref": self.latest_input_event_ref,
            "binding_kind": self.binding_kind.value,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> TaskOrchestrationMetadata:
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            orchestration_mode=OrchestrationMode.CONVERSATION_LOOP,
            root_goal=str(data["root_goal"]),
            root_input_event_ref=str(data.get("root_input_event_ref", "")),
            latest_input_event_ref=str(data.get("latest_input_event_ref", "")),
            binding_kind=TaskBindingKind(str(data.get("binding_kind", "NEW"))),
        )

    @classmethod
    def from_task_created_payload(
        cls, payload: Mapping[str, Any], *, fallback_goal: str,
    ) -> TaskOrchestrationMetadata:
        # Historical orchestration metadata is decoder-only. Every restored
        # task now executes through the conversation loop.
        raw = payload.get("conversation")
        if not isinstance(raw, Mapping):
            raw = payload.get("single_loop")
        if not isinstance(raw, Mapping):
            return cls(1, OrchestrationMode.CONVERSATION_LOOP, fallback_goal)
        return cls.from_data({**raw, "root_goal": raw.get("root_goal", fallback_goal)})


class TaskOrchestrationProjector:
    @staticmethod
    def project(
        task_id: str, fallback_goal: str, events: Sequence[RuntimeEvent],
    ) -> TaskOrchestrationMetadata:
        created = next((
            event for event in events
            if event.task_id == task_id and event.event_type == "task.created"
        ), None)
        if created is None:
            raise ValueError(f"task {task_id} has no task.created event")
        goal = str(created.payload.get("goal") or fallback_goal)
        return TaskOrchestrationMetadata.from_task_created_payload(
            created.payload, fallback_goal=goal
        )


@dataclass(frozen=True, slots=True)
class TaskBindingDecision:
    schema_version: int
    task_id: str | None
    binding_kind: TaskBindingKind
    current_input_event_ref: str
    source_task_id: str | None = None
    requires_runtime_inspection: bool = False
    continuation_requested: bool = False
    candidate_task_ids: tuple[str, ...] = ()
    reason_code: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not self.current_input_event_ref.strip():
            raise ValueError("invalid task binding decision")
        if self.binding_kind not in {TaskBindingKind.NEW, TaskBindingKind.CHOICE_REQUIRED} and not self.task_id:
            raise ValueError("task binding requires a task_id")

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "binding_kind": self.binding_kind.value,
            "current_input_event_ref": self.current_input_event_ref,
            "source_task_id": self.source_task_id,
            "requires_runtime_inspection": self.requires_runtime_inspection,
            "continuation_requested": self.continuation_requested,
            "candidate_task_ids": list(self.candidate_task_ids),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class TaskBindingTask:
    task_id: str
    state: str
    has_unfinished_work: bool = False
    task_kind: str = "CONVERSATION"


@dataclass(frozen=True, slots=True)
class TaskBindingRequest:
    current_input_event_ref: str
    tasks: tuple[TaskBindingTask, ...]
    explicit_task_id: str | None = None
    new_task_requested: bool = False
    continuation_requested: bool = False
    selected_interaction_task_id: str | None = None
    approval_resolution_task_id: str | None = None


def _decision(
    request: TaskBindingRequest, kind: TaskBindingKind, task_id: str | None = None,
    *, candidates: tuple[str, ...] = (), reason: str = "",
) -> TaskBindingDecision:
    return TaskBindingDecision(
        1, task_id, kind, request.current_input_event_ref,
        continuation_requested=request.continuation_requested,
        candidate_task_ids=candidates, reason_code=reason,
    )


def bind_task_input(request: TaskBindingRequest) -> TaskBindingDecision:
    """Bind using explicit identifiers and durable state, never message text."""
    by_id = {task.task_id: task for task in request.tasks}
    if request.explicit_task_id is not None:
        if request.explicit_task_id not in by_id:
            raise ValueError("explicit task binding reference is not in the Session")
        return TaskBindingDecision(
            1, request.explicit_task_id, TaskBindingKind.EXPLICIT_REFERENCE,
            request.current_input_event_ref, requires_runtime_inspection=True,
        )
    if request.new_task_requested:
        return _decision(request, TaskBindingKind.NEW, reason="explicit_new_task")
    if request.selected_interaction_task_id in by_id:
        return _decision(request, TaskBindingKind.USER_INTERACTION, request.selected_interaction_task_id)
    if request.approval_resolution_task_id in by_id:
        return _decision(request, TaskBindingKind.USER_INTERACTION, request.approval_resolution_task_id)
    resumable = tuple(task for task in request.tasks if task.has_unfinished_work and task.state in {"EXECUTING", "RUNNING_WORKFLOW", "INTERRUPTED", "CONFLICT"})
    if request.continuation_requested and len(resumable) == 1:
        return _decision(request, TaskBindingKind.EXPLICIT_CONTINUATION, resumable[0].task_id, candidates=(resumable[0].task_id,))
    if request.continuation_requested and len(resumable) > 1:
        return _decision(request, TaskBindingKind.CHOICE_REQUIRED, candidates=tuple(task.task_id for task in resumable), reason="ambiguous_unfinished_continuation")
    continuable = tuple(task for task in request.tasks if task.task_kind == "CONVERSATION" and task.state in {"AWAITING_INPUT", "INTERRUPTED", "CONFLICT"})
    if len(continuable) == 1:
        return _decision(request, TaskBindingKind.CONVERSATION_CONTINUATION, continuable[0].task_id)
    active = tuple(task for task in request.tasks if task.state in {"EXECUTING", "RUNNING_WORKFLOW", "AWAITING_USER", "AWAITING_APPROVAL"})
    if not active:
        return _decision(request, TaskBindingKind.NEW, reason="no_active_task")
    if len(active) > 1 or len(continuable) > 1:
        choices = continuable or active
        return _decision(request, TaskBindingKind.CHOICE_REQUIRED, candidates=tuple(task.task_id for task in choices), reason="multiple_active_tasks")
    kind = TaskBindingKind.ACTIVE_CONTINUATION if active[0].state in {"EXECUTING", "RUNNING_WORKFLOW"} else TaskBindingKind.CHOICE_REQUIRED
    return _decision(request, kind, active[0].task_id if kind is not TaskBindingKind.CHOICE_REQUIRED else None, candidates=(active[0].task_id,) if kind is TaskBindingKind.CHOICE_REQUIRED else ())
