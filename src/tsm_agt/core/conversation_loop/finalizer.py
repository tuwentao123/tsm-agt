"""Terminal boundaries for the conversation loop.

A turn ends in exactly one of three shapes:

* **text completion** -- the model answered with no tool calls.  The answer is
  archived atomically with the turn's ``llm.completed`` / ``turn.completed``
  facts, and no completion contract, evidence gate, or forced continuation runs.
* **partial boundary** -- the iteration budget ran out, or the runtime stopped
  inside the loop.  The checkpoint stays resumable and the boundary is recorded
  honestly instead of being reported as success.
* **suspension** -- approval or clarification interrupted the batch.  That
  boundary is owned by the shared tool-batch runtime and returned unchanged.

Only the first two shapes are finalized here.
"""

from __future__ import annotations

from ..kernel_process import PROCESS_COMPLETION_MESSAGE_PREFIX

from dataclasses import replace
from typing import Any

from tsm_agt.ports import ModelUsage

from ..agent_loop import AgentTurnIncomplete, AgentTurnResult, TurnState

#: Reasons recorded on a partial boundary. They are durable event payloads.
ITERATION_BUDGET_EXHAUSTED = "iteration_budget_exhausted"
TOOL_CALL_BUDGET_EXHAUSTED = "tool_call_budget_exhausted"


class ConversationFinalizer:
    """Write the conversation loop's terminal boundaries."""

    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel

    async def persist_text_completion(
        self, state: TurnState, response: Any, *,
        assistant_replay_eligible: bool,
    ) -> AgentTurnResult:
        """Archive a plain text answer as this turn's final result."""

        kernel = self._kernel
        user_message = kernel._source_user_message(state.checkpoint.messages)
        if user_message is None:
            raise RuntimeError("conversation loop lost its source user message")
        await kernel._commit_final_agent_result(
            task_id=state.task_id,
            turn_id=state.turn_id,
            user_message=user_message,
            assistant_message=response.message,
            assistant_replay_eligible=assistant_replay_eligible,
            model_context_window={
                **dict(state.checkpoint.context_window_state),
                "task_id": state.task_id,
                "messages": [message.to_data() for message in state.messages],
            },
            llm_payload={
                "turn_id": state.turn_id,
                "orchestration_mode": "CONVERSATION_LOOP",
                "loop_protocol": "conversation",
                "current_input_ref": f"message:{user_message.message_id}",
                "prompt_projection_hash": state.checkpoint.session_context_hash,
                "model_call": state.model_call_count,
                "message": response.message.to_data(),
                "finish_reason": response.finish_reason.value,
                "usage": response.usage.to_data(),
                "response_diagnostics": kernel._response_diagnostics(response),
                "context_window": state.prepared.budget.context_window,
                "context_estimated_input_tokens": (
                    state.prepared.budget.estimated_input_tokens
                ),
                "context_estimation_method": (
                    state.prepared.budget.estimation_method
                ),
                "context_budget": state.prepared.budget.event_data(),
                **state.prompt.receipt.event_data(),
            },
            additional_events=(
                (
                    "conversation.text_completed",
                    {
                        "turn_id": state.turn_id,
                        "orchestration_mode": "CONVERSATION_LOOP",
                        "assistant_message_id": response.message.message_id,
                        "model_calls": state.model_call_count,
                        "tool_calls": state.tool_call_count,
                    },
                ),
            ),
        )
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

    async def persist_partial_boundary(
        self, state: TurnState, reason: str, *, budget: dict[str, int] | None = None,
    ) -> AgentTurnIncomplete:
        """Record a resumable, explicitly-partial boundary for this turn."""

        kernel = self._kernel
        state.checkpoint = replace(
            state.checkpoint,
            messages=tuple(state.messages),
            pending_tool_calls=tuple(state.pending_tool_calls),
            seen_call_ids=tuple(sorted(state.seen_call_ids)),
            model_calls=state.model_call_count,
            tool_calls=state.tool_call_count,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            finalization_state={
                "review_continuations": state.review_continuations,
                "runtime_continuations": state.runtime_continuations,
                "edit_recovery_continuations": state.edit_recovery_continuations,
                "reviewer_call_count": state.reviewer_call_count,
                "last_candidate_hash": state.last_candidate_hash,
                "last_review_reason_code": state.last_review_reason_code,
            },
        )
        await kernel._save_agent_checkpoint(
            state.checkpoint, f"conversation-{reason}"
        )
        await kernel._append_events(state.task_id, (
            (
                "conversation.partial_boundary",
                {
                    "turn_id": state.turn_id,
                    "orchestration_mode": "CONVERSATION_LOOP",
                    "reason": reason,
                    "model_calls": state.model_call_count,
                    "tool_calls": state.tool_call_count,
                    "pending_tool_calls": len(state.pending_tool_calls),
                    **(budget or {}),
                },
            ),
            (
                "turn.incomplete_boundary",
                {
                    "turn_id": state.turn_id,
                    "orchestration_mode": "CONVERSATION_LOOP",
                    "reason": reason,
                    "model_calls": state.model_call_count,
                    "tool_calls": state.tool_call_count,
                    **(budget or {}),
                },
            ),
        ))
        return AgentTurnIncomplete(
            task_id=state.task_id,
            turn_id=state.turn_id,
            revision=state.checkpoint.revision,
            reason=reason,
            gaps=(),
        )

    async def record_model_response(
        self, state: TurnState, response: Any, tool_call_count: int, *, elapsed_seconds: float,
    ) -> None:
        """Audit one model exchange without persisting its body twice."""

        events = ((
            "conversation.model_response_recorded",
            {
                "turn_id": state.turn_id,
                "orchestration_mode": "CONVERSATION_LOOP",
                "model_call": state.model_call_count,
                "assistant_message_id": response.message.message_id,
                "finish_reason": response.finish_reason.value,
                "tool_call_count": tool_call_count,
                "usage": response.usage.to_data(),
                "elapsed_seconds": elapsed_seconds,
            },
        ),)
        process_ids = [
            message.message_id.removeprefix(PROCESS_COMPLETION_MESSAGE_PREFIX)
            for message in state.request.messages
            if message.message_id.startswith(PROCESS_COMPLETION_MESSAGE_PREFIX)
        ]
        if process_ids:
            delivered = await self._kernel._dependencies.store.read_events(
                state.task_id, event_types=("process.completions_delivered",),
            )
            seen = {pid for event in delivered for pid in event.payload["process_ids"]}
            new_ids = [pid for pid in process_ids if pid not in seen]
            if new_ids:
                events += (("process.completions_delivered", {
                    "turn_id": state.turn_id, "process_ids": new_ids,
                }),)
        await self._kernel._append_events(state.task_id, events)

    async def record_tool_result(
        self, state: TurnState, result: Any,
    ) -> None:
        """Audit one durable ToolResult after the shared executor persisted it."""

        await self._kernel._append_events(state.task_id, ((
            "conversation.tool_result_recorded",
            {
                "turn_id": state.turn_id,
                "orchestration_mode": "CONVERSATION_LOOP",
                "tool_call_id": result.call_id,
                "ok": bool(result.ok),
                "error_code": result.error_code,
                "retryable": bool(getattr(result, "retryable", False)),
                "truncated": bool(getattr(result, "truncated", False)),
            },
        ),))

    async def record_interrupted(self, state: TurnState, reason: str) -> None:
        await self._kernel._append_events(state.task_id, ((
            "conversation.interrupted",
            {
                "turn_id": state.turn_id,
                "orchestration_mode": "CONVERSATION_LOOP",
                "reason": reason,
                "model_calls": state.model_call_count,
                "tool_calls": state.tool_call_count,
            },
        ),))
