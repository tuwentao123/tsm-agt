"""Pure deterministic shadow-mode Task binding decisions."""

from __future__ import annotations

from dataclasses import dataclass

from .types import TaskBindingDecision, TaskBindingKind


_EXECUTING_STATES = frozenset({"EXECUTING", "RUNNING_WORKFLOW"})
_RESUMABLE_CONTINUATION_STATES = _EXECUTING_STATES | {"INTERRUPTED", "CONFLICT"}
_AWAITING_USER_STATE = "AWAITING_USER"
_AWAITING_APPROVAL_STATE = "AWAITING_APPROVAL"
_ACTIVE_STATES = _EXECUTING_STATES | {
    _AWAITING_USER_STATE,
    _AWAITING_APPROVAL_STATE,
}


@dataclass(frozen=True, slots=True)
class TaskBindingTask:
    """Read-only bounded Task facts relevant to deterministic input binding."""

    task_id: str
    state: str
    has_unfinished_work: bool = False

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task binding task requires a task_id")
        if not self.state.strip():
            raise ValueError("task binding task requires a state")


@dataclass(frozen=True, slots=True)
class TaskBindingRequest:
    """Bounded, text-free input for one deterministic binding decision."""

    current_input_event_ref: str
    tasks: tuple[TaskBindingTask, ...]
    explicit_task_id: str | None = None
    new_task_requested: bool = False
    continuation_requested: bool = False
    selected_interaction_task_id: str | None = None
    approval_resolution_task_id: str | None = None

    def __post_init__(self) -> None:
        if not self.current_input_event_ref.strip():
            raise ValueError("task binding requires a current input event reference")
        task_ids = tuple(item.task_id for item in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task binding tasks must have unique task_ids")


def bind_task_input(request: TaskBindingRequest) -> TaskBindingDecision:
    """Return a decision using explicit routing and bounded continuation facts."""
    by_id = {item.task_id: item for item in request.tasks}
    if request.explicit_task_id is not None:
        if request.explicit_task_id not in by_id:
            raise ValueError("explicit task binding reference is not in the Session")
        return TaskBindingDecision(
            schema_version=1, task_id=request.explicit_task_id,
            binding_kind=TaskBindingKind.EXPLICIT_REFERENCE,
            current_input_event_ref=request.current_input_event_ref,
            requires_runtime_inspection=True,
        )
    if request.new_task_requested:
        return TaskBindingDecision(
            schema_version=1, task_id=None, binding_kind=TaskBindingKind.NEW,
            current_input_event_ref=request.current_input_event_ref,
            reason_code="explicit_new_task",
        )
    if request.continuation_requested:
        resumable = tuple(
            task for task in request.tasks
            if task.state in _RESUMABLE_CONTINUATION_STATES
            and task.has_unfinished_work
        )
        if len(resumable) == 1:
            return TaskBindingDecision(
                schema_version=1, task_id=resumable[0].task_id,
                binding_kind=TaskBindingKind.EXPLICIT_CONTINUATION,
                current_input_event_ref=request.current_input_event_ref,
                continuation_requested=True,
                candidate_task_ids=(resumable[0].task_id,),
                reason_code="unique_unfinished_continuation",
            )
        if len(resumable) > 1:
            return TaskBindingDecision(
                schema_version=1, task_id=None,
                binding_kind=TaskBindingKind.CHOICE_REQUIRED,
                current_input_event_ref=request.current_input_event_ref,
                continuation_requested=True,
                candidate_task_ids=tuple(task.task_id for task in resumable),
                reason_code="ambiguous_unfinished_continuation",
            )

    active_tasks = tuple(item for item in request.tasks if item.state in _ACTIVE_STATES)
    if not active_tasks:
        return TaskBindingDecision(
            schema_version=1, task_id=None, binding_kind=TaskBindingKind.NEW,
            current_input_event_ref=request.current_input_event_ref,
            continuation_requested=request.continuation_requested,
            reason_code="no_active_task",
        )
    if len(active_tasks) != 1:
        return TaskBindingDecision(
            schema_version=1, task_id=None,
            binding_kind=TaskBindingKind.CHOICE_REQUIRED,
            current_input_event_ref=request.current_input_event_ref,
            continuation_requested=request.continuation_requested,
            candidate_task_ids=tuple(task.task_id for task in active_tasks),
            reason_code="multiple_active_tasks",
        )
    active = active_tasks[0]
    if active.state in _EXECUTING_STATES:
        return TaskBindingDecision(
            schema_version=1, task_id=active.task_id,
            binding_kind=TaskBindingKind.ACTIVE_CONTINUATION,
            current_input_event_ref=request.current_input_event_ref,
            continuation_requested=request.continuation_requested,
        )
    if active.state == _AWAITING_USER_STATE:
        if request.selected_interaction_task_id == active.task_id:
            return TaskBindingDecision(
                schema_version=1, task_id=active.task_id,
                binding_kind=TaskBindingKind.USER_INTERACTION,
                current_input_event_ref=request.current_input_event_ref,
            )
        return TaskBindingDecision(
            schema_version=1, task_id=None,
            binding_kind=TaskBindingKind.CHOICE_REQUIRED,
            current_input_event_ref=request.current_input_event_ref,
            continuation_requested=request.continuation_requested,
            candidate_task_ids=(active.task_id,),
            reason_code="awaiting_user_requires_typed_interaction",
        )
    assert active.state == _AWAITING_APPROVAL_STATE
    if request.approval_resolution_task_id == active.task_id:
        return TaskBindingDecision(
            schema_version=1, task_id=active.task_id,
            binding_kind=TaskBindingKind.USER_INTERACTION,
            current_input_event_ref=request.current_input_event_ref,
        )
    return TaskBindingDecision(
        schema_version=1, task_id=active.task_id,
        binding_kind=TaskBindingKind.QUEUED_AFTER_APPROVAL,
        current_input_event_ref=request.current_input_event_ref,
        continuation_requested=request.continuation_requested,
        candidate_task_ids=(active.task_id,),
        reason_code="awaiting_approval_requires_typed_resolution",
    )
