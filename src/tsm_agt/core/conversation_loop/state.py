"""Turn-local state shared by the conversation loop's phases.

The loop threads one :class:`~tsm_agt.core.agent_loop.TurnState` through every
iteration.  This module owns the one place that projects a durable checkpoint
into that state, so the loop body and the resume path cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tsm_agt.ports import Message, ToolSpec

from ..agent_loop import AgentProgress, AgentTurnCheckpoint, TurnState
from ..plan_guard import PlanGuard


def build_turn_state(
    *,
    checkpoint: AgentTurnCheckpoint,
    visible_tools: tuple[ToolSpec, ...],
    kernel: Any,
    initial_tool_message: Message | None = None,
    recover_interrupted: bool = False,
    on_text_delta: Callable[[str], None] | None = None,
    on_progress: Callable[[AgentProgress], None] | None = None,
) -> TurnState:
    """Project one durable checkpoint into the loop's working state.

    Only facts the checkpoint does not carry are resolved here: the current
    toolset, the visible-tool index, and the live callbacks.
    """

    state = TurnState(
        task_id=checkpoint.task_id,
        turn_id=checkpoint.turn_id,
        visible_tools=visible_tools,
        initial_tool_message=initial_tool_message,
        recover_interrupted=recover_interrupted,
        on_text_delta=on_text_delta,
        on_progress=on_progress,
        checkpoint=kernel._synchronize_tool_batch(checkpoint),
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
        review_continuations=int(
            checkpoint.finalization_state.get("review_continuations", 0)
        ),
        reviewer_call_count=int(
            checkpoint.finalization_state.get("reviewer_call_count", 0)
        ),
        last_candidate_hash=str(
            checkpoint.finalization_state.get("last_candidate_hash", "")
        ),
        last_review_reason_code=str(
            checkpoint.finalization_state.get("last_review_reason_code", "")
        ),
    )
    if initial_tool_message is not None:
        state.messages.append(initial_tool_message)
    state.pending_tool_calls = list(state.checkpoint.pending_tool_calls)
    return state
