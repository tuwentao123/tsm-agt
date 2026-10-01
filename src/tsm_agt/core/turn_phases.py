"""Per-iteration phases of an agent turn, extracted from ``core/kernel.py``.

Each phase reads and writes ``TurnState`` and calls back into the Kernel it is handed.
The original methods' closures were exactly ``(self, state)``, so ``self`` became the
``kernel`` parameter and nothing else changed.

``_temporal_context_instruction`` is defined in ``kernel.py``; it is late-imported
inside the phase that needs it so the dependency stays one-way and ``kernel.py``
remains free to import this sibling at module level (Hermes ``AGENTS.md:243-244``).
"""

from __future__ import annotations

from typing import Any

import asyncio
import json
import time
from .agent_loop import (
    AgentLoopLimitExceeded, AgentProgress, AgentProgressKind, AgentTurnResult, PhaseAction, PhaseVerdict, TurnState,
)
from .budget_renewal import (
    DiminishingModelCallBudgetPolicy,
)
from collections.abc import (
    Mapping,
)
from .context import (
    ContextWindowExceeded,
)
from dataclasses import (
    replace,
)
from .steering import (
    SteeringProjector,
)
from tsm_agt.ports import (
    CompletionGap, CompletionReadinessAction, CompletionReadinessDecision, CompletionReadinessMode, CompletionReadinessState, ConclusionProtocolMode, ExplorationBudgetState, MessageRole, ModelAttemptFailure, ModelFailureCategory, ModelRecoveryAction, ModelRecoveryExhausted, ModelRecoveryProbe, ModelRequest, ModelRetrySafety, ModelTransportProgress, ModelUsage, OutcomeBindingMode, RecoverableToolProtocolError, ToolEffect,
)
from .turn import (
    InvalidModelResponse, ModelInvocationFailed,
)
from typing import (
    Any,
)


async def _begin_iteration(
    kernel: Any, state: TurnState,
) -> PhaseVerdict:
    """Loop-head hygiene: cancellation, steering and the tool batch.

    ``OK`` means the iteration is cleared to prepare its model call; ``RETURN``
    carries a terminal outcome the caller must return unchanged.
    """
    if kernel.cancellation_signal(state.task_id).requested():
        raise asyncio.CancelledError(
            "task cancelled at a turn boundary"
        )
    state.checkpoint = kernel._synchronize_tool_batch(replace(
        state.checkpoint, messages=tuple(state.messages),
        pending_tool_calls=tuple(state.pending_tool_calls),
    ))
    state.checkpoint = await kernel._cancel_open_batch_for_queued_replace(
        state.checkpoint
    )
    state.messages = list(state.checkpoint.messages)
    state.pending_tool_calls = list(state.checkpoint.pending_tool_calls)
    state.checkpoint, replaced = await kernel._apply_pending_steering(
        replace(
            state.checkpoint, messages=tuple(state.messages),
            pending_tool_calls=tuple(state.pending_tool_calls),
            seen_call_ids=tuple(sorted(state.seen_call_ids)),
            model_calls=state.model_call_count, tool_calls=state.tool_call_count,
            input_tokens=state.input_tokens, output_tokens=state.output_tokens,
        ),
        "before-tool-batch" if state.pending_tool_calls else "before-model",
    )
    state.messages = list(state.checkpoint.messages)
    state.pending_tool_calls = list(state.checkpoint.pending_tool_calls)
    if replaced:
        state.pending_tool_calls.clear()
        await kernel.plan_task_spec(state.task_id)
        refreshed_spec = await kernel.get_task_spec(state.task_id)
        state.checkpoint = replace(
            state.checkpoint, task_spec_revision=refreshed_spec.revision,
            task_spec_hash=refreshed_spec.content_hash,
        )
    batch_outcome = await kernel._run_tool_batch(state)
    if batch_outcome is not None:
        return PhaseVerdict(PhaseAction.RETURN, batch_outcome)
    return PhaseVerdict(PhaseAction.OK)


async def _prepare_model_call(
    kernel: Any, state: TurnState,
) -> None:
    """Resolve completion readiness, context and the prompt for one model call."""
    from .kernel import (  # late import: kernel.py defines this symbol
        _temporal_context_instruction,
    )

    """Resolve completion readiness, context and the prompt for one model call.

    Extracted verbatim from ``_continue_agent_turn``. Everything it produces
    lands on ``state``; the closure held only ``self`` and ``state``.
    """
    state.checkpoint = kernel._synchronize_tool_batch(replace(
        state.checkpoint, messages=tuple(state.messages),
        pending_tool_calls=tuple(state.pending_tool_calls),
    ))
    state.messages = list(kernel._normalize_and_validate_model_boundary(
        state.checkpoint.messages
    ))
    state.checkpoint = replace(state.checkpoint, messages=tuple(state.messages))
    state.model_call_number = state.model_call_count + 1
    if state.model_call_number > state.checkpoint.max_model_calls:
        error = AgentLoopLimitExceeded(
            state.turn_id, "max_model_calls", state.checkpoint.max_model_calls,
            model_calls=state.model_call_count, tool_calls=state.tool_call_count,
            last_tool_error=state.last_tool_error,
            max_model_calls=state.checkpoint.max_model_calls,
            max_tool_calls=state.checkpoint.max_tool_calls,
        )
        await kernel._record_agent_loop_failure(state.task_id, state.turn_id, error)
        raise error
    # Keep a small, explicit finalization reserve instead of closing
    # every tool at an arbitrary percentage of the Task budget.  The
    # reserve covers one direct answer and, by default, one correction
    # if the model emits protocol markup instead of an answer.
    wrap_up_threshold = min(
        kernel._dependencies.finalization_model_calls,
        max(1, state.checkpoint.max_model_calls - 1),
    )
    state.remaining_model_calls = state.checkpoint.max_model_calls - state.model_call_count
    remaining_tool_calls = state.checkpoint.max_tool_calls - state.tool_call_count
    state.completion_state = CompletionReadinessState.from_data(
        state.checkpoint.completion_readiness_state
    )
    state.legacy_completion_gate = (
        kernel.completion_readiness_mode
        is CompletionReadinessMode.LEGACY_GATE
    )
    state.disclosure_only = state.legacy_completion_gate and (
        state.completion_state.last_action in {
            CompletionReadinessAction.REPORT_BLOCKED.value,
            CompletionReadinessAction.REPORT_INCOMPLETE_RECOVERABLE.value,
            # A resolved "stop with work open" verdict gets one wrap-up
            # round: no new substantive work, state the blocker
            # honestly. The round after that suspends resumably.
            CompletionReadinessAction.EXHAUSTED.value,
        }
    )
    state.wrap_up = state.legacy_completion_gate and (
        state.remaining_model_calls <= wrap_up_threshold
        or state.no_progress_stop
        or state.budget_wrap_up
    )
    runtime_instruction = None
    required_gaps = await kernel._completion_readiness_gaps(
        state.task_id, state.visible_tools
    )
    available_effects_for_turn = {
        ToolEffect.OBSERVE
        if tool.effect is ToolEffect.UNSPECIFIED and tool.is_read_only
        else tool.effect
        for tool in state.visible_tools
        if not tool.is_internal_state
    }
    recoverable_required_work = any(
        gap.required and gap.effective_required_effects
        and gap.effective_required_effects.issubset(
            available_effects_for_turn
        )
        for gap in required_gaps
    )
    state.recovery_batch = kernel._latest_recoverable_tool_batch(state.messages)
    recovery_result = state.recovery_batch[-1] if state.recovery_batch else None
    # An effect-level failure produces an explicit gap even though the
    # call itself returned ok=True, so it must open the same recovery
    # barrier as a retryable tool error.
    structured_recovery = recovery_result is not None or any(
        gap.kind == "UNRESOLVED_EFFECT_FAILURE"
        for gap in required_gaps
    )
    # A soft exploration stop must not consume capacity reserved for a
    # known recovery or a required executable outcome. Keep one final
    # model call, while allowing the preceding call to use tools.
    can_finish_required_work = (
        state.remaining_model_calls > 1
        and (structured_recovery or recoverable_required_work)
    )
    if can_finish_required_work:
        state.wrap_up = False
    focus_state = ExplorationBudgetState.from_data(
        state.checkpoint.exploration_budget_state
    )
    if state.disclosure_only:
        if (
            state.completion_state.last_action
            == CompletionReadinessAction.REPORT_INCOMPLETE_RECOVERABLE.value
        ):
            runtime_instruction = (
                "This Turn has no remaining capacity to finish work that is "
                "still recoverable. Do not call tools. Report it as incomplete "
                "and resumable, list the remaining required work and the last "
                "safe checkpoint, and do not call it blocked or successful."
            )
        else:
            runtime_instruction = (
                "Completion readiness found required work that cannot be "
                "safely completed in this Turn. Do not call tools. State the "
                "exact blocker, what evidence was completed, and what required "
                "fact remains unverified. Do not claim success."
            )
    elif state.no_progress_stop or state.budget_wrap_up:
        if state.budget_wrap_up:
            runtime_instruction = (
                "The evidence-aware exploration budget has reached its "
                "soft limit. Do not call tools. Answer now from collected "
                "evidence and clearly label anything still unverified."
            )
        kernel._notify_agent_progress(state.on_progress, AgentProgress(
            AgentProgressKind.WRAP_UP, model_call=state.model_call_number,
            max_model_calls=state.checkpoint.max_model_calls,
            tool_call=state.tool_call_count,
            max_tool_calls=state.checkpoint.max_tool_calls,
            goal=(await kernel.get_task(state.task_id)).goal,
            reason=(
                "plan_no_progress" if state.no_progress_stop
                else (
                    state.budget_wrap_up_decision.reason
                    if state.budget_wrap_up_decision is not None
                    else "exploration_budget"
                )
            ),
            budget_reason=(
                state.budget_wrap_up_decision.reason
                if state.budget_wrap_up_decision is not None else ""
            ),
            exploration_actions=(
                state.budget_wrap_up_decision.scored_actions
                if state.budget_wrap_up_decision is not None else 0
            ),
            max_exploration_actions=(
                state.budget_wrap_up_decision.max_scored_actions
                if state.budget_wrap_up_decision is not None else 0
            ),
            exploration_tool_calls=(
                state.budget_wrap_up_decision.used_tool_calls
                if state.budget_wrap_up_decision is not None else 0
            ),
            max_exploration_tool_calls=(
                state.budget_wrap_up_decision.max_total_tool_calls
                if state.budget_wrap_up_decision is not None else 0
            ),
            exploration_elapsed_seconds=(
                state.budget_wrap_up_decision.cumulative_tool_milliseconds / 1000
                if state.budget_wrap_up_decision is not None else 0
            ),
            max_exploration_elapsed_seconds=(
                state.budget_wrap_up_decision.max_cumulative_tool_milliseconds / 1000
                if state.budget_wrap_up_decision is not None else 0
            ),
            low_value_streak=(
                state.budget_wrap_up_decision.low_value_streak
                if state.budget_wrap_up_decision is not None else 0
            ),
            max_low_value_streak=(
                state.budget_wrap_up_decision.max_low_value_streak
                if state.budget_wrap_up_decision is not None else 0
            ),
            reserve_tool_calls=(
                state.budget_wrap_up_decision.reserve_tool_calls
                if state.budget_wrap_up_decision is not None else 0
            ),
        ))
    elif state.wrap_up:
        runtime_instruction = (
            "The execution budget is nearly exhausted. Stop exploring and do "
            "not call any tools. Answer the user's request now using only the "
            "evidence already collected. Clearly disclose any missing evidence "
            "or failed verification instead of attempting another check."
        )
        kernel._notify_agent_progress(state.on_progress, AgentProgress(
            AgentProgressKind.WRAP_UP, model_call=state.model_call_number,
            max_model_calls=state.checkpoint.max_model_calls,
            tool_call=state.tool_call_count,
            max_tool_calls=state.checkpoint.max_tool_calls,
            goal=(await kernel.get_task(state.task_id)).goal,
            reason="execution_budget_nearly_exhausted",
        ))
    elif structured_recovery and recovery_result is not None:
        action = dict(recovery_result.recovery_action)
        runtime_instruction = (
            "The previous tool failure has structured recovery kind "
            f"{recovery_result.effective_recovery_kind.value}. "
            f"Recovery requirements: {json.dumps(action, ensure_ascii=False)}. "
            "The required Task outcome remains open. Choose a legal recovery "
            "action or an alternative tool; do not merely restate the error. "
            "When same_call_safe is false, do not repeat the unchanged call."
        )
    elif recoverable_required_work and state.remaining_model_calls <= (
        kernel._dependencies.finalization_model_calls
        + kernel._dependencies.execution_reserve_model_calls
        + kernel._dependencies.recovery_reserve_model_calls
        + kernel._dependencies.verification_reserve_model_calls
    ):
        runtime_instruction = (
            "Execution capacity is reserved for required outcomes. Stop broad "
            "exploration. Use the available tool effect needed to implement, "
            "recover, or verify the remaining required work, while preserving "
            "one final model response."
        )
    elif state.last_tool_error is not None:
        runtime_instruction = (
            "The latest durable ToolResult is authoritative. Do not repeat "
            "or rely on prior assistant prose as evidence. Address the "
            f"current tool failure ({state.last_tool_error}) using its result "
            "details; inspect or retry with corrected facts when safe."
        )
    elif focus_state.focus_mode:
        runtime_instruction = (
            "The exploration soft threshold has been reached. This is "
            "not a request to stop and not a user-facing budget problem. "
            "Do not open new investigation branches or perform workspace-"
            "wide searches. If a fact required to answer the user's core "
            "question can be confirmed through a known file, explicit "
            "symbol, or tightly scoped read-only search, collect that "
            "direct evidence now. Otherwise answer with the exact external "
            "blocker. Never ask the user to increase or unlock an internal "
            "budget."
        )
    if not state.legacy_completion_gate:
        # Non-legacy modes may record factual diagnostics but must not
        # turn readiness, recovery, or budget signals into commands.
        runtime_instruction = None
    # Anchor the model to the host clock on every call. Without it the
    # model falls back to its training prior, calls genuinely current
    # search results (which carry a real publish date) "future" or
    # "simulated", and answers from stale recollection instead.
    runtime_instruction = "\n\n".join(filter(None, (
        _temporal_context_instruction(),
        runtime_instruction,
        await kernel._unfinished_plan_instruction(state.task_id),
        state.protocol_correction,
    )))
    state.live_spec = await kernel.get_task_spec(state.task_id)
    # Tool availability is stable within a turn. Outcome completion is
    # legacy-only and is never advertised for new runtime calls.
    model_visible_tools = tuple(
        tool for tool in state.visible_tools
        if tool.name != "core.task_outcome_complete"
    )
    # Dynamic context is persisted in checkpoints for crash recovery, but
    # must be rebuilt before every model call. Keeping an old
    # session-context message lets stale Task summaries outlive later
    # user input and dominate resumed turns.
    generated_prefixes = (
        "project-instructions-context-", "task-spec-context-",
        "project-onboarding-context-", "project-memory-context-",
        "session-context-", "document-references-context-", "working-memory-context-",
    )
    state.messages = [
        message for message in state.messages
        if not message.message_id.startswith(generated_prefixes)
    ]
    state.messages.extend(await kernel._project_context_messages(state.task_id))
    try:
        state.prepared = kernel._dependencies.context_manager.prepare(
            conversation=tuple(state.messages), tools=model_visible_tools,
            prompt_template=kernel._dependencies.prompt_template,
            context_window=(
                kernel._dependencies.model.capabilities.context_window
            ),
            max_output_tokens=state.checkpoint.max_output_tokens,
            runtime_instruction=runtime_instruction,
        )
    except ContextWindowExceeded as error:
        await kernel._record_turn_failure(state.task_id, state.turn_id, error)
        raise ModelInvocationFailed(state.turn_id, str(error)) from error
    if state.prepared.compaction is not None:
        state.messages = list(state.prepared.messages)
        await kernel._append_events(
            state.task_id, (("context.compacted", {
                "turn_id": state.turn_id,
                "model_call": state.model_call_number,
                **state.prepared.compaction.event_data(),
            }),),
        )
        compacted_checkpoint = replace(
            state.checkpoint, messages=tuple(state.messages),
            pending_tool_calls=tuple(state.pending_tool_calls),
            seen_call_ids=tuple(sorted(state.seen_call_ids)),
            model_calls=state.model_call_count, tool_calls=state.tool_call_count,
            input_tokens=state.input_tokens, output_tokens=state.output_tokens,
        )
        await kernel._save_agent_checkpoint(
            compacted_checkpoint, "context-compacted"
        )
    state.prompt = kernel._dependencies.prompt_template.assemble(
        state.prepared.messages, model_visible_tools, runtime_instruction
    )
    async def record_transport(update: ModelTransportProgress) -> None:
        # Persist every physical-attempt transition before the provider
        # continues. A buffered batch made a five-minute timeout look
        # like one instantaneous event after recovery completed.
        await kernel._record_model_attempt_events(
            state.task_id, state.turn_id, state.model_call_number, [update]
        )
        retry_actions = {
            ModelRecoveryAction.RETRY_SAME_REQUEST.value,
            ModelRecoveryAction.RESAMPLE.value,
            ModelRecoveryAction.RESAMPLE_WITH_CORRECTION.value,
            ModelRecoveryAction.FALLBACK_TRANSPORT.value,
        }
        is_retry = (
            update.kind == "recovery_decided"
            and update.recovery_action in retry_actions
        )
        reason = update.reason
        if (
            update.recovery_action
            == ModelRecoveryAction.FALLBACK_TRANSPORT.value
        ):
            reason = "stream_fallback:" + reason
        kernel._notify_agent_progress(state.on_progress, AgentProgress(
            AgentProgressKind.MODEL_RETRY if is_retry
            else AgentProgressKind.MODEL_TRANSPORT,
            model_call=state.model_call_number,
            max_model_calls=state.checkpoint.max_model_calls,
            tool_call=state.tool_call_count,
            max_tool_calls=state.checkpoint.max_tool_calls,
            goal=state.live_goal,
            reason=reason,
            transport_attempt=update.attempt,
            max_transport_attempts=update.max_attempts,
            retry_delay_seconds=update.delay_seconds,
            transport_mode=update.transport_mode,
            transport_event=update.kind,
            diagnostic_code=update.diagnostic_code,
            diagnostic_detail=update.diagnostic_detail,
            recovery_action=update.recovery_action,
        ))

    state.request = ModelRequest(
        turn_id=state.turn_id,
        messages=state.prompt.messages,
        max_output_tokens=state.checkpoint.max_output_tokens,
        tools=model_visible_tools,
        allow_tool_calls=not (state.wrap_up or state.disclosure_only),
        require_evidence_questions=(
            kernel._dependencies.require_evidence_questions
        ),
        on_transport_progress=record_transport,
        conclusion_protocol_mode=(
            ConclusionProtocolMode.REQUIRE_STRUCTURED
            if kernel._dependencies.model.capabilities.structured_conclusion
            else ConclusionProtocolMode.OBSERVE
        ),
        cancellation_scope=kernel._cancellation_scope(state.task_id),
    )
    state.model_started_at = time.monotonic()
    state.live_goal = (await kernel.get_task(state.task_id)).goal
    kernel._notify_agent_progress(state.on_progress, AgentProgress(
        AgentProgressKind.MODEL_STARTED, model_call=state.model_call_number,
        max_model_calls=state.checkpoint.max_model_calls,
        tool_call=state.tool_call_count,
        max_tool_calls=state.checkpoint.max_tool_calls,
        goal=state.live_goal,
    ))
    state.buffered_text: list[str] = []
    pre_model_gaps = (
        await kernel._completion_readiness_gaps(state.task_id, state.visible_tools)
        if kernel._dependencies.completion_readiness_policy is not None
        else ()
    )
    state.should_buffer_text = any(gap.required for gap in pre_model_gaps)


async def _invoke_model(
    kernel: Any, state: TurnState,
) -> PhaseVerdict:
    """Perform this iteration's model call."""
    """Perform this iteration's model call.

    Extracted from ``_continue_agent_turn``. Answers with ``CONTINUE`` when
    the iteration must restart before the response is consumed -- the caller
    owns the loop, so the phase asks instead of breaking out of a loop it
    does not own. ``OK`` means the response is on ``state`` for the next
    phase; failures raise, exactly as before.
    """
    try:
        state.response = await kernel._complete_agent_model_request(
            state.request,
            state.buffered_text.append if state.should_buffer_text else state.on_text_delta,
        )
        kernel._notify_agent_progress(state.on_progress, AgentProgress(
            AgentProgressKind.MODEL_COMPLETED,
            model_call=state.model_call_number,
            max_model_calls=state.checkpoint.max_model_calls,
            tool_call=state.tool_call_count,
            max_tool_calls=state.checkpoint.max_tool_calls,
            elapsed_seconds=time.monotonic() - state.model_started_at,
            goal=state.live_goal,
        ))
        state.tool_calls = kernel._validate_agent_response(
            state.response.message, state.response.finish_reason,
            evidence_required_tools=frozenset(
                tool.name for tool in state.visible_tools
                if kernel._dependencies.require_evidence_questions
                and tool.requires_evidence_question
            ),
            allow_tool_calls=state.request.allow_tool_calls,
        )
        state.recovery_batch = kernel._latest_recoverable_tool_batch(state.messages)
        duplicate_call_ids = [
            call.call_id for call in state.tool_calls if call.call_id in state.seen_call_ids
        ]
        if duplicate_call_ids:
            raise InvalidModelResponse(
                "tool call_id must be unique within a turn: "
                + ", ".join(duplicate_call_ids)
            )
        state.tool_calls = tuple(
            replace(
                call, outcome_ref=None,
                outcome_binding_mode=OutcomeBindingMode.FULFILLMENT,
            )
            for call in state.tool_calls
        )
        kernel._validate_tool_batch_proposal(
            state.tool_calls, state.visible_tool_by_name, state.seen_call_ids
        )
        state.accepted_batch = (
            await kernel._new_tool_batch(
                state.task_id, state.response.message, state.tool_calls
            )
            if state.tool_calls else None
        )
        # A rejected response never consumes call IDs.  The corrected
        # response may legitimately reuse the Provider-generated IDs.
        state.seen_call_ids.update(call.call_id for call in state.tool_calls)
    except RecoverableToolProtocolError as error:
        state.model_call_count = state.model_call_number
        state.protocol_recovery_attempts += 1
        failure = ModelAttemptFailure(
            ModelFailureCategory.CORRECTABLE_PROTOCOL,
            ModelRetrySafety.SAFE_WITH_CORRECTION,
            error.reason_code, str(error),
        )
        policy = kernel._dependencies.model_recovery_policy
        if policy is None:
            decision = None
        else:
            decision = await policy.evaluate(ModelRecoveryProbe(
                failure, state.protocol_recovery_attempts, 2, False
            ))
        recovery_action = (
            decision.action if decision is not None
            else (
                ModelRecoveryAction.RESAMPLE_WITH_CORRECTION
                if state.protocol_recovery_attempts == 1
                else ModelRecoveryAction.FAIL_TERMINAL
            )
        )
        recovery_reason = (
            decision.reason_code if decision is not None
            else "compatibility_protocol_recovery"
        )
        await kernel._record_model_attempt_events(
            state.task_id, state.turn_id, state.model_call_number, [
                ModelTransportProgress(
                    "attempt_failed", state.protocol_recovery_attempts, 2,
                    category=failure.category.value,
                    retry_safety=failure.retry_safety.value,
                    diagnostic_code=failure.diagnostic_code,
                    reason=recovery_reason,
                ),
                ModelTransportProgress(
                    "recovery_decided", state.protocol_recovery_attempts, 2,
                    category=failure.category.value,
                    retry_safety=failure.retry_safety.value,
                    diagnostic_code=failure.diagnostic_code,
                    recovery_action=recovery_action.value,
                    reason=recovery_reason,
                ),
            ],
        )
        if (
            recovery_action
            is ModelRecoveryAction.RESAMPLE_WITH_CORRECTION
            and state.model_call_count < state.checkpoint.max_model_calls
        ):
            state.protocol_correction = kernel._protocol_correction_text(
                error.reason_code, error.detail
            )
            await kernel._append_events(state.task_id, ((
                "llm.protocol_retry_requested", {
                    "turn_id": state.turn_id,
                    "model_call": state.model_call_number,
                    "reason_code": error.reason_code,
                    "retry_limit": 1,
                },
            ),))
            state.checkpoint = replace(
                state.checkpoint, messages=tuple(state.messages),
                pending_tool_calls=(),
                seen_call_ids=tuple(sorted(state.seen_call_ids)),
                model_calls=state.model_call_count, tool_calls=state.tool_call_count,
                input_tokens=state.input_tokens, output_tokens=state.output_tokens,
            )
            await kernel._save_agent_checkpoint(
                state.checkpoint, "tool-protocol-retry-requested"
            )
            return PhaseVerdict(PhaseAction.CONTINUE)
        if (
            error.reason_code == "tool_call_emitted_while_disabled"
            and state.model_call_count >= state.checkpoint.max_model_calls
        ):
            limit_error = AgentLoopLimitExceeded(
                state.turn_id, "max_model_calls", state.checkpoint.max_model_calls,
                model_calls=state.model_call_count, tool_calls=state.tool_call_count,
                last_tool_error=state.last_tool_error,
                max_model_calls=state.checkpoint.max_model_calls,
                max_tool_calls=state.checkpoint.max_tool_calls,
            )
            await kernel._record_agent_loop_failure(
                state.task_id, state.turn_id, limit_error
            )
            raise limit_error from error
        state.checkpoint = replace(
            state.checkpoint, messages=tuple(state.messages),
            pending_tool_calls=(),
            seen_call_ids=tuple(sorted(state.seen_call_ids)),
            model_calls=state.model_call_count, tool_calls=state.tool_call_count,
            input_tokens=state.input_tokens, output_tokens=state.output_tokens,
        )
        await kernel._save_agent_checkpoint(
            state.checkpoint, "tool-protocol-recovery-exhausted"
        )
        await kernel._record_recoverable_turn_interruption(
            state.task_id, state.turn_id, error, state.prompt.receipt,
            failure_kind="tool_protocol",
            failure_category=ModelFailureCategory.CORRECTABLE_PROTOCOL,
            retry_safety=ModelRetrySafety.SAFE_WITH_CORRECTION,
            recovery_action=recovery_action,
        )
        raise ModelInvocationFailed(
            state.turn_id,
            "model did not satisfy the tool-call protocol after one "
            f"correction ({error.reason_code})",
            failure_kind="tool_protocol",
            failure_category=ModelFailureCategory.CORRECTABLE_PROTOCOL,
            retry_safety=ModelRetrySafety.SAFE_WITH_CORRECTION,
            recovery_action=recovery_action,
            diagnostic_detail=error.detail,
        ) from error
    except Exception as error:
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
        failure_kind = "model_response"
        if isinstance(error, InvalidModelResponse):
            await kernel._record_turn_failure(
                state.task_id, state.turn_id, error, state.prompt.receipt,
                failure_kind=failure_kind,
                failure_category=failure_category,
                retry_safety=retry_safety,
                recovery_action=recovery_action,
            )
        else:
            await kernel._record_recoverable_turn_interruption(
                state.task_id, state.turn_id, error, state.prompt.receipt,
                failure_kind=failure_kind,
                failure_category=failure_category,
                retry_safety=retry_safety,
                recovery_action=recovery_action,
            )
        raise ModelInvocationFailed(
            state.turn_id, str(error), failure_kind=failure_kind,
            failure_category=failure_category,
            retry_safety=retry_safety,
            recovery_action=recovery_action,
        ) from error
    return PhaseVerdict(PhaseAction.OK)


async def _finish_iteration(
    kernel: Any, state: TurnState,
) -> PhaseVerdict:
    """Consume the model response and decide how this iteration ends."""
    """Consume the model response and decide how this iteration ends.

    Extracted from ``_continue_agent_turn``. Its three ``return``s were the
    turn's terminal results and its ``continue``s restarted the iteration;
    both now travel as a ``PhaseVerdict`` so the caller owns the loop.
    ``OK`` means the iteration is complete and the loop should go round.
    """
    state.input_tokens += state.response.usage.input_tokens
    state.output_tokens += state.response.usage.output_tokens
    state.model_call_count = state.model_call_number
    state.protocol_correction = None
    state.messages.append(state.response.message)
    response_checkpoint = replace(
        state.checkpoint, messages=tuple(state.messages),
        pending_tool_calls=tuple(state.tool_calls),
        tool_batch=state.accepted_batch,
        seen_call_ids=tuple(sorted(state.seen_call_ids)),
        model_calls=state.model_call_count, tool_calls=state.tool_call_count,
        input_tokens=state.input_tokens, output_tokens=state.output_tokens,
    )
    # A follow-up may arrive while the provider is producing what looked
    # like the final answer.  Persist that response as an intermediate
    # checkpoint, merge the inbound message, and sample again rather
    # than acknowledging steering that the model never actually saw.
    events_after_model = await kernel._dependencies.store.read_events(state.task_id)
    steering_after_model = SteeringProjector.project(
        state.task_id, events_after_model
    )
    has_late_steering = any(
        item.inbound_sequence
        > response_checkpoint.last_steering_inbound_sequence
        for item in steering_after_model.pending
    )
    if (
        state.recovery_batch
        and not state.tool_calls
        and not has_late_steering
    ):
        recovery_gaps = tuple(CompletionGap(
            gap_id=f"tool-recovery:{tool_result.call_id}",
            kind="TOOL_RECOVERY_REQUIRED",
            description=(
                tool_result.message
                or f"{tool_result.error_code or 'TOOL_FAILURE'} requires recovery"
            ),
            status=tool_result.effective_recovery_kind.value.upper(),
            required=True, recoverable=True,
            observed=json.dumps(
                tool_result.recovery_action,
                ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ),
        ) for tool_result in state.recovery_batch)
        recovery_checkpoint = replace(
            response_checkpoint,
            messages=tuple(state.messages[:-1]),
            pending_tool_calls=(), tool_batch=None,
        )
        blocker = kernel._recovery_batch_blocker_message(state.recovery_batch)
        suspension = await kernel._suspend_incomplete_recoverable(
            recovery_checkpoint, blocker, recovery_gaps,
            reason="unresolved_tool_batch",
        )
        source_user_message = next((
            message for message in state.messages
            if message.role is MessageRole.USER
            and not message.message_id.startswith((
                "project-onboarding-context-",
                "project-memory-context-", "session-context-",
                "working-memory-context-", "task-spec-context-",
                "project-instructions-context-", "completion-readiness-",
            ))
        ), None)
        if source_user_message is not None:
            await kernel._record_session_task_result(
                state.task_id, state.turn_id, source_user_message, blocker
            )
            await kernel._refresh_continuation_session_identity(state.task_id)
        return PhaseVerdict(PhaseAction.RETURN, suspension)
    readiness = CompletionReadinessDecision(
        CompletionReadinessAction.COMPLETE, "not_a_final_candidate",
        CompletionReadinessState.from_data(
            response_checkpoint.completion_readiness_state
        ),
    )
    state.legacy_completion_gate = (
        kernel.completion_readiness_mode
        is CompletionReadinessMode.LEGACY_GATE
    )
    if not state.tool_calls and not has_late_steering:
        readiness = await kernel._evaluate_completion_readiness(
            state.task_id, state.turn_id, response_checkpoint, state.visible_tools,
            forced_wrap_up=state.wrap_up or state.disclosure_only,
        )
        response_checkpoint = replace(
            response_checkpoint,
            completion_readiness_state=readiness.state.to_data(),
        )
        renewal_events: tuple[tuple[str, Mapping[str, Any]], ...] = ()
        renewable_action = state.legacy_completion_gate and (
            readiness.action in {
                CompletionReadinessAction.CONTINUE,
                CompletionReadinessAction.REPORT_INCOMPLETE_RECOVERABLE,
                # A bounded stop with recoverable work left is exactly
                # what the renewal budget exists for. Keyed on the
                # action, never on a reason string.
                CompletionReadinessAction.EXHAUSTED,
            }
        )
        renewable_effects = {
            ToolEffect.OBSERVE
            if tool.effect is ToolEffect.UNSPECIFIED and tool.is_read_only
            else tool.effect
            for tool in state.visible_tools
            if not tool.is_internal_state
        }
        required_recoverable_work = renewable_action and any(
            gap.required
            and gap.effective_required_effects
            and gap.effective_required_effects.issubset(
                renewable_effects
            )
            for gap in readiness.gaps
        )
        renewal_policy = DiminishingModelCallBudgetPolicy(
            increments=kernel._dependencies.model_call_renewal_increments,
            max_renewals=(
                kernel._dependencies.model_call_renewal_max_count
            ),
            absolute_limit=(
                kernel._dependencies.model_call_renewal_absolute_limit
            ),
            threshold=kernel._dependencies.model_call_renewal_threshold,
        )
        renewal = renewal_policy.evaluate(
            current_limit=response_checkpoint.max_model_calls,
            consumed=state.model_call_count,
            renewal_count=(
                response_checkpoint.model_budget_renewal_count
            ),
            required_recoverable_work=required_recoverable_work,
        )
        if state.legacy_completion_gate and renewal is not None:
            # A renewal buys *capacity*, not more nudges. Resetting the
            # correction counter here let a renewal refill the very budget the
            # nudge is capped against, so one instruction could be repeated until
            # the renewal sequence ran out (measured: 25 nudges, 26 calls).
            renewed_state = CompletionReadinessState(
                continue_attempts=readiness.state.continue_attempts,
                disclosure_attempts=readiness.state.disclosure_attempts,
                automatic_resume_attempts=renewal.renewal_count,
                last_action="",
                schema_version=readiness.state.schema_version,
                stalled_continuations=(
                    readiness.state.stalled_continuations
                ),
            )
            response_checkpoint = replace(
                response_checkpoint,
                max_model_calls=renewal.new_limit,
                model_budget_renewal_count=renewal.renewal_count,
                model_budget_total_granted=(
                    response_checkpoint.model_budget_total_granted
                    + renewal.increment
                ),
                completion_readiness_state=renewed_state.to_data(),
            )
            action_before = readiness.action
            reason_before = readiness.reason
            readiness = CompletionReadinessDecision(
                CompletionReadinessAction.CONTINUE,
                "model_call_budget_renewed",
                renewed_state, readiness.gaps,
            )
            renewal_events = (("budget.renewed", {
                "turn_id": state.turn_id,
                "renewal_count": renewal.renewal_count,
                "increment": renewal.increment,
                "previous_max_model_calls": renewal.previous_limit,
                "max_model_calls": renewal.new_limit,
                "absolute_max_model_calls": (
                    renewal_policy.absolute_limit
                ),
                "remaining_model_calls_before": (
                    renewal.remaining_before
                ),
                "readiness_action_before": action_before.value,
                "readiness_reason": reason_before,
                "gap_ids": [gap.gap_id for gap in readiness.gaps],
            }),)
    else:
        renewal_events = ()
    if (
        not state.legacy_completion_gate
        and not state.tool_calls
        and not has_late_steering
    ):
        readiness = CompletionReadinessDecision(
            CompletionReadinessAction.COMPLETE,
            "diagnostic_only", readiness.state, readiness.gaps,
        )
    # INV-13 / INV-16: a required gap may never coexist with a success
    # verdict. This is enforced here, at the consumer, so it also covers
    # policies this Runtime does not own -- including the "no policy
    # configured" default and any custom adapter. The diagnostic-only
    # per-turn override above is intentionally exempt: it hands the
    # decision to the caller and never finalises or fails the Task.
    required_gaps_open = any(
        gap.required for gap in readiness.gaps
    )
    if (
        state.legacy_completion_gate
        and required_gaps_open
        and readiness.action is CompletionReadinessAction.COMPLETE
    ):
        await kernel._append_events(state.task_id, ((
            "completion.invariant_violated", {
                "turn_id": state.turn_id,
                "violation": "complete_with_open_required_gaps",
                "reason": readiness.reason,
                "gap_ids": [
                    gap.gap_id for gap in readiness.gaps if gap.required
                ],
            },
        ),))
        exhausted_state = replace(
            readiness.state,
            last_action=CompletionReadinessAction.EXHAUSTED.value,
        )
        readiness = CompletionReadinessDecision(
            CompletionReadinessAction.EXHAUSTED,
            "complete_with_open_required_gaps",
            exhausted_state, readiness.gaps,
        )
        response_checkpoint = replace(
            response_checkpoint,
            completion_readiness_state=exhausted_state.to_data(),
        )
    final_response = bool(
        not state.tool_calls and not has_late_steering
        and readiness.action is CompletionReadinessAction.COMPLETE
        and not required_gaps_open
    )
    # A bounded stop with requirements still open ends the turn at a
    # resumable boundary (INV-14): the objective axis records "work
    # remains" here, so the acceptance layer is never the first to
    # discover it (INV-16).
    unmet_boundary = bool(
        not state.tool_calls
        and required_gaps_open
        and readiness.action in {
            CompletionReadinessAction.REPORT_BLOCKED,
            CompletionReadinessAction.EXHAUSTED,
        }
    )
    # The first "stop with work open" verdict buys one wrap-up round so
    # the user gets an honest blocker report; the second one suspends.
    # Derived from the persisted last action, not from a counter.
    # ``remaining_model_calls`` still counts the call just made, so a
    # further round needs more than one left; otherwise suspend with the
    # checkpoint already in hand.
    exhausted_wrap_up_due = bool(
        readiness.action is CompletionReadinessAction.EXHAUSTED
        and state.completion_state.last_action
        != CompletionReadinessAction.EXHAUSTED.value
        and state.remaining_model_calls > 1
    )
    incomplete_recovery_boundary = bool(
        state.legacy_completion_gate and unmet_boundary
        and not exhausted_wrap_up_due
    )
    await kernel._commit_model_response_checkpoint(
        response_checkpoint, state.response, state.prompt.receipt, state.prepared.budget,
        final=final_response or incomplete_recovery_boundary,
        additional_events=renewal_events,
    )
    if state.should_buffer_text and (
        state.tool_calls or has_late_steering or final_response
    ) and state.on_text_delta is not None:
        for text_delta in state.buffered_text:
            state.on_text_delta(text_delta)
    if not state.tool_calls and has_late_steering:
        response_checkpoint, _ = await kernel._apply_pending_steering(
            response_checkpoint, "after-model-response"
        )
        state.checkpoint = response_checkpoint
        state.messages = list(response_checkpoint.messages)
        state.pending_tool_calls = list(response_checkpoint.pending_tool_calls)
        return PhaseVerdict(PhaseAction.CONTINUE)
    if not state.tool_calls:
        # A stop gate may nudge -- bounded, in-turn -- but it never strands the
        # turn. When the control budget is spent, whatever the model said is the
        # answer: the turn ends and the Task completes. Hermes and Codex have no
        # equivalent suspension at all; their stop gates are bounded nudges that
        # then let the turn finish.
        if (
            not incomplete_recovery_boundary
            and readiness.action is not CompletionReadinessAction.COMPLETE
        ):
            correction = kernel._completion_correction_message(readiness)
            state.messages.append(correction)
            response_checkpoint = replace(
                response_checkpoint, messages=tuple(state.messages),
                pending_tool_calls=(),
                completion_readiness_state=readiness.state.to_data(),
            )
            event_type = (
                "completion.continuation_requested"
                if readiness.action is CompletionReadinessAction.CONTINUE
                else (
                    "completion.incomplete_recoverable_requested"
                    if readiness.action is CompletionReadinessAction.REPORT_INCOMPLETE_RECOVERABLE
                    else "completion.blocker_disclosure_requested"
                )
            )
            await kernel._append_events(state.task_id, ((event_type, {
                "turn_id": state.turn_id, "reason": readiness.reason,
                "gap_count": len(readiness.gaps),
                "gap_ids": [gap.gap_id for gap in readiness.gaps],
            }),))
            await kernel._save_agent_checkpoint(
                response_checkpoint, "completion-readiness-correction"
            )
            state.checkpoint = response_checkpoint
            state.pending_tool_calls = []
            return PhaseVerdict(PhaseAction.CONTINUE)
        source_user_message = next((
            message for message in state.messages
            if message.role is MessageRole.USER
            and not message.message_id.startswith((
                "project-onboarding-context-",
                "project-memory-context-", "session-context-",
                "working-memory-context-", "task-spec-context-",
                "project-instructions-context-",
            ))
        ), None)
        if source_user_message is None:
            raise RuntimeError("Agent turn lost its source user message")
        return PhaseVerdict(PhaseAction.RETURN, AgentTurnResult(turn_id=state.turn_id, task_id=state.task_id, assistant_message=state.response.message, finish_reason=state.response.finish_reason, usage=ModelUsage(state.input_tokens, state.output_tokens), model_calls=state.model_call_count, tool_calls=state.tool_call_count, messages=tuple(state.messages)))

    if state.tool_call_count + len(state.tool_calls) > state.checkpoint.max_tool_calls:
        error = AgentLoopLimitExceeded(
            state.turn_id, "max_tool_calls", state.checkpoint.max_tool_calls,
            model_calls=state.model_call_count, tool_calls=state.tool_call_count,
            last_tool_error=state.last_tool_error,
            max_model_calls=state.checkpoint.max_model_calls,
            max_tool_calls=state.checkpoint.max_tool_calls,
        )
        await kernel._record_agent_loop_failure(state.task_id, state.turn_id, error)
        raise error
    if state.model_call_count == state.checkpoint.max_model_calls:
        error = AgentLoopLimitExceeded(
            state.turn_id, "max_model_calls", state.checkpoint.max_model_calls,
            model_calls=state.model_call_count, tool_calls=state.tool_call_count,
            last_tool_error=state.last_tool_error,
            max_model_calls=state.checkpoint.max_model_calls,
            max_tool_calls=state.checkpoint.max_tool_calls,
        )
        await kernel._record_agent_loop_failure(state.task_id, state.turn_id, error)
        raise error

    state.pending_tool_calls = list(state.tool_calls)
    state.checkpoint = response_checkpoint
    return PhaseVerdict(PhaseAction.OK)
