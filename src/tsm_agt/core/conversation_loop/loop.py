"""The model-led conversation loop.

This is the Runtime's main loop for ordinary chat Tasks.  Its control model is
deliberately small:

* the model decides whether a turn needs a tool, which advertised tool to call,
  how to react to a failure, and when it is finished;
* the Runtime decides whether a call is legal, permitted, approved, how long it
  may run, how its result is paired and persisted, and when a hard budget is
  spent.

The loop has no completion contract.  A plain assistant message with no tool
calls normally ends the turn. A rejected, unstarted workspace edit gets one
bounded recovery opportunity before an immediate text-only exit is accepted.
A tool result -- success, failure, truncation, or timeout -- is written back as a ``tool`` role message and the next model sample sees it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import uuid4

from tsm_agt.ports import (
    CompletionReviewAction,
    CompletionReviewFailurePolicy,
    CompletionReviewVerdict,
    ConclusionProtocolMode,
    Message,
    MessageBlock,
    MessageRole,
    ModelFailureCategory,
    ModelRecoveryAction,
    ModelRecoveryExhausted,
    ModelRequest,
    ModelRetrySafety,
    ModelTransportProgress,
    OutcomeBindingMode,
    RecoverableToolProtocolError,
    RuntimeEvent,
    RuntimeUnitOfWork,
    TextBlock,
    ToolCall,
    ToolRecoveryKind,
    ToolResult,
    ToolResultBlock,
    ToolSpec,
    FinalizationAction,
    TurnFinalizationFailed,
)
from ..agent_loop import (
    AgentCheckpointConflict,
    AgentClarificationSuspended,
    AgentLoopLimitExceeded,
    AgentProgress,
    AgentProgressKind,
    AgentTurnCheckpoint,
    AgentTurnIncomplete,
    AgentTurnResult,
    AgentTurnSuspended,
    ProviderCapabilityMismatch,
    TurnState,
    record_model_transport_progress,
)
from ..approval import ApprovalNotPending
from ..clarification import ClarificationNotPending
from ..context import ContextWindowExceeded
from ..context_messages import GENERATED_CONTEXT_PREFIXES
from ..task import TaskKind, TaskSnapshot, TaskState
from ..turn import InvalidModelResponse, InvalidTurnState, ModelInvocationFailed
from .finalizer import ITERATION_BUDGET_EXHAUSTED, ConversationFinalizer
from .finalization import ConversationFinalizationPipeline
from .prompt import assemble_stable_prompt, latest_tool_result_instruction
from .state import build_turn_state
from .execution_context import project_execution_context


#: Message-id prefixes the Runtime re-projects on every model call. They are
#: dropped and rebuilt so a stale context projection is never sent twice.
_GENERATED_CONTEXT_PREFIXES = GENERATED_CONTEXT_PREFIXES

#: Runtime-owned outcome bookkeeping is never exposed as a model tool.
_NON_MODEL_TOOLS = frozenset({"core.task_outcome_complete"})

def model_visible_tools(visible_tools: tuple[ToolSpec, ...]) -> tuple[ToolSpec, ...]:
    """The tool schemas the conversation loop actually shows the model."""

    return tuple(
        tool for tool in visible_tools if tool.name not in _NON_MODEL_TOOLS
    )


def select_model_tools(
    visible_tools: tuple[ToolSpec, ...], *, user_text: str, model_call: int,
    task_kind: TaskKind | None = None,
) -> tuple[ToolSpec, ...]:
    """Return the tools applicable to this execution kind on every sample.

    The harness must not infer an intent from request keywords and then hide
    alternatives.  Tool selection is a model decision informed by stable tool
    contracts; provider capability and policy validation remain runtime-owned.
    ``user_text`` and ``model_call`` stay in this compatibility signature so
    callers cannot accidentally reintroduce a first-turn classifier.
    Fixed work-item contracts are not capabilities of a conversation container.
    """
    del user_text, model_call
    tools = model_visible_tools(visible_tools)
    if task_kind is TaskKind.CONVERSATION:
        # These describe a fixed work-item contract, not a persistent chat.
        # Keep the runtime registry/checkpoint toolset intact for recovery.
        tools = tuple(tool for tool in tools if tool.name not in {
            "core.task_spec_read", "core.task_spec_update",
        })
    return tools


def model_tool_exposure_profile(
    visible_tools: tuple[ToolSpec, ...], *, user_text: str, model_call: int,
) -> str:
    del visible_tools, user_text, model_call
    return "all-visible"


class ConversationAgentLoop:
    """Drive one Task with the model-led conversation protocol."""

    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel
        self._finalizer = ConversationFinalizer(kernel)
        self._finalization = ConversationFinalizationPipeline(kernel)

    async def run(
        self,
        task_id: str,
        user_text: str,
        user_blocks: tuple[MessageBlock, ...] | None,
        *,
        max_model_calls: int,
        max_tool_calls: int,
        max_output_tokens: int,
        tool_timeout_seconds: float,
        on_text_delta: Callable[[str], None] | None,
        on_progress: Callable[[AgentProgress], None] | None,
    ) -> AgentTurnResult | AgentTurnIncomplete | AgentTurnSuspended | AgentClarificationSuspended:
        kernel = self._kernel
        task = await kernel.get_task(task_id)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        visible_tools = await kernel.list_tools()

        # The Skill provider discovers user-level files and exposes a shared
        # loader. Catalog availability is independent of Session enablement.
        enabled_skill_ids = tuple(
            skill.id for skill in kernel._dependencies.skill_registry.list_all()
        )
        initial_model_tools = select_model_tools(
            visible_tools, user_text=user_text, model_call=1,
            task_kind=task.task_kind,
        )
        initial_exposure_profile = model_tool_exposure_profile(
            visible_tools, user_text=user_text, model_call=1,
        )
        capabilities = kernel._dependencies.model.capabilities
        if visible_tools and not capabilities.tools:
            raise ProviderCapabilityMismatch(
                "selected model provider does not support tools, but the Agent turn "
                f"exposes {len(visible_tools)} tool(s)"
            )
        turn_id = f"turn-{uuid4().hex}"
        user_message = Message(
            message_id=f"msg-{uuid4().hex}",
            role=MessageRole.USER,
            content=user_blocks or (TextBlock(user_text),),
        )
        await kernel._append_events(task_id, (("turn.started", {
            "turn_id": turn_id,
            "orchestration_mode": "CONVERSATION_LOOP",
            "current_input_ref": f"message:{user_message.message_id}",
            "input_role": user_message.role.value,
            "input_character_count": len(user_text),
            "max_model_calls": max_model_calls,
            "max_tool_calls": max_tool_calls,
            "max_output_tokens": max_output_tokens,
            "visible_tools": [
                tool.name for tool in initial_model_tools
            ],
            "tool_exposure_profile": initial_exposure_profile,
            "provider_capabilities": capabilities.to_data(),
        }),))
        await kernel._append_events(task_id, (("conversation.turn_started", {
            "turn_id": turn_id,
            "orchestration_mode": "CONVERSATION_LOOP",
            "max_model_calls": max_model_calls,
            "max_tool_calls": max_tool_calls,
            "visible_tools": [
                tool.name for tool in initial_model_tools
            ],
            "tool_exposure_profile": initial_exposure_profile,
        }),))
        # The input becomes history now, not when the Turn ends: a Turn that is
        # interrupted or killed never writes a result event, and the question
        # would vanish from the conversation.
        await kernel._record_session_turn_input(task_id, turn_id, user_message)
        checkpoint = AgentTurnCheckpoint(
            task_id=task_id,
            turn_id=turn_id,
            revision=1,
            messages=(user_message,),
            pending_tool_calls=(),
            seen_call_ids=(),
            model_calls=0,
            tool_calls=0,
            input_tokens=0,
            output_tokens=0,
            max_model_calls=max_model_calls,
            max_tool_calls=max_tool_calls,
            max_output_tokens=max_output_tokens,
            tool_timeout_seconds=tool_timeout_seconds,
            enabled_skills=enabled_skill_ids,
        )
        checkpoint = await kernel._bind_agent_checkpoint(
            task, checkpoint, visible_tools
        )
        await kernel._save_agent_checkpoint(checkpoint, "conversation-loop-started")
        return await self._drive(
            checkpoint, visible_tools, on_text_delta=on_text_delta,
            on_progress=on_progress,
        )

    async def resume_checkpoint(
        self,
        task_id: str,
        *,
        on_text_delta: Callable[[str], None] | None,
        on_progress: Callable[[AgentProgress], None] | None,
    ) -> AgentTurnResult | AgentTurnIncomplete | AgentTurnSuspended | AgentClarificationSuspended:
        """Resume a durable conversation checkpoint without new authority."""
        kernel = self._kernel
        await kernel._close_cancellation_scope(task_id)
        await kernel.recover_workspace_transactions(task_id)
        await kernel.reconcile_background_processes(task_id)
        stored = await kernel._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        if task.state is TaskState.AWAITING_APPROVAL:
            raise ApprovalNotPending(
                "task is waiting for explicit approve/reject; resume cannot grant approval"
            )
        if task.state is TaskState.AWAITING_USER:
            raise ClarificationNotPending(
                "task is waiting for an answer; resume requires request_id and token"
            )
        if task.state.value not in {"EXECUTING", "INTERRUPTED", "CONFLICT"}:
            raise InvalidTurnState(
                f"task {task_id} cannot resume from {task.state.value}"
            )
        if task.active_agent_checkpoint is None:
            raise LookupError(f"task has no active Agent checkpoint: {task_id}")
        checkpoint = AgentTurnCheckpoint.from_data(task.active_agent_checkpoint)
        visible_tools = await kernel.list_tools()
        decision = await kernel._evaluate_checkpoint_compatibility(
            task, checkpoint, visible_tools
        )
        action = decision.action.value
        if task.state is TaskState.INTERRUPTED and action == "EXACT_RESUME":
            checkpoint = await kernel._reconcile_agent_checkpoint(
                task, checkpoint, visible_tools, decision
            )
            task = await kernel.get_task(task_id)
        elif action in {"RECONCILE_REQUIRED", "REBASE_REQUIRED"}:
            reconcile = (
                kernel._reconcile_agent_checkpoint
                if action == "RECONCILE_REQUIRED"
                else kernel._rebase_agent_checkpoint
            )
            checkpoint = await reconcile(task, checkpoint, visible_tools, decision)
            task = await kernel.get_task(task_id)
        elif action != "EXACT_RESUME":
            await kernel._mark_checkpoint_conflict(
                task.task_id, checkpoint, decision.conflict_reasons
            )
            details = (
                " (" + ", ".join(decision.conflict_reasons) + ")"
                if decision.conflict_reasons else ""
            )
            raise AgentCheckpointConflict(
                "Agent checkpoint cannot resume: " + decision.reason_code + details
            )
        if task.state.value in {"INTERRUPTED", "CONFLICT"}:
            stored = await kernel._require_stored_task(task_id)
            current = TaskSnapshot.from_data(stored.data)
            resuming = current.transition(TaskState.RESUMING)
            executing = resuming.transition(TaskState.EXECUTING)
            events = (
                RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
                    "task.state_changed", {
                        "previous_state": current.state.value,
                        "next_state": resuming.state.value,
                        "reason": "resume checkpoint validation passed",
                    },
                ),
                RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 2,
                    "task.state_changed", {
                        "previous_state": resuming.state.value,
                        "next_state": executing.state.value,
                        "reason": "resume Agent turn",
                    },
                ),
            )
            await kernel._dependencies.store.commit(RuntimeUnitOfWork(
                task_id, stored.version, executing.to_data(), events
            ))
        await kernel._append_events(task_id, (("turn.resumed", {
            "turn_id": checkpoint.turn_id,
            "previous_revision": checkpoint.revision,
            "revision": checkpoint.revision + 1,
            "checkpoint_hash": checkpoint.checkpoint_hash,
            "reason": "process restart or explicit resume",
            "orchestration_mode": "CONVERSATION_LOOP",
        }),))
        resumed = replace(checkpoint, revision=checkpoint.revision + 1)
        await kernel._save_agent_checkpoint(resumed, "conversation-loop-resumed")
        return await self._drive(
            resumed, visible_tools, recover_interrupted=True,
            on_text_delta=on_text_delta, on_progress=on_progress,
        )

    async def continue_after_approval(
        self,
        checkpoint: AgentTurnCheckpoint,
        visible_tools: tuple[ToolSpec, ...],
        initial_tool_message: Message,
        *,
        on_text_delta: Callable[[str], None] | None,
        on_progress: Callable[[AgentProgress], None] | None,
    ) -> AgentTurnResult | AgentTurnIncomplete | AgentTurnSuspended | AgentClarificationSuspended:
        """Continue the shared approval resolution path in this loop."""
        return await self._drive(
            checkpoint, visible_tools, initial_tool_message=initial_tool_message,
            on_text_delta=on_text_delta, on_progress=on_progress,
        )

    async def _drive(
        self,
        checkpoint: AgentTurnCheckpoint,
        visible_tools: tuple[ToolSpec, ...],
        *,
        initial_tool_message: Message | None = None,
        recover_interrupted: bool = False,
        on_text_delta: Callable[[str], None] | None,
        on_progress: Callable[[AgentProgress], None] | None,
    ) -> AgentTurnResult | AgentTurnIncomplete | AgentTurnSuspended | AgentClarificationSuspended:
        kernel = self._kernel
        state = build_turn_state(
            checkpoint=checkpoint,
            visible_tools=visible_tools,
            kernel=kernel,
            initial_tool_message=initial_tool_message,
            recover_interrupted=recover_interrupted,
            on_text_delta=on_text_delta,
            on_progress=on_progress,
        )
        while True:
            if state.pending_tool_calls:
                message_count_before = len(state.messages)
                outcome = await kernel._run_tool_batch(state)
                if outcome is not None:
                    # Approval, clarification, or a completed-unit continuation
                    # hands the turn back to the Runtime mid-loop. Record that
                    # boundary before returning the unchanged suspension.
                    await self._finalizer.record_interrupted(
                        state, f"suspended:{type(outcome).__name__}"
                    )
                    return outcome
                await self._record_new_tool_results(state, message_count_before)
            # ``run_tool_batch`` drains an assistant batch atomically. Only once
            # it has closed may a queued explicit STEER/REPLACE enter the model
            # transcript; doing it inside the batch would let it split sibling
            # tool calls. This is also the normal boundary before the next
            # sample, so a steer queued while a tool was running is now
            # guaranteed to be observable by that next sample.
            await self._apply_pending_steering_at_boundary(
                state, "before-model"
            )
            if state.model_call_count >= state.checkpoint.max_model_calls:
                return await self._finalizer.persist_partial_boundary(
                    state, ITERATION_BUDGET_EXHAUSTED
                )
            await self._prepare_model_call(state)
            buffered_text_deltas: list[str] = []
            kernel._notify_agent_progress(state.on_progress, AgentProgress(
                AgentProgressKind.MODEL_STARTED,
                model_call=state.model_call_number,
                max_model_calls=state.checkpoint.max_model_calls,
                tool_call=state.tool_call_count,
                max_tool_calls=state.checkpoint.max_tool_calls,
                goal=(await kernel.get_task(state.task_id)).goal,
            ))
            response = None
            parsed_calls: tuple[ToolCall, ...] = ()
            try:
                response = await kernel._complete_agent_model_request(
                    state.request, buffered_text_deltas.append,
                    lambda text: kernel._notify_agent_progress(
                        state.on_progress,
                        AgentProgress(AgentProgressKind.REASONING_DELTA,
                                      model_call=state.model_call_number,
                                      reasoning_text=text),
                    ),
                )
                kernel._notify_agent_progress(state.on_progress, AgentProgress(
                    AgentProgressKind.MODEL_COMPLETED,
                    model_call=state.model_call_number,
                    max_model_calls=state.checkpoint.max_model_calls,
                    tool_call=state.tool_call_count,
                    max_tool_calls=state.checkpoint.max_tool_calls,
                    elapsed_seconds=time.monotonic() - state.model_started_at,
                    goal=(await kernel.get_task(state.task_id)).goal,
                ))
                parsed_calls = kernel._validate_agent_response(
                    response.message,
                    response.finish_reason,
                )
                duplicate = [
                    call.call_id for call in parsed_calls
                    if call.call_id in state.seen_call_ids
                ]
                if duplicate:
                    raise ModelInvocationFailed(
                        state.turn_id,
                        "tool call_id must be unique within a turn: "
                        + ", ".join(duplicate),
                    )
                parsed_calls = tuple(
                    replace(
                        call,
                        outcome_ref=None,
                        outcome_binding_mode=OutcomeBindingMode.FULFILLMENT,
                    )
                    for call in parsed_calls
                )
                kernel._validate_tool_batch_proposal(
                    parsed_calls, state.visible_tool_by_name, state.seen_call_ids
                )
            except RecoverableToolProtocolError as error:
                await self._record_recoverable_protocol_error(
                    state, response, parsed_calls, error
                )
                continue
            except Exception as error:
                # A conversation turn must never be the one path that leaves the
                # Task EXECUTING with no recorded outcome: without this the
                # exception escaped as an unretrieved background task and the
                # Web waited forever on a turn that had already stopped. The
                # classification mirrors the single-loop turn phases.
                await self._record_model_failure(state, error)
                raise
            if state.tool_call_count + len(parsed_calls) > checkpoint.max_tool_calls:
                raise AgentLoopLimitExceeded(
                    state.turn_id, "max_tool_calls", checkpoint.max_tool_calls,
                    model_calls=state.model_call_count,
                    tool_calls=state.tool_call_count,
                    max_model_calls=checkpoint.max_model_calls,
                    max_tool_calls=checkpoint.max_tool_calls,
                    last_tool_error=state.last_tool_error,
                )
            state.seen_call_ids.update(call.call_id for call in parsed_calls)
            state.model_call_count += 1
            state.input_tokens += response.usage.input_tokens
            state.output_tokens += response.usage.output_tokens

            # The model response has not been exposed or made conversational
            # history yet. If explicit steering arrived while this sample was in
            # flight, discard that stale candidate and sample again with the
            # durable steering message. Previously this no-tool path went
            # straight to finalization, so the Runtime claimed "steered" while
            # the model never received the input.
            if not parsed_calls and await self._apply_pending_steering_at_boundary(
                state, "before-finalization"
            ):
                await self._record_superseded_model_response(state)
                continue

            state.messages.append(response.message)
            accepted_batch = (
                await kernel._new_tool_batch(
                    state.task_id, response.message, parsed_calls
                )
                if parsed_calls else None
            )
            state.checkpoint = replace(
                state.checkpoint,
                messages=tuple(state.messages),
                pending_tool_calls=parsed_calls,
                tool_batch=accepted_batch,
                seen_call_ids=tuple(sorted(state.seen_call_ids)),
                model_calls=state.model_call_count,
                tool_calls=state.tool_call_count,
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
            )
            await self._finalizer.record_model_response(
                state, response, len(parsed_calls)
            )
            if parsed_calls:
                commentary = "\n".join(
                    block.text.strip() for block in response.message.content
                    if isinstance(block, TextBlock) and block.text.strip()
                )
                # This is a display boundary, not an intent classifier.  A
                # commentary is a model-authored text block paired with a tool
                # proposal. Runtime emits no prose when it is absent, never
                # summarizes tool outcomes, and does not infer stage boundaries.
                if commentary:
                    await kernel._append_events(state.task_id, ((
                        "conversation.commentary", {
                            "turn_id": state.turn_id,
                            "model_call": state.model_call_count,
                            "text": commentary,
                            "source": "model",
                        },
                    ),))
                    kernel._notify_agent_progress(state.on_progress, AgentProgress(
                        AgentProgressKind.COMMENTARY,
                        model_call=state.model_call_count,
                        activity=commentary,
                        phase="model",
                    ))
            if not parsed_calls:
                candidate = await self._finalization.build_candidate(state, response)
                runtime_decision = await self._finalization.runtime_gate(
                    state, response
                )
                if runtime_decision.action is FinalizationAction.CONTINUE:
                    await self._finalization.append_runtime_continuation(
                        state, runtime_decision
                    )
                    continue
                if runtime_decision.action is FinalizationAction.INCOMPLETE:
                    return await self._finalizer.persist_partial_boundary(
                        state, runtime_decision.reason_code
                    )
                if runtime_decision.action is FinalizationAction.FAILED:
                    raise TurnFinalizationFailed(runtime_decision.reason_code)

                stop_decision = await self._finalization.run_stop_policies(candidate)
                if (
                    stop_decision is not None
                    and stop_decision.action is CompletionReviewAction.CONTINUE
                ):
                    if self._finalization.can_continue(state):
                        await self._finalization.append_review_continuation(
                            state, stop_decision, source="stop_policy"
                        )
                        continue
                    return await self._finalizer.persist_partial_boundary(
                        state, "stop_policy_continuation_exhausted"
                    )

                candidate_hash = self._finalization.candidate_hash(candidate)
                repeated_candidate = (
                    bool(state.last_candidate_hash)
                    and state.last_candidate_hash == candidate_hash
                )
                try:
                    review = await self._finalization.review(state, candidate)
                except Exception as error:
                    await kernel._append_events(state.task_id, ((
                        "turn.completion_review_failed", {
                            "turn_id": state.turn_id,
                            "orchestration_mode": "CONVERSATION_LOOP",
                            "reviewer_call_count": state.reviewer_call_count,
                            "error_type": type(error).__name__,
                            "reason_code": getattr(error, "reason_code", "reviewer_failed"),
                            "failure_policy": (
                                kernel._dependencies
                                .completion_review_failure_policy.value
                            ),
                        },
                    ),))
                    if (
                        kernel._dependencies.completion_review_failure_policy
                        is CompletionReviewFailurePolicy.INCOMPLETE
                    ):
                        return await self._finalizer.persist_partial_boundary(
                            state, "completion_reviewer_unavailable"
                        )
                    if (
                        kernel._dependencies.completion_review_failure_policy
                        is CompletionReviewFailurePolicy.VISIBLE_UNVERIFIED
                    ):
                        await kernel._append_events(state.task_id, ((
                            "turn.completion_review_bypassed", {
                                "turn_id": state.turn_id,
                                "orchestration_mode": "CONVERSATION_LOOP",
                                "reason_code": "reviewer_unavailable",
                                "answer_visibility": "unverified",
                            },
                        ),))
                        review = CompletionReviewVerdict(
                            CompletionReviewAction.ACCEPT,
                            "reviewer_failure_visible_unverified",
                            "completion reviewer was unavailable; answer is visible but unverified",
                        )
                    else:
                        review = CompletionReviewVerdict(
                            CompletionReviewAction.ACCEPT,
                            "reviewer_failure_accepted",
                            "completion reviewer failure was accepted by configuration",
                        )
                if review.action is CompletionReviewAction.CONTINUE:
                    exhausted_reason = ""
                    if repeated_candidate:
                        exhausted_reason = "repeated_rejected_candidate"
                    elif not self._finalization.can_continue(state):
                        exhausted_reason = "completion_review_exhausted"
                    if exhausted_reason:
                        await kernel._append_events(state.task_id, ((
                            "turn.completion_review_exhausted", {
                                "turn_id": state.turn_id,
                                "orchestration_mode": "CONVERSATION_LOOP",
                                "reason_code": exhausted_reason,
                                "reviewer_call_count": state.reviewer_call_count,
                                "review_continuations": state.review_continuations,
                            },
                        ),))
                        return await self._finalizer.persist_partial_boundary(
                            state, exhausted_reason
                        )
                    state.last_candidate_hash = candidate_hash
                    await self._finalization.append_review_continuation(
                        state, review, source="completion_reviewer"
                    )
                    continue
                # Completion review may itself take time. Run the same durable
                # boundary immediately before committing the answer, and remove
                # the unshown candidate if a steer arrived in that interval.
                if state.messages and state.messages[-1] == response.message:
                    state.messages.pop()
                if await self._apply_pending_steering_at_boundary(
                    state, "before-finalization-commit"
                ):
                    await self._record_superseded_model_response(state)
                    continue
                state.messages.append(response.message)
                await self._finalization.persist_finalization_state(
                    state, "conversation-loop-text-completion"
                )
                await kernel._append_events(state.task_id, ((
                    "turn.finalization_accepted", {
                        "turn_id": state.turn_id,
                        "review_reason_code": review.reason_code,
                        "reviewer_call_count": state.reviewer_call_count,
                        "review_continuations": state.review_continuations,
                    },
                ),))
                # A response already shown to the user is conversational
                # history, even when completion review is disabled or failed.
                # Hiding it makes a natural follow-up ("yes, implement it")
                # lose its referent.  This flag controls only history replay;
                # it never upgrades prose into tool evidence or acceptance.
                task = await kernel.get_task(state.task_id)
                replay_eligible = task.task_kind is TaskKind.CONVERSATION
                result = await self._finalizer.persist_text_completion(
                    state, response,
                    assistant_replay_eligible=replay_eligible,
                )
                if state.on_text_delta is not None:
                    for text in buffered_text_deltas:
                        state.on_text_delta(text)
                return result
            await kernel._save_agent_checkpoint(
                state.checkpoint, "conversation-loop-model-response"
            )
            state.pending_tool_calls = list(parsed_calls)

    async def _apply_pending_steering_at_boundary(
        self, state: TurnState, safe_point: str,
    ) -> bool:
        """Project accepted steering into the next model-visible transcript.

        ``queue_steering`` is intentionally asynchronous to tool/model work.
        This helper is the single conversation-loop drain point for the
        corresponding durable queue. It carries all in-flight accounting into
        the checkpoint before the Kernel atomically applies the queued input.
        """

        previous_sequence = state.checkpoint.last_steering_inbound_sequence
        state.checkpoint, _ = await self._kernel._apply_pending_steering(
            replace(
                state.checkpoint,
                messages=tuple(state.messages),
                pending_tool_calls=tuple(state.pending_tool_calls),
                seen_call_ids=tuple(sorted(state.seen_call_ids)),
                model_calls=state.model_call_count,
                tool_calls=state.tool_call_count,
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
            ),
            safe_point,
        )
        state.messages = list(state.checkpoint.messages)
        state.pending_tool_calls = list(state.checkpoint.pending_tool_calls)
        return (
            state.checkpoint.last_steering_inbound_sequence
            > previous_sequence
        )

    async def _record_superseded_model_response(self, state: TurnState) -> None:
        """Leave an audit fact when an unshown sample lost to user steering."""

        await self._kernel._append_events(state.task_id, ((
            "turn.model_response_superseded", {
                "turn_id": state.turn_id,
                "model_call": state.model_call_count,
                "reason_code": "steering_applied_before_finalization",
            },
        ),))

    async def _record_new_tool_results(
        self, state: TurnState, message_count_before: int,
    ) -> None:
        for message in state.messages[message_count_before:]:
            if message.role is not MessageRole.TOOL:
                continue
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    await self._finalizer.record_tool_result(state, block.result)

    async def _record_model_failure(
        self, state: TurnState, error: Exception,
    ) -> None:
        """Record one stopped model exchange before it is re-raised.

        A recoverable stop preserves the checkpoint as ``INTERRUPTED`` so the
        conversation can take the next message; anything else is a real failure.
        """
        kernel = self._kernel
        recovery = (
            error if isinstance(error, ModelRecoveryExhausted) else None
        )
        failure_category = (
            recovery.failure.category if recovery is not None
            else ModelFailureCategory.UNKNOWN
        )
        retry_safety = (
            recovery.failure.retry_safety if recovery is not None
            else ModelRetrySafety.NEVER
        )
        recovery_action = (
            recovery.decision.action if recovery is not None
            else ModelRecoveryAction.FAIL_TERMINAL
        )
        if isinstance(error, InvalidModelResponse):
            await kernel._record_turn_failure(
                state.task_id, state.turn_id, error, state.prompt.receipt,
                failure_kind="model_response",
                failure_category=failure_category,
                retry_safety=retry_safety,
                recovery_action=recovery_action,
            )
            return
        await kernel._record_recoverable_turn_interruption(
            state.task_id, state.turn_id, error, state.prompt.receipt,
            failure_kind="model_response",
            failure_category=failure_category,
            retry_safety=retry_safety,
            recovery_action=recovery_action,
        )

    async def _record_recoverable_protocol_error(
        self,
        state: TurnState,
        response: Any,
        parsed_calls: tuple[ToolCall, ...],
        error: RecoverableToolProtocolError,
    ) -> None:
        """Write a rejected call back as a model-visible failure.

        A batch rejected for an unknown tool name or bad arguments never
        executed, but the assistant message that proposed it is still the
        correct protocol partner for a ``tool`` role failure.  Continuing with a
        tool-role result keeps the message roles honest and lets the next sample
        fix the call from the schema it was already shown.
        """
        kernel = self._kernel
        state.model_call_count += 1
        if response is not None:
            state.input_tokens += response.usage.input_tokens
            state.output_tokens += response.usage.output_tokens
        if response is not None and parsed_calls:
            state.messages.append(response.message)
            for call in parsed_calls:
                state.messages.append(Message(
                    message_id=f"msg-tool-{uuid4().hex}",
                    role=MessageRole.TOOL,
                    content=(ToolResultBlock(ToolResult(
                        call.call_id, False,
                        error_code="INVALID_ARGUMENT",
                        message=(
                            error.detail
                            or "the runtime rejected this tool call before execution"
                        ),
                        hint=(
                            "Correct this call using the advertised schema and continue "
                            "the original user request. No call was executed; do not "
                            "substitute instructions or claim a tool is unavailable."
                        ),
                        retryable=True,
                        recovery_kind=(
                            ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS
                        ),
                        recovery_action={
                            "tool": call.name,
                            "reason": (
                                "arguments_do_not_match_advertised_schema"
                            ),
                        },
                        meta={
                            "runtime_guard": "CONVERSATION_LOOP_TOOL_PROTOCOL",
                            "protocol_reason": error.reason_code,
                        },
                    )),),
                ))
        else:
            state.messages.append(Message(
                message_id=f"tool-protocol-correction-{uuid4().hex}",
                role=MessageRole.USER,
                content=(TextBlock(json.dumps({
                    "type": "tool_protocol_rejected",
                    "error_code": error.reason_code,
                    "detail": error.detail,
                    "instruction": (
                        "Emit a valid tool call using only the advertised tools and "
                        "their schemas; no call was executed."
                    ),
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),),
            ))
        state.checkpoint = replace(
            state.checkpoint,
            messages=tuple(state.messages),
            model_calls=state.model_call_count,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
        )
        await kernel._append_events(state.task_id, ((
            "tool.protocol_rejected",
            {
                "turn_id": state.turn_id,
                "orchestration_mode": "CONVERSATION_LOOP",
                "reason_code": error.reason_code,
                "detail": error.detail,
            },
        ),))
        await kernel._save_agent_checkpoint(
            state.checkpoint, "conversation-loop-tool-protocol-correction"
        )

    async def _prepare_model_call(self, state: TurnState) -> None:
        kernel = self._kernel
        state.messages = list(kernel._normalize_and_validate_model_boundary(
            tuple(state.messages)
        ))
        model_call = state.model_call_count + 1
        state.messages = [
            message for message in state.messages
            if not message.message_id.startswith(_GENERATED_CONTEXT_PREFIXES)
        ]
        state.messages.extend(await kernel._project_context_messages(state.task_id))
        task = await kernel.get_task(state.task_id)
        state.messages = list(project_execution_context(
            state.messages, task_id=state.task_id, turn_id=state.turn_id,
            tool_specs=state.visible_tool_by_name,
            executions=tuple(task.tool_executions.values()),
        ))
        state.checkpoint = replace(state.checkpoint, messages=tuple(state.messages))
        source_user = kernel._source_user_message(state.checkpoint.messages)
        model_tools = select_model_tools(
            state.visible_tools,
            user_text=source_user.text if source_user is not None else "",
            model_call=model_call,
            task_kind=task.task_kind,
        )
        runtime_instruction = latest_tool_result_instruction(
            kernel, state, model_tools,
        )
        try:
            prepared = kernel._dependencies.context_manager.prepare(
                conversation=tuple(state.messages),
                tools=model_tools,
                prompt_template=kernel._dependencies.prompt_template,
                context_window=kernel._dependencies.model.capabilities.context_window,
                max_output_tokens=state.checkpoint.max_output_tokens,
                runtime_instruction=runtime_instruction,
                read_reuse_task_id=state.task_id,
            )
        except ContextWindowExceeded as error:
            await kernel._record_turn_failure(state.task_id, state.turn_id, error)
            raise ModelInvocationFailed(state.turn_id, str(error)) from error
        if prepared.compaction is not None:
            state.messages = list(prepared.canonical_messages)
            await kernel._append_events(state.task_id, (("context.compacted", {
                "turn_id": state.turn_id,
                "model_call": model_call,
                **prepared.compaction.event_data(),
            }),))
            state.checkpoint = replace(state.checkpoint, messages=tuple(state.messages))
            await kernel._save_agent_checkpoint(
                state.checkpoint, "conversation-loop-context-compacted"
            )
        if prepared.read_reuse is not None and prepared.read_reuse.references:
            await kernel._append_events(state.task_id, (("context.read_results_reused", {
                "turn_id": state.turn_id, "model_call": model_call,
                **prepared.read_reuse.event_data(),
            }),))
        prompt = assemble_stable_prompt(
            kernel._dependencies.prompt_template,
            prepared.messages,
            model_tools,
            runtime_instruction,
        )
        async def record_transport(update: ModelTransportProgress) -> None:
            await record_model_transport_progress(
                kernel, state, update,
                goal=(await kernel.get_task(state.task_id)).goal,
            )

        state.request = ModelRequest(
            turn_id=state.turn_id,
            messages=prompt.messages,
            max_output_tokens=state.checkpoint.max_output_tokens,
            tools=model_tools,
            allow_tool_calls=True,
            on_transport_progress=record_transport,
            conclusion_protocol_mode=(
                ConclusionProtocolMode.REQUIRE_STRUCTURED
                if kernel._dependencies.model.capabilities.structured_conclusion
                else ConclusionProtocolMode.OBSERVE
            ),
            cancellation_scope=kernel._cancellation_scope(state.task_id),
            # Per-turn reasoning effort applies only to the main conversation
            # loop; routing, planning and review calls keep their bootstrap
            # defaults.
            reasoning_effort=getattr(state.checkpoint, "reasoning_effort", None),
        )
        state.prepared = prepared
        state.prompt = prompt
        state.model_call_number = model_call
        state.model_started_at = time.monotonic()
