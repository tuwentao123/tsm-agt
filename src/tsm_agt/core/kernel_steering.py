"""Steering queue and its application at iteration boundaries for `Kernel`.

A mixin module in the facade-plus-siblings shape (``run_agent.py`` +
``agent/session_persistence.py``): the facade ``core/kernel.py`` imports
this at module level and adds ``SteeringMixin`` to ``Kernel``'s bases, so
**every method resolves through ``Kernel``'s MRO unchanged** --
``self._dependencies``, ``self._append_events`` and ``Kernel``'s own class
attributes all still resolve. Moved bodies are verbatim: nothing is renamed
and no signature changes.

This module must never bind the facade at module level; late-import it
inside a method instead (the project's module-boundary rule).

Moved from ``core/kernel.py``:

* ``_apply_pending_steering``
* ``get_steering``
* ``queue_steering``
"""

from __future__ import annotations


import json
from dataclasses import replace
from tsm_agt.ports import (
    Message, MessageRole, RuntimeEvent, RuntimeUnitOfWork, TextBlock
)
from uuid import uuid4
from .agent_loop import AgentTurnCheckpoint
from .configuration import canonical_hash
from .steering import SteeringKind, SteeringProjection, SteeringProjector
from .task import TaskSnapshot, TaskState
from .task_spec import TaskSpecProjector, TaskSpecSnapshot
from .turn import InvalidTurnState
from .working_memory import WorkingMemorySnapshot


class SteeringMixin:
    async def get_steering(self, task_id: str) -> SteeringProjection:
        await self.get_task(task_id)
        events = await self._dependencies.store.read_events(task_id)
        return SteeringProjector.project(task_id, events)

    async def queue_steering(
        self, task_id: str, kind: SteeringKind, text: str, steering_id: str,
    ) -> SteeringProjection:
        normalized = text.strip()
        if not normalized or not steering_id.strip():
            raise ValueError("steering text and identity must not be empty")
        if len(normalized) > 20_000:
            raise ValueError("steering text exceeds 20000 characters")
        for attempt in range(5):
            stored = await self._require_stored_task(task_id)
            task = TaskSnapshot.from_data(stored.data)
            if task.state not in {
                TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW,
                TaskState.AWAITING_APPROVAL, TaskState.AWAITING_USER,
                TaskState.INTERRUPTED, TaskState.CONFLICT,
            }:
                raise InvalidTurnState(
                    f"steering requires an active Task, got {task.state.value}"
                )
            events = await self._dependencies.store.read_events(task_id)
            projection = SteeringProjector.project(task_id, events)
            existing = next((
                event for event in events
                if event.event_type == "steering.queued"
                and event.payload.get("steering_id") == steering_id
            ), None)
            request_hash = canonical_hash({"kind": kind.value, "text": normalized})
            if existing is not None:
                if existing.payload.get("request_hash") != request_hash:
                    raise ValueError("steering_id was reused with different input")
                return projection
            event = RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
                "steering.queued", {
                    "steering_id": steering_id,
                    "inbound_sequence": projection.latest_inbound_sequence + 1,
                    "kind": kind.value, "text": normalized,
                    "text_hash": canonical_hash(normalized),
                    "request_hash": request_hash,
                },
            )
            try:
                await self._dependencies.store.commit(RuntimeUnitOfWork(
                    task_id, stored.version, task.to_data(), (event,)
                ))
                return SteeringProjector.project(task_id, events + (event,))
            except ValueError as error:
                if "version conflict" not in str(error) or attempt == 4:
                    raise
        raise RuntimeError("unreachable steering retry state")

    async def _apply_pending_steering(
        self, checkpoint: AgentTurnCheckpoint, safe_point: str,
    ) -> tuple[AgentTurnCheckpoint, bool]:
        """Atomically merge ordered input only at a model-safe boundary.

        Steering is accepted and durably queued by ``queue_steering`` while a
        tool runs.  It is deliberately not inserted into Provider messages
        until every call from the current assistant tool batch has a result.
        """
        if checkpoint.pending_tool_calls or (
            checkpoint.tool_batch is not None and checkpoint.tool_batch.is_open
        ):
            return checkpoint, False
        for attempt in range(5):
            stored = await self._require_stored_task(checkpoint.task_id)
            task = TaskSnapshot.from_data(stored.data)
            events = await self._dependencies.store.read_events(checkpoint.task_id)
            projection = SteeringProjector.project(checkpoint.task_id, events)
            pending = tuple(
                item for item in projection.pending
                if item.inbound_sequence > checkpoint.last_steering_inbound_sequence
            )
            if not pending:
                return checkpoint, False
            messages = list(checkpoint.messages)
            goal_revision = checkpoint.goal_revision
            replaced = False
            replacement_goal: str | None = None
            applied_ids: list[str] = []
            for item in pending:
                if item.kind is SteeringKind.REPLACE:
                    goal_revision += 1
                    replaced = True
                    replacement_goal = item.text
                    boundary = "user_goal_replacement"
                    warning = (
                        "This replaces only unstarted work. Previously committed "
                        "tool executions, workspace mutations, processes, and evidence "
                        "remain facts and are not undone."
                    )
                else:
                    boundary = "user_runtime_steering"
                    warning = (
                        "Apply this user update to future actions at this safe point; "
                        "it grants no additional tool authority or approval."
                    )
                body = json.dumps({
                    "boundary": boundary, "warning": warning,
                    "kind": item.kind.value,
                    "inbound_sequence": item.inbound_sequence,
                    "goal_revision": goal_revision, "text": item.text,
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                messages.append(Message(
                    f"steering-{item.inbound_sequence}-{item.text_hash[:16]}",
                    MessageRole.USER, (TextBlock(body),),
                    scaffolding="steering",
                ))
                applied_ids.append(item.steering_id)
            working_memory_event: RuntimeEvent | None = None
            working_memory_hash = checkpoint.working_memory_hash
            current_spec = TaskSpecProjector.project(
                checkpoint.task_id, task.goal, events
            )
            spec_goal = current_spec.goal
            spec_scope = current_spec.scope
            spec_constraints = list(current_spec.constraints)
            spec_criteria = current_spec.acceptance_criteria
            spec_outcomes = current_spec.outcomes
            continuation_mode = current_spec.continuation_mode
            schema_version = current_spec.schema_version
            for item in pending:
                if item.kind is SteeringKind.REPLACE:
                    replacement_spec = TaskSpecSnapshot.initial(
                        checkpoint.task_id, item.text
                    )
                    spec_goal = item.text
                    spec_scope = replacement_spec.scope
                    spec_constraints = []
                    spec_criteria = replacement_spec.acceptance_criteria
                    # Replace starts a new contract revision.  Old outcomes remain
                    # facts in prior events but are not silently carried into the
                    # replacement goal.  B2 will populate the replacement outcomes
                    # from a validated model proposal.
                    spec_outcomes = replacement_spec.outcomes
                    continuation_mode = replacement_spec.continuation_mode
                    schema_version = replacement_spec.schema_version
            # Ordinary steering is conversation input, not a durable Task
            # constraint. Only explicit Replace revises the Task spec; a
            # model may later propose structured scope/constraint/focus changes.
            updated_spec = current_spec
            task_spec_event: RuntimeEvent | None = None
            if replaced:
                updated_spec = TaskSpecSnapshot(
                    task_id=checkpoint.task_id,
                    revision=current_spec.revision + 1, goal=spec_goal,
                    scope=spec_scope, constraints=tuple(spec_constraints),
                    acceptance_criteria=spec_criteria, outcomes=spec_outcomes,
                    continuation_mode=continuation_mode,
                    schema_version=schema_version,
                )
                task_spec_event = RuntimeEvent(
                    f"evt-{uuid4().hex}", checkpoint.task_id,
                    stored.last_event_sequence + 2, "task_spec.revised", {
                        "operation_id": canonical_hash({
                            "kind": "runtime-steering",
                            "steering_ids": applied_ids,
                        }),
                        "request_hash": canonical_hash(updated_spec.to_data()),
                        "writer": "runtime-steering",
                        "revision": updated_spec.revision,
                        "content_hash": updated_spec.content_hash,
                        "snapshot": updated_spec.to_data(),
                    },
                )
            if replacement_goal is not None:
                current_memory = self._dependencies.working_memory_projector.project(
                    checkpoint.task_id, task.goal, events
                )
                memory_sequence = stored.last_event_sequence + 3
                memory_state = current_memory.state_data()
                memory_state["goal"] = replacement_goal
                updated_memory = WorkingMemorySnapshot.from_update(
                    task_id=checkpoint.task_id,
                    revision=current_memory.revision + 1,
                    state=memory_state,
                    source_event_sequences=(
                        current_memory.source_event_sequences + (memory_sequence,)
                    ),
                )
                working_memory_hash = updated_memory.content_hash
                operation_id = canonical_hash({
                    "kind": "steering.replace",
                    "steering_ids": applied_ids,
                    "goal_revision": goal_revision,
                })
                working_memory_event = RuntimeEvent(
                    f"evt-{uuid4().hex}", checkpoint.task_id, memory_sequence,
                    "working_memory.updated", {
                        "operation_id": operation_id,
                        "request_hash": canonical_hash({
                            "expected_revision": current_memory.revision,
                            "state": memory_state,
                        }),
                        "writer": "runtime-steering",
                        "revision": updated_memory.revision,
                        "content_hash": updated_memory.content_hash,
                        "snapshot": updated_memory.to_data(),
                    },
                )
            updated_checkpoint = replace(
                checkpoint, revision=checkpoint.revision + 1,
                messages=tuple(messages),
                pending_tool_calls=checkpoint.pending_tool_calls,
                last_steering_inbound_sequence=pending[-1].inbound_sequence,
                goal_revision=goal_revision,
                working_memory_hash=working_memory_hash,
                exploration_budget_state=(
                    {} if replaced else checkpoint.exploration_budget_state
                ),
                stop_or_pivot_state=(
                    {} if replaced else checkpoint.stop_or_pivot_state
                ),
                evidence_relation_state=(
                    {} if replaced else checkpoint.evidence_relation_state
                ),
                rejection_loop_state=(
                    {} if replaced else checkpoint.rejection_loop_state
                ),
                exploration_outcome_state=(
                    {} if replaced else checkpoint.exploration_outcome_state
                ),
                finalization_state=(
                    {} if replaced else checkpoint.finalization_state
                ),
            )
            updated_task = task.with_agent_checkpoint(updated_checkpoint.to_data())
            event = RuntimeEvent(
                f"evt-{uuid4().hex}", checkpoint.task_id,
                stored.last_event_sequence + 1, "steering.applied", {
                    "turn_id": checkpoint.turn_id,
                    "steering_ids": applied_ids,
                    "inbound_sequences": [item.inbound_sequence for item in pending],
                    "safe_point": safe_point, "goal_revision": goal_revision,
                    "replaced_pending_tool_calls": 0,
                    "committed_mutation_count": len(task.mutation_journal),
                    "completed_tool_execution_count": len(task.tool_executions),
                    "checkpoint_hash": updated_checkpoint.checkpoint_hash,
                    "working_memory_revised": working_memory_event is not None,
                    "task_spec_revision": updated_spec.revision,
                    "task_spec_hash": updated_spec.content_hash,
                },
            )
            try:
                committed = [event]
                if task_spec_event is not None:
                    committed.append(task_spec_event)
                if working_memory_event is not None:
                    committed.append(working_memory_event)
                await self._dependencies.store.commit(RuntimeUnitOfWork(
                    checkpoint.task_id, stored.version, updated_task.to_data(),
                    tuple(committed),
                ))
                return updated_checkpoint, replaced
            except ValueError as error:
                if "version conflict" not in str(error) or attempt == 4:
                    raise
        raise RuntimeError("unreachable steering apply retry state")
