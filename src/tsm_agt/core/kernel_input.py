"""Runtime input dispatch and routing for `Kernel`.

A mixin module in the Hermes facade+siblings shape (``run_agent.py`` +
``agent/session_persistence.py``): the facade ``core/kernel.py`` imports
this at module level and adds ``InputMixin`` to ``Kernel``'s bases, so
**every method resolves through ``Kernel``'s MRO unchanged** --
``self._dependencies``, ``self._append_events`` and ``Kernel``'s own class
attributes all still resolve. Moved bodies are verbatim: nothing is renamed
and no signature changes.

This module must never bind the facade at module level; late-import it
inside a method instead (Hermes ``AGENTS.md:243-244``).

Moved from ``core/kernel.py``:

* ``_classify_semantic_action``
* ``_handle_approval_resolution``
* ``_handle_cancel_task``
* ``_handle_clarification_reply``
* ``_handle_interrupt_task``
* ``_handle_runtime_text``
* ``_handle_session_text``
* ``_judge_input_relation``
* ``dispatch_input_event``
* ``route_runtime_input``

Module-level symbols that moved with them, transitively. They had no
user outside the moved set, and this module must not import the facade
to reach them, so their home is here now:

* ``_INPUT_HANDLERS``
"""

from __future__ import annotations


from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from tsm_agt.ports import (
    Message, MessageRole, RuntimeEvent, RuntimeUnitOfWork,
    SemanticAction, SessionInputRelation, SessionInputRelationJudgement,
    ToolResult, ToolResultBlock
)
from uuid import uuid4
from .agent_loop import (
    AgentClarificationSuspended, AgentTurnCheckpoint, AgentTurnResult,
    AgentTurnSuspended
)
from .cancellation import note_cancel_requested
from .configuration import canonical_hash
from .evidence_question import (
    EvidenceQuestionProjector, ToolActionDisposition
)
from .runtime_input import (
    ApprovalResolutionInput, CancelTaskInput, ClarificationReplyInput,
    InterruptTaskInput, RuntimeInputContext, RuntimeInputIntent,
    RuntimeInputRoute, RuntimeInputRouter, RuntimeTextInput,
    SessionInputDecision, SessionTextInput
)
from .steering import SteeringKind, SteeringProjector
from .task import TaskSnapshot, TaskState
from .turn import InvalidTurnState


_INPUT_HANDLERS: Mapping[type, str] = {
    ApprovalResolutionInput: "_handle_approval_resolution",
    ClarificationReplyInput: "_handle_clarification_reply",
    InterruptTaskInput: "_handle_interrupt_task",
    CancelTaskInput: "_handle_cancel_task",
    SessionTextInput: "_handle_session_text",
    RuntimeTextInput: "_handle_runtime_text",
}


class InputMixin:
    async def dispatch_input_event(
        self,
        event: RuntimeInputEvent,
        *,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ) -> (
        RuntimeInputRoute | SessionInputDecision | TaskSnapshot
        | AgentTurnResult | AgentTurnSuspended | AgentClarificationSuspended
    ):
        """Route typed input before any semantic interpretation.

        Protocol replies are resolved directly against their pending request;
        only ``RuntimeTextInput`` may enter the ordinary-text route.

        Dispatch is a **table**, not an if/elif ladder (Hermes ``AGENTS.md:249``):
        a new input kind adds a row to ``_INPUT_HANDLERS`` plus one uniform
        handler, and cannot silently fall through to a neighbouring branch. The
        handlers all accept both callbacks even where only some publish progress,
        so the table has a single signature to bind.
        """
        for event_type in type(event).__mro__:
            name = _INPUT_HANDLERS.get(event_type)
            if name is not None:
                return await getattr(self, name)(
                    event, on_text_delta=on_text_delta, on_progress=on_progress,
                )
        raise TypeError(f"unsupported runtime input event: {type(event)!r}")

    async def _handle_approval_resolution(
        self, event: ApprovalResolutionInput, *,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ):
        return await self.resolve_agent_approval(
            event.request_id, event.decision, event.reason,
            on_text_delta=on_text_delta, on_progress=on_progress,
        )

    async def _handle_clarification_reply(
        self, event: ClarificationReplyInput, *,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ):
        return await self.resolve_agent_clarification(
            event.request_id, event.resume_token, event.answer,
            selected_choice=event.selected_choice,
            on_text_delta=on_text_delta, on_progress=on_progress,
        )

    async def _handle_interrupt_task(
        self, event: InterruptTaskInput, *,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ):
        del on_text_delta, on_progress  # this route publishes neither
        return await self.interrupt_agent_turn(event.task_id, event.reason)

    async def _handle_cancel_task(
        self, event: CancelTaskInput, *,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ):
        del on_text_delta, on_progress  # this route publishes neither
        task = await self.get_task(event.task_id)
        if task.state is TaskState.CANCELLED:
            return task
        # NEEDS_REVIEW is a settled handoff that a human may still abandon,
        # so it stays cancellable. Every other terminal state keeps the
        # existing "terminal task cannot be cancelled" semantics.
        if (
            task.state.is_terminal
            and task.state is not TaskState.NEEDS_REVIEW
        ):
            raise InvalidTurnState(
                f"terminal task {event.task_id} cannot be cancelled"
            )
        # Spec §9.4: settle any unstarted batch calls before the Task
        # settles, so no orphan execution record is left behind.
        if task.active_agent_checkpoint is not None:
            checkpoint = AgentTurnCheckpoint.from_data(
                task.active_agent_checkpoint
            )
            settled = await self._settle_open_batch_for_cancel(checkpoint)
            if settled is not checkpoint:
                await self._save_agent_checkpoint(
                    settled, "tool-batch-cancelled-by-user"
                )
        result = await self.transition_task(
            event.task_id, TaskState.CANCELLED, event.reason,
            intent="cancel",
        )
        note_cancel_requested(event.task_id)
        return result

    async def _handle_session_text(
        self, event: SessionTextInput, *,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ):
        del on_text_delta, on_progress  # this route publishes neither
        return await self.resolve_session_input(
            event.session_id, event.text,
            Path(event.workspace) if event.workspace else None,
        )

    async def _handle_runtime_text(
        self, event: RuntimeTextInput, *,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ):
        del on_text_delta, on_progress  # this route publishes neither
        return await self.route_runtime_input(
            event.task_id, event.text, event.input_id,
            explicit_intent=event.explicit_intent,
            fallback_intent=event.fallback_intent,
        )

    async def _judge_input_relation(
        self, text: str, task: TaskSnapshot,
    ) -> SessionInputRelationJudgement:
        """Ask the shared relation judge; a failure never changes the Task."""
        judge = self._dependencies.session_input_relation
        if judge is None:
            return SessionInputRelationJudgement(
                SessionInputRelation.SUPPLEMENT, 0.0, "no_relation_judge",
            )
        context = {
            "session_id": task.session_id,
            "active_task": {
                "task_id": task.task_id, "state": task.state.value,
                "goal": task.goal,
            },
        }
        try:
            return await judge.judge_input_relation(text, context)
        except Exception:
            return SessionInputRelationJudgement(
                SessionInputRelation.UNKNOWN, 0.0, "relation_judge_failed",
            )

    async def route_runtime_input(
        self, task_id: str, text: str, input_id: str, *,
        explicit_intent: RuntimeInputIntent | None = None,
        fallback_intent: RuntimeInputIntent | None = None,
    ) -> RuntimeInputRoute:
        """Validate a protocol route and atomically enqueue live Task input.

        The routing Event contains only a text hash.  Approval remains a
        separate protocol: ordinary language is never classified here and can
        never approve an action.
        """
        normalized = text.strip()
        normalized_id = input_id.strip()
        if not normalized or not normalized_id:
            raise ValueError("runtime input text and identity must not be empty")
        for attempt in range(5):
            stored = await self._require_stored_task(task_id)
            task = TaskSnapshot.from_data(stored.data)
            if task.state not in {
                TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW,
                TaskState.AWAITING_APPROVAL, TaskState.AWAITING_USER,
                TaskState.INTERRUPTED, TaskState.CONFLICT,
            }:
                raise InvalidTurnState(
                    f"runtime input requires an active Task, got {task.state.value}"
                )
            events = await self._dependencies.store.read_events(task_id)
            request_hash = canonical_hash({
                "text": normalized,
                "explicit_intent": (
                    explicit_intent.value if explicit_intent else None
                ),
                "fallback_intent": (
                    fallback_intent.value if fallback_intent else None
                ),
            })
            existing = next((
                event for event in events
                if event.event_type == "runtime_input.routed"
                and event.payload.get("input_id") == normalized_id
            ), None)
            if existing is not None:
                if existing.payload.get("request_hash") != request_hash:
                    raise ValueError("input_id was reused with different input")
                return RuntimeInputRoute(
                    RuntimeInputIntent(str(existing.payload["intent"])),
                    float(existing.payload["confidence"]),
                    str(existing.payload["reason_code"]),
                    bool(existing.payload["requires_confirmation"]),
                    bool(existing.payload.get("applied", False)),
                    str(existing.payload.get("router_version", "unknown")),
                )
            context = RuntimeInputContext(
                task.state.value, task.goal,
                task.pending_clarification is not None,
                task.pending_approval is not None,
                (task.pending_approval.kind.value
                 if task.pending_approval is not None else ""),
                (task.pending_approval.action
                 if task.pending_approval is not None else ""),
                (task.pending_approval.target
                 if task.pending_approval is not None else ""),
                (task.pending_approval.risk.value
                 if task.pending_approval is not None else ""),
            )
            classified_intent: RuntimeInputIntent | None = None
            classified_confidence: float | None = None
            judge = self._dependencies.session_input_relation
            if (
                explicit_intent is None
                and not context.awaiting_approval
                and not context.awaiting_clarification
                and fallback_intent is None
                and judge is not None
            ):
                # One general judge says how the input relates to the Session and
                # Task; Runtime maps that relation onto a control intent. This is
                # the same judge the unified user-input entry uses, so Web, CLI
                # and SDK share one decision source.
                judgement = await self._judge_input_relation(normalized, task)
                relation_intent = {
                    SessionInputRelation.SUPPLEMENT: RuntimeInputIntent.STEER,
                    SessionInputRelation.REPLACE: RuntimeInputIntent.REPLACE,
                    SessionInputRelation.STATUS_QUERY: RuntimeInputIntent.STATUS_QUERY,
                }.get(judgement.relation)
                if relation_intent is not None and judgement.confidence >= 0.85:
                    classified_intent = relation_intent
                    classified_confidence = judgement.confidence
            route = RuntimeInputRouter().route(
                normalized, context, explicit_intent, fallback_intent,
                classified_intent, classified_confidence,
            )
            steering_kind = {
                RuntimeInputIntent.STEER: SteeringKind.STEER,
                RuntimeInputIntent.REPLACE: SteeringKind.REPLACE,
            }.get(route.intent)
            applied = steering_kind is not None and not route.requires_confirmation
            checkpoint: AgentTurnCheckpoint | None = None
            if applied and context.awaiting_approval:
                if task.active_agent_checkpoint is None:
                    route = RuntimeInputRoute(
                        route.intent, route.confidence,
                        "approval_has_no_resumable_agent_checkpoint", True,
                        router_version=route.router_version,
                    )
                    applied = False
                else:
                    checkpoint = AgentTurnCheckpoint.from_data(
                        task.active_agent_checkpoint
                    )
            route = route.with_applied(applied)
            projection = SteeringProjector.project(task_id, events)
            payload = route.to_event_data(normalized, normalized_id)
            payload["request_hash"] = request_hash
            committed_events = [RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
                "runtime_input.routed", payload,
            )]
            next_task = task
            if applied and checkpoint is not None:
                approval = task.pending_approval
                assert approval is not None
                evidence_projection = EvidenceQuestionProjector.project(
                    task_id, events
                )
                evidence_record = None
                if approval.call.evidence_question is not None:
                    current_evidence = evidence_projection.get(
                        approval.call.evidence_question.question_id
                    )
                    if (
                        current_evidence is not None
                        and approval.call.call_id in current_evidence.tool_call_ids
                    ):
                        evidence_projection, evidence_record = evidence_projection.dispose(
                            approval.call, ToolActionDisposition.REPLACE,
                            "pending approval superseded by runtime input",
                        )
                cancellation_messages = tuple(
                    Message(
                        f"msg-superseded-{call.call_id}-{uuid4().hex}",
                        MessageRole.TOOL,
                        (ToolResultBlock(ToolResult(
                            call.call_id, False, error_code="ACTION_SUPERSEDED",
                            message=(
                                "The pending action was cancelled before execution "
                                "because the user supplied a new Task direction."
                            ),
                            hint="Re-plan from the latest user input.",
                        )),),
                    )
                    for call in checkpoint.pending_tool_calls
                )
                checkpoint = replace(
                    checkpoint, revision=checkpoint.revision + 1,
                    messages=checkpoint.messages + cancellation_messages,
                    pending_tool_calls=(), pending_user_action={},
                )
                next_task = task.resolve_approval().with_agent_checkpoint(
                    checkpoint.to_data()
                )
                sequence = stored.last_event_sequence + len(committed_events) + 1
                committed_events.append(RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id, sequence,
                    "approval.resolved", {
                        "request_id": approval.request_id,
                        "payload_hash": approval.payload_hash,
                        "outcome_ref": approval.call.outcome_ref,
                        "decision": "superseded",
                        "reason": "user supplied a new Task direction",
                        "pending_user_action": {},
                    },
                ))
                if evidence_record is not None:
                    current_record = current_evidence
                    assert current_record is not None
                    sequence += 1
                    evidence_record = replace(
                        evidence_record, updated_event_sequence=sequence
                    )
                    committed_events.append(RuntimeEvent(
                        f"evt-{uuid4().hex}", task_id, sequence,
                        "evidence.question_state_changed", {
                            "turn_id": approval.turn_id,
                            "tool_call_id": approval.call.call_id,
                            "tool_name": approval.call.name,
                            "question_ref": evidence_record.question_ref,
                            "previous_status": current_record.status.value,
                            "next_status": evidence_record.status.value,
                            "observation_kind": (
                                evidence_record.observation_kind.value
                                if evidence_record.observation_kind else None
                            ),
                            "blocking_reason": evidence_record.blocking_reason,
                            "evidence_count": len(
                                evidence_record.evidence_references
                            ),
                            "record": evidence_record.to_data(),
                        },
                    ))
                sequence += 1
                committed_events.append(RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id, sequence,
                    "tool.action_disposed", {
                        "turn_id": approval.turn_id,
                        "tool_call_id": approval.call.call_id,
                        "tool_name": approval.call.name,
                        "disposition": ToolActionDisposition.REPLACE.value,
                        "reason": "pending approval superseded by runtime input",
                    },
                ))
            if applied and steering_kind is not None:
                sequence = stored.last_event_sequence + len(committed_events) + 1
                committed_events.append(RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id,
                    sequence, "steering.queued", {
                        "steering_id": normalized_id,
                        "inbound_sequence": projection.latest_inbound_sequence + 1,
                        "kind": steering_kind.value, "text": normalized,
                        "text_hash": canonical_hash(normalized),
                        "request_hash": canonical_hash({
                            "kind": steering_kind.value, "text": normalized,
                        }),
                    },
                ))
            if applied and checkpoint is not None:
                sequence = stored.last_event_sequence + len(committed_events) + 1
                committed_events.append(RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id, sequence,
                    "checkpoint.saved", {
                        "turn_id": checkpoint.turn_id,
                        "revision": checkpoint.revision,
                        "checkpoint_hash": checkpoint.checkpoint_hash,
                        "reason": "approval-superseded-by-runtime-input",
                        "model_calls": checkpoint.model_calls,
                        "tool_calls": checkpoint.tool_calls,
                        "pending_tool_calls": 0,
                        "pending_user_action": {},
                    },
                ))
                sequence += 1
                committed_events.append(RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id, sequence,
                    "task.state_changed", {
                        "previous_state": task.state.value,
                        "next_state": next_task.state.value,
                        "reason": "pending approval superseded by runtime input",
                    },
                ))
            try:
                await self._dependencies.store.commit(RuntimeUnitOfWork(
                    task_id, stored.version, next_task.to_data(),
                    tuple(committed_events),
                ))
                return route
            except ValueError as error:
                if "version conflict" not in str(error) or attempt == 4:
                    raise
        raise RuntimeError("unreachable runtime input retry state")

    async def _classify_semantic_action(
        self, task_id: str, turn_id: str, call: ToolCall,
    ) -> SemanticAction | None:
        classifier = self._dependencies.semantic_action_classifier
        if classifier is None:
            return None
        try:
            action = await classifier.classify(call)
        except Exception as error:
            await self._append_events(task_id, ((
                "semantic.action_classification_failed", {
                    "turn_id": turn_id,
                    "tool_call_id": call.call_id,
                    "tool_name": call.name,
                    "error_type": type(error).__name__,
                },
            ),))
            return None
        await self._append_events(task_id, ((
            "semantic.action_classified", {
                "turn_id": turn_id,
                "tool_call_id": call.call_id,
                "tool_name": call.name,
                **action.event_data(),
            },
        ),))
        return action
