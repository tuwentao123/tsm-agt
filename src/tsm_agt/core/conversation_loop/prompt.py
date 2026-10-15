"""Stable prompt assembly for the conversation loop.

The loop owns no prompt of its own: it composes the same protected template the
other orchestration paths use and lets that template filter its capability
guidance against the tools actually advertised this turn.  What this module adds
is the one *dynamic* instruction the runtime may hand the model: a facts-only
note about the latest durable tool result.

That note is deliberately not a gate.  It may name an ``error_code``,
``retryable`` flag, or ``recovery_action`` that already exists in the ledger; it
may never require a tool the Runtime did not expose, and it may never block a
plain text answer.
"""

from __future__ import annotations

import json
from typing import Any

from tsm_agt.ports import Message, ToolSpec

from ..agent_loop import TurnState
from ..prompt import PromptAssembly, PromptTemplate

def latest_tool_result_instruction(
    kernel: Any, state: TurnState, model_tools: tuple[ToolSpec, ...] = (),
) -> str:
    """Read the newest durable tool outcome and state only what it reports.

    Returns an empty string when the last tool exchange was clean, so the stable
    prompt is left untouched on the common path.
    """

    write_tools = [tool.name for tool in model_tools if tool.effect.value == "mutate"]
    availability = ""
    if write_tools:
        availability = (
            "Runtime fact: this model call has workspace write tools available: "
            + ", ".join(write_tools)
            + ". A tool being available does not itself grant approval; Runtime "
            "decides that when called. Not calling one does not mean it is absent "
            "or that write permission was denied."
        )
    recovery_batch = kernel._latest_recoverable_tool_batch(state.messages)
    if recovery_batch:
        recovery = recovery_batch[-1]
        action = dict(recovery.recovery_action)
        recovery_instruction = (
            "The latest tool result reports a failure that may still be "
            f"recoverable (kind {recovery.effective_recovery_kind.value}). "
            f"Its structured recovery facts are: "
            f"{json.dumps(action, ensure_ascii=False, sort_keys=True)}. "
            "Choose a legal next step within the advertised tools and continue "
            "the original user request. If no legal recovery remains, report the "
            "blocker with those facts and do not claim the work was completed."
        )
        return "\n\n".join(part for part in (availability, recovery_instruction) if part)
    if state.last_tool_error:
        error_instruction = (
            "The latest durable tool result is a failure "
            f"({state.last_tool_error}). Its details are authoritative; retry "
            "with corrected facts only when that is safe, otherwise change "
            "method or report the blocker."
        )
        return "\n\n".join(part for part in (availability, error_instruction) if part)
    return availability


def assemble_stable_prompt(
    template: PromptTemplate,
    messages: tuple[Message, ...],
    tools: tuple[ToolSpec, ...],
    runtime_instruction: str,
) -> PromptAssembly:
    """Assemble the protected system prompt plus this turn's conversation."""

    return template.assemble(messages, tools, runtime_instruction)
