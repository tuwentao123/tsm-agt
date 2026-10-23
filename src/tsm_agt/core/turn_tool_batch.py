"""Tool-batch phase of an agent turn, extracted from ``core/kernel.py``.

Reads and writes ``TurnState`` and calls back into the Kernel it is handed; the
closure of the original method was exactly ``(self, state)``, so ``self`` became
the ``kernel`` parameter and nothing else changed.

No name here is defined in ``kernel.py``, so this module never imports the facade
-- the dependency runs one way, which is what keeps ``kernel.py`` free to import
this sibling at module level.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import json
import time

from .agent_loop import (
    AgentClarificationSuspended, AgentProgress, AgentProgressKind,
    AgentTurnCheckpoint, AgentTurnSuspended, ToolBatchStatus, TurnState,
)
from .approval import (
    ApprovalRequired,
)
from .clarification import (
    ClarificationRequired,
)
from .configuration import (
    canonical_hash,
)
from dataclasses import (
    replace,
)
from .execution import (
    ToolCommitState, ToolExecutionRecord,
)
from .execution_facts import (
    EffectStatus,
    classify_effect_status,
    project_execution_fact,
    project_tool_execution_fact,
)
from .exploration_coordinator import (
    ExplorationCoordinatorAction,
)
from pathlib import (
    Path,
)
from .plan_guard import (
    ActionProgressState,
)
from .trust import (
    workspace_fingerprint,
)
from tsm_agt.ports import (
    ArtifactReadAction, ExplorationBudgetAction, ExplorationBudgetObservation, ExplorationBudgetProbe, ExplorationBudgetState, ExplorationOutcomeAction, Message, MessageRole, ProgressiveScopeAction, ProgressiveScopeDecision, ReadHitsAction, StopOrPivotAction, StopOrPivotSignals, TextBlock, ToolCall, ToolRecoveryKind, ToolResult, ToolResultBlock,
)
from .turn import (
    InvalidModelResponse,
)
from uuid import (
    uuid4,
)


def _govern_runtime_result(
    kernel: Any,
    state: TurnState,
    call: ToolCall,
    result: ToolResult,
) -> ToolResult:
    """Apply the per-tool model-content budget to loop-owned guard feedback."""
    governance = result.meta.get("result_governance")
    if isinstance(governance, Mapping) and governance.get("schema_version") == 2:
        return result
    spec = state.visible_tool_by_name.get(call.name)
    if spec is None:
        return result
    return kernel._dependencies.tool_result_governor.govern(spec, call, result)


async def run_tool_batch(
    kernel: Any, state: TurnState,
) -> AgentTurnSuspended | AgentClarificationSuspended | None:
    """Drain pending tool calls for a ConversationAgentLoop turn."""
    while state.pending_tool_calls:
        state.checkpoint, replaced = await kernel._apply_pending_steering(
            replace(
                state.checkpoint, messages=tuple(state.messages),
                pending_tool_calls=tuple(state.pending_tool_calls),
                seen_call_ids=tuple(sorted(state.seen_call_ids)),
                model_calls=state.model_call_count, tool_calls=state.tool_call_count,
                input_tokens=state.input_tokens, output_tokens=state.output_tokens,
            ), "before-tool",
        )
        state.messages = list(state.checkpoint.messages)
        state.pending_tool_calls = list(state.checkpoint.pending_tool_calls)
        if replaced:
            break
        call = state.pending_tool_calls[0]
        call_spec = state.visible_tool_by_name.get(call.name)
        frozen_call = (
            next((item for item in state.checkpoint.tool_batch.calls
                  if item.call_id == call.call_id), None)
            if state.checkpoint.tool_batch is not None else None
        )

        if frozen_call is not None:
            # Outcome bindings and normalized arguments were accepted
            # atomically with the batch. Never reinterpret them after a
            # previous call changes Task state.
            call = frozen_call
            state.pending_tool_calls[0] = call
        elif call_spec is not None and call.outcome_ref is None:
            # Resolve the Action→Outcome edge before emitting any
            # lifecycle Event so requested/policy/approval/result all
            # carry the same authoritative reference.
            call = await kernel._bind_tool_call_outcome(
                state.task_id, call, call_spec
            )
            state.pending_tool_calls[0] = call
        elif call_spec is not None:
            persisted = (await kernel.get_task(state.task_id)).tool_executions.get(
                ToolExecutionRecord.identity(state.turn_id, call.call_id)
            )
            call = await kernel._bind_tool_call_outcome(
                state.task_id, call, call_spec,
                # Calls in this list were already accepted into the
                # durable checkpoint while their Outcome was active.
                # This also lets pre-fix checkpoints finish a batch
                # whose first result closed the shared Outcome early.
                allow_closed_ref=True,
            )
            state.pending_tool_calls[0] = call
        task_before_action = await kernel.get_task(state.task_id)
        memory_before_action = await kernel.get_working_memory(state.task_id)
        semantic_action = await kernel._classify_semantic_action(
            state.task_id, state.turn_id, call
        )
        scope_probe = kernel._progressive_scope_probe(
            Path(task_before_action.workspace), call, semantic_action
        )
        exploration_decision = None
        effective_tool_timeout_seconds = state.checkpoint.tool_timeout_seconds
        scope_decision = ProgressiveScopeDecision(
            ProgressiveScopeAction.ALLOW, "evidence_guided",
            "evidence_guided", current_depth=scope_probe.depth,
        )
        if (
            kernel._exploration_coordinator is not None
            and scope_probe.scope_hash
        ):
            exploration_decision = await kernel._evaluate_exploration_before(
                state.task_id, state.turn_id, call, semantic_action,
                state.checkpoint.evidence_relation_state,
                state.checkpoint.rejection_loop_state,
                state.checkpoint.evidence_inventory,
            )
            state.checkpoint = replace(
                state.checkpoint,
                evidence_relation_state=(
                    exploration_decision.relation_state.to_data()
                ),
                rejection_loop_state=(
                    exploration_decision.rejection_state.to_data()
                ),
            )
            if exploration_decision.call != call:
                call = exploration_decision.call
                state.pending_tool_calls[0] = call
                semantic_action = await kernel._classify_semantic_action(
                    state.task_id, state.turn_id, call
                )
                scope_probe = kernel._progressive_scope_probe(
                    Path(task_before_action.workspace), call, semantic_action
                )
            if exploration_decision.probe_timeout_seconds is not None:
                effective_tool_timeout_seconds = min(
                    effective_tool_timeout_seconds,
                    exploration_decision.probe_timeout_seconds,
                )
            if exploration_decision.action in {
                ExplorationCoordinatorAction.RETURN_FEEDBACK,
                ExplorationCoordinatorAction.STOP_ROUTE,
                ExplorationCoordinatorAction.ASK_USER,
            }:
                result = ToolResult(
                    call.call_id, False,
                    error_code="EXPLORATION_ROUTE_REJECTED",
                    message=(
                        "The proposed exploration route is not supported "
                        "by the evidence gathered so far."
                    ),
                    hint=(
                        "Use a known evidence relation, change the method, "
                        "or ask the user when the target is ambiguous."
                    ),
                    meta={
                        "runtime_guard": "EVIDENCE_GUIDED_EXPLORATION",
                        "next_action": exploration_decision.action.value,
                        "reason": exploration_decision.reason,
                        "occurrence": (
                            exploration_decision.rejection_occurrence
                        ),
                    },
                )
                state.messages.append(Message(
                    message_id=f"msg-tool-{uuid4().hex}",
                    role=MessageRole.TOOL,
                    content=(ToolResultBlock(_govern_runtime_result(kernel, state, call, result)),),
                ))
                state.pending_tool_calls.pop(0)
                state.checkpoint = replace(
                    state.checkpoint, messages=tuple(state.messages),
                    pending_tool_calls=tuple(state.pending_tool_calls),
                    seen_call_ids=tuple(sorted(state.seen_call_ids)),
                )
                await kernel._save_agent_checkpoint(
                    state.checkpoint, "exploration-route-rejected"
                )
                continue
        else:
            scope_decision = await kernel._evaluate_progressive_scope_before(
                state.task_id, state.turn_id, call, semantic_action, scope_probe,
                state.checkpoint.progressive_scope_state,
            )
        if (
            kernel._exploration_coordinator is None
            and scope_decision.action
            is ProgressiveScopeAction.REQUIRE_EXPANSION_REASON
        ):
            pivot_decision, pivot_state = await kernel._decide_stop_or_pivot(
                state.task_id, state.turn_id, call, StopOrPivotSignals(
                    semantic_family=(
                        semantic_action.family.value
                        if semantic_action is not None else ""
                    ),
                    semantic_signature=(
                        semantic_action.semantic_signature
                        if semantic_action is not None else ""
                    ),
                    scope_reason_required=True,
                    scope_relation=scope_decision.relation,
                ), state.checkpoint.stop_or_pivot_state,
            )
            await kernel._emit_agent_progress_projection(
                state.task_id, state.turn_id, call, semantic_action,
                scope_decision.relation, scope_probe.depth,
                None, None, pivot_decision.action.value,
                "decision", state.on_progress, reason=pivot_decision.reason,
                goal=task_before_action.goal,
            )
            result = ToolResult(
                call.call_id, False,
                error_code="SCOPE_EXPANSION_REASON_REQUIRED",
                message=(
                    "This search expands a scope that was already narrowed "
                    "for the same evidence question and search method."
                ),
                hint=(
                    "Retry only if broader search is necessary. Otherwise keep "
                    "the narrower scope or change the investigation method."
                ),
                meta={
                    "runtime_guard": "PROGRESSIVE_SCOPE",
                    "previous_depth": scope_decision.previous_depth,
                    "current_depth": scope_decision.current_depth,
                    "next_action": pivot_decision.action.value,
                },
            )
            state.messages.append(Message(
                message_id=f"msg-tool-{uuid4().hex}",
                role=MessageRole.TOOL,
                content=(ToolResultBlock(_govern_runtime_result(kernel, state, call, result)),),
            ))
            state.pending_tool_calls.pop(0)
            state.checkpoint = replace(
                state.checkpoint, messages=tuple(state.messages),
                pending_tool_calls=tuple(state.pending_tool_calls),
                seen_call_ids=tuple(sorted(state.seen_call_ids)),
                stop_or_pivot_state=pivot_state,
            )
            await kernel._save_agent_checkpoint(
                state.checkpoint, "scope-expansion-reason-required"
            )
            continue
        budget_decision = await kernel._evaluate_exploration_budget_before(
            state.task_id, state.turn_id, call, semantic_action,
            ExplorationBudgetProbe(
                state.checkpoint.max_model_calls - state.model_call_count,
                state.checkpoint.max_tool_calls - state.tool_call_count,
                state.checkpoint.max_model_calls, state.checkpoint.max_tool_calls,
            ),
            state.checkpoint.exploration_budget_state,
        )
        if budget_decision.action is ExplorationBudgetAction.FOCUS:
            previous_budget = ExplorationBudgetState.from_data(
                state.checkpoint.exploration_budget_state
            )
            entered_focus = not previous_budget.focus_mode
            focused_budget = replace(
                previous_budget, focus_mode=True,
                focus_reason=budget_decision.reason,
            )
            state.checkpoint = replace(
                state.checkpoint,
                exploration_budget_state=focused_budget.to_data(),
            )
            if entered_focus:
                await kernel._append_events(state.task_id, ((
                    "exploration_budget.focus_entered", {
                        "turn_id": state.turn_id,
                        "tool_call_id": call.call_id,
                        "tool_name": call.name,
                        "reason": budget_decision.reason,
                        "scored_actions": budget_decision.scored_actions,
                        "used_tool_calls": budget_decision.used_tool_calls,
                    },
                ),))
                kernel._notify_agent_progress(state.on_progress, AgentProgress(
                    AgentProgressKind.FOCUS,
                    model_call=state.model_call_count,
                    max_model_calls=state.checkpoint.max_model_calls,
                    tool_call=state.tool_call_count,
                    max_tool_calls=state.checkpoint.max_tool_calls,
                    goal=task_before_action.goal,
                    reason=budget_decision.reason,
                    exploration_actions=budget_decision.scored_actions,
                    max_exploration_actions=(
                        budget_decision.max_scored_actions
                    ),
                    exploration_tool_calls=(
                        budget_decision.used_tool_calls
                    ),
                    max_exploration_tool_calls=(
                        budget_decision.max_total_tool_calls
                    ),
                ))
            if not budget_decision.focus_allows_call:
                result = ToolResult(
                    call.call_id, False,
                    error_code="EXPLORATION_FOCUS_REQUIRED",
                    message=(
                        "The exploration soft threshold was reached. "
                        "This broad action would open a new investigation "
                        "branch instead of completing direct evidence."
                    ),
                    hint=(
                        "Use a known file path, an explicit symbol "
                        "definition/reference, or a search scoped to one "
                        "specific subdirectory with max_matches <= 20. "
                        "Do not ask the user to increase an internal budget."
                    ),
                    meta={
                        "runtime_guard": "EXPLORATION_FOCUS",
                        "reason": budget_decision.reason,
                        "next_action": "NARROW_TO_DIRECT_EVIDENCE",
                    },
                )
                state.messages.append(Message(
                    message_id=f"msg-tool-{uuid4().hex}",
                    role=MessageRole.TOOL,
                    content=(ToolResultBlock(_govern_runtime_result(kernel, state, call, result)),),
                ))
                state.pending_tool_calls.pop(0)
                state.checkpoint = replace(
                    state.checkpoint, messages=tuple(state.messages),
                    pending_tool_calls=tuple(state.pending_tool_calls),
                    seen_call_ids=tuple(sorted(state.seen_call_ids)),
                )
                await kernel._save_agent_checkpoint(
                    state.checkpoint, "exploration-focus-required"
                )
                continue
        if budget_decision.action is ExplorationBudgetAction.WRAP_UP:
            pivot_decision, pivot_state = await kernel._decide_stop_or_pivot(
                state.task_id, state.turn_id, call, StopOrPivotSignals(
                    semantic_family=(
                        semantic_action.family.value
                        if semantic_action is not None else ""
                    ),
                    semantic_signature=(
                        semantic_action.semantic_signature
                        if semantic_action is not None else ""
                    ),
                    budget_wrap_up=True,
                    budget_reason=budget_decision.reason,
                    low_value_streak=budget_decision.low_value_streak,
                    remaining_model_calls=(
                        state.checkpoint.max_model_calls - state.model_call_count
                    ),
                    remaining_tool_calls=(
                        state.checkpoint.max_tool_calls - state.tool_call_count
                    ),
                ), state.checkpoint.stop_or_pivot_state,
            )
            await kernel._emit_agent_progress_projection(
                state.task_id, state.turn_id, call, semantic_action,
                scope_decision.relation, scope_probe.depth,
                None, None, pivot_decision.action.value,
                "decision", state.on_progress, reason=pivot_decision.reason,
                goal=task_before_action.goal,
                budget_decision=budget_decision,
            )
            if pivot_decision.action is StopOrPivotAction.CONTINUE:
                previous_budget = ExplorationBudgetState.from_data(
                    state.checkpoint.exploration_budget_state
                )
                state.checkpoint = replace(
                    state.checkpoint,
                    exploration_budget_state=replace(
                        previous_budget, low_value_streak=0
                    ).to_data(),
                    stop_or_pivot_state=pivot_state,
                )
            elif pivot_decision.action is StopOrPivotAction.CHANGE_METHOD:
                result = ToolResult(
                    call.call_id, False,
                    error_code="EXPLORATION_CHANGE_METHOD_REQUIRED",
                    message=(
                        "The current exploration method produced consecutive "
                        "low-value results."
                    ),
                    hint=(
                        "Change the semantic method: for example read an existing "
                        "hit, inspect a definition/reference, narrow the scope, or "
                        "ask the user. Do not repeat the same search method."
                    ),
                    meta={
                        "runtime_guard": "STOP_OR_PIVOT",
                        "next_action": pivot_decision.action.value,
                        "reason": pivot_decision.reason,
                    },
                )
                state.messages.append(Message(
                    message_id=f"msg-tool-{uuid4().hex}",
                    role=MessageRole.TOOL,
                    content=(ToolResultBlock(_govern_runtime_result(kernel, state, call, result)),),
                ))
                state.pending_tool_calls.pop(0)
                state.checkpoint = replace(
                    state.checkpoint, messages=tuple(state.messages),
                    pending_tool_calls=tuple(state.pending_tool_calls),
                    seen_call_ids=tuple(sorted(state.seen_call_ids)),
                    stop_or_pivot_state=pivot_state,
                )
                await kernel._save_agent_checkpoint(
                    state.checkpoint, "exploration-change-method-required"
                )
                continue
            else:
                result = ToolResult(
                    call.call_id, False,
                    error_code="EXPLORATION_BUDGET_WRAP_UP",
                    message=(
                        "Exploration should stop based on the current evidence "
                        "and progress state."
                    ),
                    hint=(
                        "Do not call more tools. Answer from collected evidence "
                        "and state anything that remains unverified."
                    ),
                    meta={
                        "runtime_guard": "STOP_OR_PIVOT",
                        "next_action": pivot_decision.action.value,
                        "reason": pivot_decision.reason,
                    },
                )
                state.messages.append(Message(
                    message_id=f"msg-tool-{uuid4().hex}",
                    role=MessageRole.TOOL,
                    content=(ToolResultBlock(_govern_runtime_result(kernel, state, call, result)),),
                ))
                state.pending_tool_calls.pop(0)
                state.budget_wrap_up = True
                state.budget_wrap_up_decision = budget_decision
                state.checkpoint = replace(
                    state.checkpoint, messages=tuple(state.messages),
                    pending_tool_calls=tuple(state.pending_tool_calls),
                    seen_call_ids=tuple(sorted(state.seen_call_ids)),
                    stop_or_pivot_state=pivot_state,
                )
                await kernel._save_agent_checkpoint(
                    state.checkpoint, "exploration-budget-wrap-up"
                )
                continue
        read_hits_decision = await kernel._evaluate_read_hits_before(
            state.task_id, state.turn_id, call, semantic_action,
            state.checkpoint.read_hits_state,
        )
        if read_hits_decision.action is ReadHitsAction.REQUIRE_READ:
            pivot_decision, pivot_state = await kernel._decide_stop_or_pivot(
                state.task_id, state.turn_id, call, StopOrPivotSignals(
                    semantic_family=(
                        semantic_action.family.value
                        if semantic_action is not None else ""
                    ),
                    semantic_signature=(
                        semantic_action.semantic_signature
                        if semantic_action is not None else ""
                    ),
                    read_hits_required=True,
                    candidate_count=read_hits_decision.candidate_count,
                ), state.checkpoint.stop_or_pivot_state,
            )
            await kernel._emit_agent_progress_projection(
                state.task_id, state.turn_id, call, semantic_action,
                scope_decision.relation, scope_probe.depth,
                None, None, pivot_decision.action.value,
                "decision", state.on_progress, reason=pivot_decision.reason,
                goal=task_before_action.goal,
            )
            result = ToolResult(
                call.call_id, False, error_code="READ_HITS_REQUIRED",
                message=(
                    "A previous search for this evidence question already "
                    "returned a small set of concrete candidate files."
                ),
                hint=(
                    "Read one of the candidate paths from the previous tool "
                    "result with core.read_file or code.symbol_overview before "
                    "searching again. Use a new evidence question only when the "
                    "unknown has genuinely changed."
                ),
                meta={
                    "runtime_guard": "READ_HITS",
                    "candidate_count": (
                        read_hits_decision.candidate_count
                    ),
                    "next_action": pivot_decision.action.value,
                },
            )
            state.messages.append(Message(
                message_id=f"msg-tool-{uuid4().hex}",
                role=MessageRole.TOOL,
                content=(ToolResultBlock(_govern_runtime_result(kernel, state, call, result)),),
            ))
            state.pending_tool_calls.pop(0)
            state.checkpoint = replace(
                state.checkpoint, messages=tuple(state.messages),
                pending_tool_calls=tuple(state.pending_tool_calls),
                seen_call_ids=tuple(sorted(state.seen_call_ids)),
                stop_or_pivot_state=pivot_state,
            )
            await kernel._save_agent_checkpoint(
                state.checkpoint, "read-hits-required"
            )
            continue
        artifact_probe = kernel._artifact_read_probe(
            Path(task_before_action.workspace), call,
            state.checkpoint.artifact_read_state,
        )
        artifact_decision = await kernel._evaluate_artifact_read_before(
            state.task_id, state.turn_id, call, artifact_probe,
            state.checkpoint.artifact_read_state,
        )
        if artifact_decision.action is ArtifactReadAction.REQUIRE_REUSE:
            result = ToolResult(
                call.call_id, False, error_code="ARTIFACT_ALREADY_READ",
                message=(
                    "The same unchanged file range was already read for this "
                    "same call fingerprint."
                ),
                hint=(
                    "Reuse the existing Tool Result or Evidence. Read a different "
                    "line range, use different effective arguments, or "
                    "read again after the file content changes."
                ),
                meta={
                    "runtime_guard": "ARTIFACT_READ_CACHE",
                    "matching_record_count": (
                        artifact_decision.matching_record_count
                    ),
                },
            )
            state.messages.append(Message(
                message_id=f"msg-tool-{uuid4().hex}",
                role=MessageRole.TOOL,
                content=(ToolResultBlock(_govern_runtime_result(kernel, state, call, result)),),
            ))
            state.pending_tool_calls.pop(0)
            state.checkpoint = replace(
                state.checkpoint, messages=tuple(state.messages),
                pending_tool_calls=tuple(state.pending_tool_calls),
                seen_call_ids=tuple(sorted(state.seen_call_ids)),
            )
            await kernel._save_agent_checkpoint(
                state.checkpoint, "artifact-read-reuse-required"
            )
            continue
        decision = state.plan_guard.evaluate(
            call, memory_before_action,
            workspace_fingerprint(
                Path(task_before_action.workspace),
                kernel._dependencies.workspace_path,
            ),
            ActionProgressState.from_data(state.checkpoint.action_progress),
            semantic_action,
        )
        await kernel._append_events(state.task_id, (("plan.action_evaluated", {
            "turn_id": state.turn_id,
            "tool_call_id": call.call_id,
            "tool_name": call.name,
            "step_id": decision.goal_slice.step_id,
            "step_description_hash": canonical_hash(
                decision.goal_slice.description
            ),
            "completion_criteria_hash": canonical_hash(
                decision.goal_slice.completion_criteria
            ),
            "implicit_step": decision.goal_slice.implicit,
            "action_signature": decision.action_signature,
            "semantic_signature": decision.semantic_signature,
            "semantic_family": decision.semantic_family,
            "semantic_repeat": decision.semantic_repeat,
            "relevant_state_hash": decision.relevant_state_hash,
            "consecutive_no_progress": (
                decision.consecutive_no_progress
            ),
            "should_stop": decision.should_stop,
        }),))
        state.checkpoint = replace(
            state.checkpoint, action_progress=ActionProgressState(
                decision.action_signature, decision.relevant_state_hash,
                decision.consecutive_no_progress,
                False, decision.semantic_signature,
                decision.semantic_family,
            ).to_data(),
        )
        pivot_decision, pivot_state = await kernel._decide_stop_or_pivot(
            state.task_id, state.turn_id, call, StopOrPivotSignals(
                semantic_family=decision.semantic_family,
                semantic_signature=decision.semantic_signature,
                plan_should_stop=decision.should_stop,
                plan_finished=decision.goal_slice.plan_finished,
                consecutive_no_progress=decision.consecutive_no_progress,
                remaining_model_calls=(
                    state.checkpoint.max_model_calls - state.model_call_count
                ),
                remaining_tool_calls=(
                    state.checkpoint.max_tool_calls - state.tool_call_count
                ),
            ), state.checkpoint.stop_or_pivot_state,
        )
        state.checkpoint = replace(
            state.checkpoint, stop_or_pivot_state=pivot_state
        )
        await kernel._emit_agent_progress_projection(
            state.task_id, state.turn_id, call, semantic_action,
            scope_decision.relation, scope_probe.depth,
            None, None, pivot_decision.action.value,
            "planned", state.on_progress, reason=pivot_decision.reason,
            goal=task_before_action.goal,
        )
        if pivot_decision.terminal:
            cancelled_calls = tuple(state.pending_tool_calls)
            for cancelled in cancelled_calls:
                state.messages.append(Message(
                    f"msg-tool-cancelled-{uuid4().hex}",
                    MessageRole.TOOL,
                    (ToolResultBlock(ToolResult(
                        cancelled.call_id, False,
                        error_code="CANCELLED_BY_RUNTIME_POLICY",
                        message=(
                            "The accepted call was not started because "
                            "the Runtime reached a no-progress safety stop."
                        ),
                        recovery_kind=ToolRecoveryKind.TERMINAL,
                        meta={"runtime_guard": "NO_PROGRESS"},
                    )),),
                ))
            state.pending_tool_calls.clear()
            stop_body = json.dumps({
                "boundary": "runtime_no_progress_stop",
                "warning": (
                    "Do not call tools. Answer from existing evidence; "
                    "state what is unverified and do not claim completion."
                ),
                "step_id": decision.goal_slice.step_id,
                "next_action": pivot_decision.action.value,
                "reason": pivot_decision.reason,
                "consecutive_no_progress": (
                    decision.consecutive_no_progress
                ),
            }, sort_keys=True, separators=(",", ":"))
            state.messages.append(Message(
                f"plan-stop-{decision.action_signature[:16]}",
                MessageRole.USER, (TextBlock(stop_body),),
            ))
            state.checkpoint = replace(
                state.checkpoint, pending_tool_calls=(), messages=tuple(state.messages),
                tool_batch=(
                    state.checkpoint.tool_batch.with_pending(
                        (), status=ToolBatchStatus.CANCELLED
                    ) if state.checkpoint.tool_batch is not None else None
                ),
            )
            state.no_progress_stop = True
            await kernel._append_events(state.task_id, (("plan.no_progress_stopped", {
                "turn_id": state.turn_id,
                "step_id": decision.goal_slice.step_id,
                "action_signature": decision.action_signature,
                "consecutive_no_progress": (
                    decision.consecutive_no_progress
                ),
                "limit": state.plan_guard.no_progress_limit,
                "next_action": pivot_decision.action.value,
            }),))
            await kernel._save_agent_checkpoint(
                state.checkpoint, "plan-no-progress-stopped"
            )
            break
        next_tool_count = state.tool_call_count + 1
        await kernel._append_events(
            state.task_id,
            ((
                "tool.requested",
                {
                    "turn_id": state.turn_id,
                    "tool_call_number": next_tool_count,
                    "call": call.to_data(),
                },
            ),),
        )
        suspension = AgentTurnCheckpoint(
            task_id=state.task_id,
            turn_id=state.turn_id,
            revision=state.checkpoint.revision,
            messages=tuple(state.messages),
            pending_tool_calls=tuple(state.pending_tool_calls),
            tool_batch=(
                state.checkpoint.tool_batch.with_pending(
                    tuple(state.pending_tool_calls),
                    status=ToolBatchStatus.EXECUTING,
                ) if state.checkpoint.tool_batch is not None else None
            ),
            seen_call_ids=tuple(sorted(state.seen_call_ids)),
            model_calls=state.model_call_count,
            tool_calls=state.tool_call_count,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            max_model_calls=state.checkpoint.max_model_calls,
            max_tool_calls=state.checkpoint.max_tool_calls,
            max_output_tokens=state.checkpoint.max_output_tokens,
            tool_timeout_seconds=state.checkpoint.tool_timeout_seconds,
            model_budget_renewal_count=(
                state.checkpoint.model_budget_renewal_count
            ),
            model_budget_total_granted=(
                state.checkpoint.model_budget_total_granted
            ),
            workspace_fingerprint=state.checkpoint.workspace_fingerprint,
            effective_config_hash=state.checkpoint.effective_config_hash,
            toolset_hash=state.checkpoint.toolset_hash,
            prompt_manifest_hash=state.checkpoint.prompt_manifest_hash,
            session_id=state.checkpoint.session_id,
            session_context_hash=state.checkpoint.session_context_hash,
            working_memory_hash=state.checkpoint.working_memory_hash,
            last_steering_inbound_sequence=(
                state.checkpoint.last_steering_inbound_sequence
            ),
            goal_revision=state.checkpoint.goal_revision,
            action_progress=state.checkpoint.action_progress,
            evidence_inventory=state.checkpoint.evidence_inventory,
            read_hits_state=state.checkpoint.read_hits_state,
            artifact_read_state=state.checkpoint.artifact_read_state,
            progressive_scope_state=state.checkpoint.progressive_scope_state,
            exploration_budget_state=state.checkpoint.exploration_budget_state,
            stop_or_pivot_state=state.checkpoint.stop_or_pivot_state,
            evidence_relation_state=state.checkpoint.evidence_relation_state,
            rejection_loop_state=state.checkpoint.rejection_loop_state,
            exploration_outcome_state=state.checkpoint.exploration_outcome_state,
            finalization_state=state.checkpoint.finalization_state,
        )
        await kernel._save_agent_checkpoint(
            suspension, "before-tool-execution"
        )
        tool_started_at = time.monotonic()
        presentation = kernel._present_tool_arguments(
            call.name, call.arguments
        )
        kernel._notify_agent_progress(state.on_progress, AgentProgress(
            AgentProgressKind.TOOL_STARTED,
            model_call=state.model_call_count,
            max_model_calls=state.checkpoint.max_model_calls,
            tool_call=next_tool_count,
            max_tool_calls=state.checkpoint.max_tool_calls,
            tool_name=call.name,
            goal=task_before_action.goal,
            operation=call.name,
            operation_arguments=dict(presentation.visible_arguments),
            operation_presentation=presentation.text,
            operation_presentation_mode=presentation.mode,
            scope_target=kernel._live_scope_target(call),
            scope_change_reason="",
        ))
        try:
            result = await kernel._invoke_agent_tool(
                state.task_id, state.turn_id, call, effective_tool_timeout_seconds,
                agent_checkpoint=suspension.to_data(),
                recover_interrupted=state.recover_interrupted,
                allow_closed_outcome_ref=True,
            )
        except ApprovalRequired as required:
            request = required.request
            return AgentTurnSuspended(
                task_id=state.task_id,
                turn_id=state.turn_id,
                revision=state.checkpoint.revision,
                approval_request_id=request.request_id,
                payload_hash=request.payload_hash,
                action=request.action,
                target=request.target,
                preview=request.preview,
                risk=request.risk.value,
                network_access=request.network_access,
                data_transmission=request.data_transmission,
                rollback=request.rollback,
                approval_kind=request.kind.value,
            )
        except ClarificationRequired as required:
            request = required.request
            return AgentClarificationSuspended(
                task_id=state.task_id, turn_id=state.turn_id,
                revision=state.checkpoint.revision,
                request_id=request.request_id,
                question=request.question,
                choices=tuple(
                    (choice.value, choice.label)
                    for choice in request.choices
                ),
                reason=request.reason, required=request.required,
                resume_token=required.resume_token,
            )
        tool_elapsed_milliseconds = max(
            0, int((time.monotonic() - tool_started_at) * 1000)
        )
        evidence_inventory, evidence_delta = (
            await kernel._evaluate_evidence_delta(
                state.task_id, state.turn_id, call, result,
                state.checkpoint.evidence_inventory,
            )
        )
        if evidence_delta is not None:
            result = replace(
                result,
                meta={
                    **dict(result.meta),
                    "evidence_delta": {
                        "question_id": evidence_delta.question_id,
                        "has_progress": evidence_delta.has_progress,
                        "total_new": evidence_delta.total_new,
                        "counts": evidence_delta.counts,
                        "consecutive_zero_delta": (
                            evidence_delta.consecutive_zero_delta
                        ),
                    },
                },
            )
        read_hits_state = await kernel._update_read_hits_after(
            state.task_id, state.turn_id, call, result, semantic_action,
            state.checkpoint.read_hits_state,
        )
        artifact_read_state = await kernel._update_artifact_read_after(
            state.task_id, state.turn_id, call, result, artifact_probe,
            state.checkpoint.artifact_read_state,
        )
        progressive_scope_state = (
            await kernel._update_progressive_scope_after(
                state.task_id, state.turn_id, call, result, semantic_action,
                scope_probe, state.checkpoint.progressive_scope_state,
            )
        )
        evidence_relation_state = state.checkpoint.evidence_relation_state
        rejection_loop_state = state.checkpoint.rejection_loop_state
        exploration_outcome_state = state.checkpoint.exploration_outcome_state
        if kernel._exploration_coordinator is not None:
            exploration_update = await kernel._update_exploration_after(
                state.task_id, state.turn_id, call, result, semantic_action,
                evidence_delta, state.checkpoint.evidence_relation_state,
                state.checkpoint.rejection_loop_state,
                state.checkpoint.exploration_outcome_state,
            )
            evidence_relation_state = (
                exploration_update.relation_state.to_data()
            )
            rejection_loop_state = (
                exploration_update.rejection_state.to_data()
            )
            exploration_outcome_state = (
                exploration_update.outcome.state.to_data()
            )
            outcome = exploration_update.outcome
            result = replace(result, meta={
                **dict(result.meta),
                "exploration_outcome": {
                    "action": outcome.action.value,
                    "reason": outcome.reason,
                },
            })
            if outcome.action in {
                ExplorationOutcomeAction.STOP_ROUTE,
                ExplorationOutcomeAction.WRAP_UP,
                ExplorationOutcomeAction.ASK_USER,
            }:
                state.budget_wrap_up = True
        exploration_budget_state, budget_update = (
            await kernel._update_exploration_budget_after(
                state.task_id, state.turn_id, call, result, semantic_action,
                evidence_delta, ExplorationBudgetObservation(
                    tool_elapsed_milliseconds,
                    broad_search=(
                        bool(scope_probe.scope_hash)
                        and scope_probe.depth == 0
                    ),
                    scope_expanded=(scope_decision.relation == "expanded"),
                ), state.checkpoint.exploration_budget_state,
            )
        )
        await kernel._emit_agent_progress_projection(
            state.task_id, state.turn_id, call, semantic_action,
            scope_decision.relation, scope_probe.depth,
            evidence_delta, budget_update,
            (
                StopOrPivotAction.CHANGE_METHOD.value
                if budget_update is not None
                and budget_update.state.low_value_streak >= 2
                else StopOrPivotAction.CONTINUE.value
            ), "tool_result", state.on_progress,
            goal=task_before_action.goal,
            tool_calls_used=next_tool_count,
        )
        presentation = kernel._present_tool_arguments(
            call.name, call.arguments
        )
        kernel._notify_agent_progress(state.on_progress, AgentProgress(
            AgentProgressKind.TOOL_COMPLETED,
            model_call=state.model_call_count,
            max_model_calls=state.checkpoint.max_model_calls,
            tool_call=next_tool_count,
            max_tool_calls=state.checkpoint.max_tool_calls,
            tool_name=call.name,
            ok=result.ok,
            error_code=result.error_code,
            elapsed_seconds=tool_elapsed_milliseconds / 1000.0,
            evidence_delta=(
                evidence_delta.total_new
                if evidence_delta is not None else None
            ),
            consecutive_zero_delta=(
                evidence_delta.consecutive_zero_delta
                if evidence_delta is not None else 0
            ),
            goal=task_before_action.goal,
            operation=call.name,
            operation_arguments=dict(presentation.visible_arguments),
            operation_presentation=presentation.text,
            operation_presentation_mode=presentation.mode,
            scope_target=kernel._live_scope_target(call),
            scope_change_reason="",
        ))
        if not result.ok:
            state.last_tool_error = (
                f"{call.name}: {result.error_code or 'TOOL_ERROR'}"
            )
        else:
            # A command that exits non-zero returns ok=True: the call
            # ran, the effect failed. Surface it here so the model is
            # told the real outcome instead of a silent success.
            state.live_spec = state.visible_tool_by_name.get(call.name)
            classified = (
                classify_effect_status(
                    state.live_spec.effect, state.live_spec.result_authority,
                    invocation_state=ToolCommitState.COMMITTED,
                    ok=True, data=result.data,
                )
                if state.live_spec is not None else None
            )
            if (
                classified is not None
                and classified[0] is EffectStatus.FAILED
            ):
                state.last_tool_error = f"{call.name}: {classified[1]}"
        state.tool_call_count = next_tool_count
        state.messages.append(
            Message(
                message_id=f"msg-tool-{uuid4().hex}",
                role=MessageRole.TOOL,
                content=(ToolResultBlock(_govern_runtime_result(kernel, state, call, result)),),
            )
        )
        state.pending_tool_calls.pop(0)
        after_tool = replace(
            suspension, messages=tuple(state.messages),
            pending_tool_calls=tuple(state.pending_tool_calls),
            tool_batch=(
                suspension.tool_batch.with_pending(
                    tuple(state.pending_tool_calls)
                ) if suspension.tool_batch is not None else None
            ),
            tool_calls=state.tool_call_count,
            workspace_fingerprint=workspace_fingerprint(
                Path((await kernel.get_task(state.task_id)).workspace),
                kernel._dependencies.workspace_path,
            ),
            working_memory_hash=(
                await kernel.get_working_memory(state.task_id)
            ).content_hash,
            evidence_inventory=evidence_inventory,
            read_hits_state=read_hits_state,
            artifact_read_state=artifact_read_state,
            progressive_scope_state=progressive_scope_state,
            exploration_budget_state=exploration_budget_state,
            stop_or_pivot_state=state.checkpoint.stop_or_pivot_state,
            evidence_relation_state=evidence_relation_state,
            rejection_loop_state=rejection_loop_state,
            exploration_outcome_state=exploration_outcome_state,
            finalization_state=state.checkpoint.finalization_state,
            action_progress=(
                ActionProgressState(
                    decision.action_signature,
                    decision.relevant_state_hash,
                    evidence_delta.consecutive_zero_delta,
                    True,
                    decision.semantic_signature,
                    decision.semantic_family,
                ).to_data()
                if evidence_delta is not None
                else suspension.action_progress
            ),
        )
        await kernel._save_agent_checkpoint(after_tool, "tool-result-recorded")
        state.checkpoint = after_tool
        if result.error_code == "UNKNOWN_OUTCOME":
            return await kernel._suspend_unknown_tool_outcome(
                after_tool, call, result
            )
