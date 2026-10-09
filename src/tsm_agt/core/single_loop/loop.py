"""Minimal executor for Tasks fixed to ``SINGLE_LOOP`` orchestration.

This module intentionally owns the single-loop control flow instead of routing
back through :meth:`Kernel.run_agent_turn`.  The legacy runner is planner-led;
calling it from here would both re-enter mode dispatch and invoke legacy task
planning for a Task whose execution identity is already fixed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import uuid4

from tsm_agt.ports import (
    ConclusionProtocolMode,
    FinishReason,
    Message,
    MessageBlock,
    MessageRole,
    ModelRequest,
    ModelUsage,
    OutcomeBindingMode,
    RuntimeEvent,
    RuntimeUnitOfWork,
    TextBlock,
    ToolCall,
    ToolSpec,
    RecoverableToolProtocolError,
)
from tsm_agt.ports.evidence import GateResolution

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
)
from ..approval import ApprovalNotPending
from ..clarification import ClarificationNotPending
from ..context import ContextWindowExceeded
from ..evidence_question import EvidenceQuestionProjector
from ..plan_guard import PlanGuard
from ..task import TaskSnapshot, TaskState
from ..task_contract import TaskContractProjector
from ..turn import InvalidTurnState, ModelInvocationFailed
from ..working_memory import EffectiveWorkingMemory
from .context_projection import ActiveContextProjector
from .completion_shadow import CompletionShadowValidation, validate_completion_shadow
from .contracts import (
    CompletionValidation,
    required_capability_ids,
    required_effects,
    validate_completion,
)


class SingleAgentLoop:
    """Drive one Task without legacy planning or session input resolution."""

    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel

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
        task = await self._kernel.get_task(task_id)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        visible_tools = await self._kernel.list_tools()
        capabilities = self._kernel._dependencies.model.capabilities
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
        await self._kernel._append_events(task_id, (("turn.started", {
            "turn_id": turn_id,
            "orchestration_mode": "SINGLE_LOOP",
            "current_input_ref": f"message:{user_message.message_id}",
            "input_role": user_message.role.value,
            "input_character_count": len(user_text),
            "max_model_calls": max_model_calls,
            "max_tool_calls": max_tool_calls,
            "max_output_tokens": max_output_tokens,
            "visible_tools": [tool.name for tool in visible_tools],
            "provider_capabilities": capabilities.to_data(),
        }),))
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
        )
        checkpoint = await self._kernel._bind_agent_checkpoint(
            task, checkpoint, visible_tools
        )
        await self._kernel._save_agent_checkpoint(checkpoint, "single-loop-started")
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
        """Resume an interrupted single-loop checkpoint without new authority."""
        await self._kernel._close_cancellation_scope(task_id)
        await self._kernel.recover_workspace_transactions(task_id)
        await self._kernel.reconcile_background_processes(task_id)
        stored = await self._kernel._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        if task.state is TaskState.AWAITING_APPROVAL:
            raise ApprovalNotPending(
                "task is waiting for explicit approve/reject; resume cannot grant approval"
            )
        if task.state is TaskState.AWAITING_USER:
            raise ClarificationNotPending(
                "task is waiting for an answer; resume requires request_id and token"
            )
        if task.state.value not in {
            "EXECUTING", "INTERRUPTED", "CONFLICT",
        }:
            raise InvalidTurnState(
                f"task {task_id} cannot resume from {task.state.value}"
            )
        if task.active_agent_checkpoint is None:
            raise LookupError(f"task has no active Agent checkpoint: {task_id}")
        checkpoint = AgentTurnCheckpoint.from_data(task.active_agent_checkpoint)
        visible_tools = await self._kernel.list_tools()
        decision = await self._kernel._evaluate_checkpoint_compatibility(
            task, checkpoint, visible_tools
        )
        if (
            task.state is TaskState.INTERRUPTED
            and decision.action.value == "EXACT_RESUME"
        ):
            checkpoint = await self._kernel._reconcile_agent_checkpoint(
                task, checkpoint, visible_tools, decision
            )
            task = await self._kernel.get_task(task_id)
        elif decision.action.value == "RECONCILE_REQUIRED":
            checkpoint = await self._kernel._reconcile_agent_checkpoint(
                task, checkpoint, visible_tools, decision
            )
            task = await self._kernel.get_task(task_id)
        elif decision.action.value == "REBASE_REQUIRED":
            checkpoint = await self._kernel._rebase_agent_checkpoint(
                task, checkpoint, visible_tools, decision
            )
            task = await self._kernel.get_task(task_id)
        elif decision.action.value != "EXACT_RESUME":
            await self._kernel._mark_checkpoint_conflict(
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
            stored = await self._kernel._require_stored_task(task_id)
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
            await self._kernel._dependencies.store.commit(RuntimeUnitOfWork(
                task_id, stored.version, executing.to_data(), events
            ))
        await self._kernel._append_events(task_id, (("turn.resumed", {
            "turn_id": checkpoint.turn_id,
            "previous_revision": checkpoint.revision,
            "revision": checkpoint.revision + 1,
            "checkpoint_hash": checkpoint.checkpoint_hash,
            "reason": "process restart or explicit resume",
            "orchestration_mode": "SINGLE_LOOP",
        }),))
        resumed = replace(checkpoint, revision=checkpoint.revision + 1)
        await self._kernel._save_agent_checkpoint(resumed, "single-loop-resumed")
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
        state = TurnState(
            task_id=checkpoint.task_id,
            turn_id=checkpoint.turn_id,
            visible_tools=visible_tools,
            initial_tool_message=initial_tool_message,
            recover_interrupted=recover_interrupted,
            on_text_delta=on_text_delta,
            on_progress=on_progress,
            checkpoint=self._kernel._synchronize_tool_batch(checkpoint),
            visible_tool_by_name={tool.name: tool for tool in visible_tools},
            messages=list(checkpoint.messages),
            pending_tool_calls=list(checkpoint.pending_tool_calls),
            seen_call_ids=set(checkpoint.seen_call_ids),
            model_call_count=checkpoint.model_calls,
            tool_call_count=checkpoint.tool_calls,
            input_tokens=checkpoint.input_tokens,
            output_tokens=checkpoint.output_tokens,
            closed_outcomes_at_entry=frozenset(),
            plan_guard=PlanGuard(),
        )
        if initial_tool_message is not None:
            state.messages.append(initial_tool_message)
        state.pending_tool_calls = list(state.checkpoint.pending_tool_calls)
        while True:
            if state.pending_tool_calls:
                outcome = await self._kernel._run_tool_batch(state)
                if outcome is not None:
                    return outcome
            await self._prepare_model_call(state)
            buffered_text_deltas: list[str] = []
            self._kernel._notify_agent_progress(state.on_progress, AgentProgress(
                AgentProgressKind.MODEL_STARTED,
                model_call=state.model_call_number,
                max_model_calls=state.checkpoint.max_model_calls,
                tool_call=state.tool_call_count,
                max_tool_calls=state.checkpoint.max_tool_calls,
                goal=(await self._kernel.get_task(state.task_id)).goal,
            ))
            response = None
            try:
                response = await self._kernel._complete_agent_model_request(
                    state.request, buffered_text_deltas.append
                )
                self._kernel._notify_agent_progress(state.on_progress, AgentProgress(
                    AgentProgressKind.MODEL_COMPLETED,
                    model_call=state.model_call_number,
                    max_model_calls=state.checkpoint.max_model_calls,
                    tool_call=state.tool_call_count,
                    max_tool_calls=state.checkpoint.max_tool_calls,
                    elapsed_seconds=time.monotonic() - state.model_started_at,
                    goal=(await self._kernel.get_task(state.task_id)).goal,
                ))
                calls = self._kernel._validate_agent_response(
                    response.message,
                    response.finish_reason,
                    evidence_required_tools=frozenset(
                        tool.name for tool in visible_tools
                        if self._kernel._dependencies.require_evidence_questions
                        and tool.requires_evidence_question
                    ),
                )
                duplicate = [
                    call.call_id for call in calls if call.call_id in state.seen_call_ids
                ]
                if duplicate:
                    raise ModelInvocationFailed(
                        state.turn_id,
                        "tool call_id must be unique within a turn: " + ", ".join(duplicate),
                    )
                calls = tuple(
                    replace(
                        call,
                        outcome_ref=None,
                        outcome_binding_mode=OutcomeBindingMode.FULFILLMENT,
                    )
                    for call in calls
                )
                self._kernel._validate_tool_batch_proposal(
                    calls, state.visible_tool_by_name, state.seen_call_ids
                )
            except RecoverableToolProtocolError as error:
                state.model_call_count += 1
                if response is not None:
                    state.input_tokens += response.usage.input_tokens
                    state.output_tokens += response.usage.output_tokens
                state.messages.append(self._tool_protocol_correction(error))
                state.checkpoint = replace(
                    state.checkpoint,
                    messages=tuple(state.messages),
                    model_calls=state.model_call_count,
                    input_tokens=state.input_tokens,
                    output_tokens=state.output_tokens,
                )
                await self._kernel._append_events(state.task_id, ((
                    "tool.protocol_rejected",
                    {
                        "turn_id": state.turn_id,
                        "reason_code": error.reason_code,
                        "detail": error.detail,
                    },
                ),))
                await self._kernel._save_agent_checkpoint(
                    state.checkpoint, "single-loop-tool-protocol-correction"
                )
                continue
            if state.tool_call_count + len(calls) > checkpoint.max_tool_calls:
                raise AgentLoopLimitExceeded(
                    state.turn_id, "max_tool_calls", checkpoint.max_tool_calls,
                    model_calls=state.model_call_count,
                    tool_calls=state.tool_call_count,
                    max_model_calls=checkpoint.max_model_calls,
                    max_tool_calls=checkpoint.max_tool_calls,
                )
            state.seen_call_ids.update(call.call_id for call in calls)
            state.model_call_count += 1
            state.input_tokens += response.usage.input_tokens
            state.output_tokens += response.usage.output_tokens
            state.messages.append(response.message)
            accepted_batch = (
                await self._kernel._new_tool_batch(
                    state.task_id, response.message, calls
                )
                if calls else None
            )
            state.checkpoint = replace(
                state.checkpoint,
                messages=tuple(state.messages),
                pending_tool_calls=calls,
                tool_batch=accepted_batch,
                seen_call_ids=tuple(sorted(state.seen_call_ids)),
                model_calls=state.model_call_count,
                tool_calls=state.tool_call_count,
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
            )
            if not calls:
                completion = await self._completion_validation(state, response.message)
                shadow = await self._completion_shadow_validation(state, completion)
                await self._record_completion_shadow(state, shadow)
                if completion.complete:
                    if shadow.gaps and not self._has_contract_feedback(state):
                        state.messages.append(self._contract_feedback_continuation(shadow))
                        state.checkpoint = replace(
                            state.checkpoint, messages=tuple(state.messages)
                        )
                        await self._kernel._save_agent_checkpoint(
                            state.checkpoint, "single-loop-contract-feedback"
                        )
                        continue
                    await self._commit_final_response(state, response, completion)
                    if state.on_text_delta is not None:
                        for text in buffered_text_deltas:
                            state.on_text_delta(text)
                    return AgentTurnResult(
                        turn_id=state.turn_id,
                        task_id=state.task_id,
                        assistant_message=response.message,
                        finish_reason=response.finish_reason,
                        usage=ModelUsage(state.input_tokens, state.output_tokens),
                        model_calls=state.model_call_count,
                        tool_calls=state.tool_call_count,
                        messages=tuple(state.messages),
                    )
                if not self._can_recover_completion(state, completion):
                    if completion.gate_decision.resolution is GateResolution.BLOCKED:
                        return await self._blocked_boundary(state, completion)
                    return await self._incomplete_boundary(state, completion)
                state.messages.append(self._completion_continuation(completion))
                state.checkpoint = replace(state.checkpoint, messages=tuple(state.messages))
                await self._kernel._save_agent_checkpoint(
                    state.checkpoint, "single-loop-completion-continuation"
                )
                continue
            await self._kernel._save_agent_checkpoint(
                state.checkpoint, "single-loop-model-response"
            )
            state.pending_tool_calls = list(calls)

    async def _prepare_model_call(self, state: TurnState) -> None:
        state.messages = list(self._kernel._normalize_and_validate_model_boundary(
            tuple(state.messages)
        ))
        model_call = state.model_call_count + 1
        if model_call > state.checkpoint.max_model_calls:
            raise AgentLoopLimitExceeded(
                state.turn_id, "max_model_calls", state.checkpoint.max_model_calls,
                model_calls=state.model_call_count,
                tool_calls=state.tool_call_count,
                max_model_calls=state.checkpoint.max_model_calls,
                max_tool_calls=state.checkpoint.max_tool_calls,
            )
        generated_prefixes = (
            "project-instructions-context-", "task-spec-context-",
            "project-onboarding-context-", "project-memory-context-",
            "session-context-", "document-references-context-",
            "working-memory-context-", "task-contract-context-",
        )
        state.messages = [
            message for message in state.messages
            if not message.message_id.startswith(generated_prefixes)
        ]
        state.messages.extend(await self._kernel._project_context_messages(state.task_id))
        state.checkpoint = replace(state.checkpoint, messages=tuple(state.messages))
        task = await self._kernel.get_task(state.task_id)
        events = await self._kernel._dependencies.store.read_events(state.task_id)
        working_memory = self._kernel._dependencies.working_memory_projector.project(
            state.task_id, task.goal, events
        )
        evidence_questions = EvidenceQuestionProjector.project(state.task_id, events)
        effective_memory = self._kernel._dependencies.effective_working_memory_projector.project(
            working_memory, evidence_questions
        )
        session_prompt = await self._kernel.get_session_prompt_projection(
            task.session_id, exclude_task_id=state.task_id
        )
        shadow = ActiveContextProjector.project(
            task,
            state.checkpoint,
            effective_memory,
            evidence_questions,
            session_prompt,
            events,
            model_call=model_call,
        )
        await self._kernel._append_events(state.task_id, ((
            "context.single_loop_shadow_projected", shadow.event_payload()
        ),))
        model_tools = tuple(
            tool for tool in state.visible_tools
            if tool.name != "core.task_outcome_complete"
        )
        try:
            prepared = self._kernel._dependencies.context_manager.prepare(
                conversation=tuple(state.messages),
                tools=model_tools,
                prompt_template=self._kernel._dependencies.prompt_template,
                context_window=self._kernel._dependencies.model.capabilities.context_window,
                max_output_tokens=state.checkpoint.max_output_tokens,
                runtime_instruction="",
            )
        except ContextWindowExceeded as error:
            await self._kernel._record_turn_failure(state.task_id, state.turn_id, error)
            raise ModelInvocationFailed(state.turn_id, str(error)) from error
        if prepared.compaction is not None:
            state.messages = list(prepared.messages)
            await self._kernel._append_events(state.task_id, (("context.compacted", {
                "turn_id": state.turn_id,
                "model_call": model_call,
                **prepared.compaction.event_data(),
            }),))
            await self._kernel._append_events(state.task_id, ((
                "context.single_loop_shadow_compacted",
                shadow.compaction_event_payload(prepared.compaction.event_data()),
            ),))
            state.checkpoint = replace(state.checkpoint, messages=tuple(state.messages))
            await self._kernel._save_agent_checkpoint(
                state.checkpoint, "single-loop-context-compacted"
            )
        prompt = self._kernel._dependencies.prompt_template.assemble(
            prepared.messages, model_tools, ""
        )
        state.request = ModelRequest(
            turn_id=state.turn_id,
            messages=prompt.messages,
            max_output_tokens=state.checkpoint.max_output_tokens,
            tools=model_tools,
            allow_tool_calls=True,
            require_evidence_questions=self._kernel._dependencies.require_evidence_questions,
            conclusion_protocol_mode=(
                ConclusionProtocolMode.REQUIRE_STRUCTURED
                if self._kernel._dependencies.model.capabilities.structured_conclusion
                else ConclusionProtocolMode.OBSERVE
            ),
            cancellation_scope=self._kernel._cancellation_scope(state.task_id),
        )
        state.prepared = prepared
        state.prompt = prompt
        state.model_call_number = model_call
        state.model_started_at = time.monotonic()

    async def _completion_validation(
        self, state: TurnState, assistant_message: Message,
    ) -> CompletionValidation:
        task = await self._kernel.get_task(state.task_id)
        events = await self._kernel._dependencies.store.read_events(state.task_id)
        metadata = await self._kernel.get_task_orchestration_metadata(state.task_id)
        return validate_completion(
            task=task,
            metadata=metadata,
            events=events,
            turn_id=state.turn_id,
            assistant_message=assistant_message,
            visible_tools=state.visible_tools,
            evidence_gate=self._kernel._dependencies.evidence_gate,
        )

    async def _completion_shadow_validation(
        self,
        state: TurnState,
        completion: CompletionValidation,
    ) -> CompletionShadowValidation:
        """Build rollout diagnostics without affecting completion control flow."""
        task = await self._kernel.get_task(state.task_id)
        events = await self._kernel._dependencies.store.read_events(state.task_id)
        metadata = await self._kernel.get_task_orchestration_metadata(state.task_id)
        return validate_completion_shadow(
            task=task,
            metadata=metadata,
            events=events,
            turn_id=state.turn_id,
            completion=completion,
        )

    def _can_recover_completion(
        self, state: TurnState, completion: CompletionValidation,
    ) -> bool:
        if state.model_call_count >= state.checkpoint.max_model_calls:
            return False
        if completion.gate_decision.resolution is GateResolution.BLOCKED:
            return False
        pending_capabilities = required_capability_ids(completion.gaps)
        if pending_capabilities:
            available_capabilities = frozenset(
                capability
                for tool in state.visible_tools
                for capability in tool.evidence_capability_ids
            )
            return pending_capabilities.issubset(available_capabilities)
        pending_effects = required_effects(completion.gaps)
        available_effects = frozenset(tool.effect for tool in state.visible_tools)
        return pending_effects.issubset(available_effects)

    async def _record_completion_shadow(
        self,
        state: TurnState,
        shadow: CompletionShadowValidation,
    ) -> None:
        await self._kernel._append_events(state.task_id, ((
            "completion.shadow_validated", shadow.to_data()
        ),))

    @staticmethod
    def _has_contract_feedback(state: TurnState) -> bool:
        return any(
            message.message_id.startswith("contract-feedback-")
            for message in state.messages
        )

    @staticmethod
    def _contract_feedback_continuation(
        shadow: CompletionShadowValidation,
    ) -> Message:
        details = {
            "type": "contract_feedback",
            "diagnostic_only": True,
            "gaps": [gap.to_data() for gap in shadow.gaps],
            "instruction": (
                "Review these diagnostic contract observations before replying. "
                "Use tools only when further work is needed; a final response is "
                "allowed after this feedback."
            ),
        }
        return Message(
            message_id=f"contract-feedback-{uuid4().hex}",
            role=MessageRole.USER,
            content=(TextBlock(json.dumps(
                details, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )),),
        )

    @staticmethod
    def _tool_protocol_correction(error: RecoverableToolProtocolError) -> Message:
        details = {
            "type": "tool_failure",
            "error_code": error.reason_code,
            "detail": error.detail,
            "instruction": "Correct the tool name and arguments using the visible tool schemas; no call was executed.",
        }
        return Message(
            message_id=f"tool-protocol-correction-{uuid4().hex}",
            role=MessageRole.USER,
            content=(TextBlock(json.dumps(
                details, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )),),
        )

    @staticmethod
    def _completion_continuation(completion: CompletionValidation) -> Message:
        details = {
            "type": "completion_continuation",
            "gaps": [gap.to_data() for gap in completion.gaps],
            "gate_decision": completion.gate_decision.to_data(),
            "instruction": "Resolve every durable completion gap with tools before replying.",
        }
        return Message(
            message_id=f"completion-continuation-{uuid4().hex}",
            role=MessageRole.USER,
            content=(TextBlock(str(details)),),
        )

    async def _blocked_boundary(
        self, state: TurnState, completion: CompletionValidation,
    ) -> AgentClarificationSuspended:
        """Turn an unsatisfiable active Contract into the existing user wait."""
        contract = TaskContractProjector.project(
            await self._kernel._dependencies.store.read_events(state.task_id)
        )
        if contract is None:
            raise RuntimeError("blocked completion has no projected contract")
        state.checkpoint = replace(state.checkpoint, messages=tuple(state.messages))
        request, token = await self._kernel.suspend_for_contract_gate(
            state.task_id,
            state.turn_id,
            state.checkpoint.to_data(),
            contract,
            completion.gate_decision,
        )
        return AgentClarificationSuspended(
            task_id=state.task_id,
            turn_id=state.turn_id,
            revision=state.checkpoint.revision,
            request_id=request.request_id,
            question=request.question,
            choices=(),
            reason=request.reason,
            required=True,
            resume_token=token,
            kind=request.kind.value,
        )

    async def _incomplete_boundary(
        self, state: TurnState, completion: CompletionValidation,
    ) -> AgentTurnIncomplete:
        state.checkpoint = replace(state.checkpoint, messages=tuple(state.messages))
        await self._kernel._save_agent_checkpoint(
            state.checkpoint, "single-loop-incomplete-boundary"
        )
        await self._kernel._append_events(state.task_id, ((
            "turn.incomplete_boundary",
            {
                "turn_id": state.turn_id,
                "orchestration_mode": "SINGLE_LOOP",
                "reason": "completion_gaps_unrecoverable",
                "model_calls": state.model_call_count,
                "tool_calls": state.tool_call_count,
                "completion": completion.to_data(),
            },
        ),))
        return AgentTurnIncomplete(
            task_id=state.task_id,
            turn_id=state.turn_id,
            revision=state.checkpoint.revision,
            reason="completion_gaps_unrecoverable",
            gaps=tuple(gap.to_data() for gap in completion.gaps),
        )

    async def _commit_final_response(
        self,
        state: TurnState,
        response: Any,
        completion: CompletionValidation,
    ) -> None:
        user_message = self._kernel._source_user_message(state.checkpoint.messages)
        if user_message is None:
            raise RuntimeError("single loop lost its source user message")
        await self._kernel._commit_final_agent_result(
            task_id=state.task_id,
            turn_id=state.turn_id,
            user_message=user_message,
            assistant_message=response.message,
            llm_payload={
                "turn_id": state.turn_id,
                "orchestration_mode": "SINGLE_LOOP",
                "current_input_ref": f"message:{user_message.message_id}",
                "prompt_projection_hash": state.checkpoint.session_context_hash,
                "model_call": state.model_call_count,
                "message": response.message.to_data(),
                "finish_reason": response.finish_reason.value,
                "usage": response.usage.to_data(),
                "response_diagnostics": self._kernel._response_diagnostics(response),
                "context_window": state.prepared.budget.context_window,
                "context_estimated_input_tokens": state.prepared.budget.estimated_input_tokens,
                "context_estimation_method": state.prepared.budget.estimation_method,
                "context_budget": state.prepared.budget.event_data(),
                **state.prompt.receipt.event_data(),
            },
            additional_events=(
                (
                    "completion.claim_validated",
                    {
                        "turn_id": state.turn_id,
                        "orchestration_mode": "SINGLE_LOOP",
                        "completion": completion.to_data(),
                    },
                ),
            ),
        )
