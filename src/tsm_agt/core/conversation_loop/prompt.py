"""Stable prompt assembly for the conversation loop.

The loop owns no prompt of its own: it composes the same protected template the
other orchestration paths use and lets that template filter its capability
guidance against the tools actually advertised this turn.  What this module adds
is the one *dynamic* instruction the runtime may hand the model: a compact,
facts-only note about action and verification results.

That note is deliberately not a gate. It may describe an error or observed
outcome but does not expose internal retry classifications or pick a repair.
The bounded finalization policy is a separate protocol rule; this prompt note
itself never blocks a plain-text answer.
"""

from __future__ import annotations

import json
from typing import Any

from tsm_agt.ports import Message, ToolSpec

from ..agent_loop import TurnState
from ..prompt import PromptAssembly, PromptTemplate
from .action_facts import (
    latest_failed_delivery_action, post_mutation_verification_fact,
    project_runtime_action_facts,
)

def latest_tool_result_instruction(
    kernel: Any, state: TurnState, model_tools: tuple[ToolSpec, ...] = (),
) -> str:
    """Read the newest durable tool outcome and state only what it reports.

    Returns an empty string when the last tool exchange was clean, so the stable
    prompt is left untouched on the common path.
    """

    # Keep the stable prompt and advertised tool definitions cacheable. This
    # dynamic note contains only the compact result facts that changed.
    # Use the turn's full advertised tool registry: a tool may have produced a
    # durable result on an earlier iteration but be outside a narrower profile
    # selected for the *next* model call. Its effect metadata must not vanish.
    tool_specs = state.visible_tool_by_name
    if not isinstance(tool_specs, dict):
        tool_specs = {tool.name: tool for tool in model_tools}
    facts = project_runtime_action_facts(state.messages, tool_specs)
    latest_failure = latest_failed_delivery_action(facts)
    post_mutation = post_mutation_verification_fact(facts)
    fact_parts: list[str] = []
    if latest_failure is not None:
        fact_parts.append(
            "Runtime action facts: " + json.dumps(
                latest_failure.prompt_data(), ensure_ascii=False, sort_keys=True,
            ) + ". This action does not establish completion of the original "
            "request. Choose a legal recovery, a different legal method, or an "
            "evidence-based blocker; do not claim the action succeeded."
        )
    post_mutation_data = post_mutation.prompt_data()
    if post_mutation_data is not None and post_mutation.status in {
        "missing", "failed", "unknown",
    }:
        fact_parts.append(
            "Runtime post-mutation facts: " + json.dumps(
                post_mutation_data, ensure_ascii=False, sort_keys=True,
            ) + ". These facts do not select a verifier or recovery for you. "
            "Decide whether an available verification is needed; do not describe "
            "the change as verified without matching evidence."
        )
    if state.last_tool_error:
        error_instruction = (
            "The latest durable tool result is a failure "
            f"({state.last_tool_error}). Consult the tool result for details; "
            "do not claim the failed action succeeded."
        )
        return "\n\n".join((*fact_parts, error_instruction))
    return "\n\n".join(fact_parts)


def assemble_stable_prompt(
    template: PromptTemplate,
    messages: tuple[Message, ...],
    tools: tuple[ToolSpec, ...],
    runtime_instruction: str,
) -> PromptAssembly:
    """Assemble the protected system prompt plus this turn's conversation."""

    return template.assemble(messages, tools, runtime_instruction)
