"""Stable prompt assembly for the conversation loop.

The loop owns no prompt of its own: it composes the same protected template the
other orchestration paths use and lets that template filter its capability
guidance against the tools actually advertised this turn.  What this module adds
is the one *dynamic* instruction the runtime may hand the model: a compact,
facts-only note about action and verification results.

That note is deliberately not a gate. It describes outcomes and reminds the
model to finish relevant verification, without picking a verifier or repair.
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

_FAILURE_CONTINUATION = (
    "Continue the original request. Determine what failed and whether the action "
    "took effect. Use the observed result to select and perform the next permitted "
    "action in this turn, then return to the remaining work and verification. "
    "One failed method does not establish that every method is blocked. Do not "
    "replace feasible execution with a proposal or instructions for the user. "
    "Respect the user's scope, denials, and approval boundaries; inspect uncertain "
    "effects before risking a duplicate. Report a blocker only when observations "
    "establish a constraint preventing further permitted progress."
)

_POST_MUTATION_GUIDANCE = {
    "missing": (
        "No recognized verification attempt is recorded after the latest mutation. "
        "A write receipt or file read-back does not prove behavior. Choose validation "
        "by change type, acceptance criteria, and affected scope: compilation, type/import "
        "checks, focused behavior tests, or relevant project commands as sufficient. "
        "Runtime/end-to-end testing is not mandatory for every edit. For implementation "
        "work, execute an appropriate available check and finish remaining wiring before "
        "finalizing. Missing live integration infrastructure does not prevent independent "
        "local checks; run feasible checks and report the actual coverage gap. Do not "
        "infer verification is unavailable without observed evidence. "
        "If an adequate check is already observed through another "
        "tool, use its actual evidence and scope instead of repeating work merely to "
        "satisfy command recognition. Prose-only changes need no runtime test."
    ),
    "failed": (
        "The latest recognized verification attempt after the mutation failed. "
        "Inspect the failure, determine its relation to this change, repair relevant "
        "failures, and rerun the affected check. Do not treat a successful invocation "
        "as a passing check or an unrelated failure as proof your change is broken."
    ),
    "unknown": (
        "The latest recognized verification attempt has no observed terminal outcome. "
        "Inspect its status, wait, or logs through the available tools before claiming "
        "it passed. Do not start a duplicate check while its effect is uncertain."
    ),
    "passed": (
        "The latest recognized verification attempt after the mutation passed. "
        "It establishes only what that check covers. Compare the requested behavior "
        "and remaining implementation with the observed evidence; perform further "
        "checks only for material gaps, and do not repeat a still-valid passing check."
    ),
}

def latest_tool_result_instruction(
    kernel: Any, state: TurnState, model_tools: tuple[ToolSpec, ...] = (),
) -> str:
    """Read the newest durable tool outcome and state only what it reports.

    Returns an empty string when no failure or successful mutation needs context.
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
            "request; do not claim the action succeeded."
        )
    post_mutation_data = post_mutation.prompt_data()
    if post_mutation_data is not None and post_mutation.status in _POST_MUTATION_GUIDANCE:
        fact_parts.append(
            "Runtime post-mutation facts: " + json.dumps(
                post_mutation_data, ensure_ascii=False, sort_keys=True,
            ) + ". " + _POST_MUTATION_GUIDANCE[post_mutation.status]
            + " Choose checks from the request, project evidence, and advertised "
            "capabilities. Respect scope, denials, and approval boundaries. If "
            "verification is unavailable, establish the concrete blocker and state "
            "what remains unverified; never claim unobserved success."
        )
    if state.last_tool_error:
        error_instruction = (
            "The latest durable tool result is a failure "
            f"({state.last_tool_error}). Consult the tool result for details; "
            "do not claim the failed action succeeded."
        )
        fact_parts.append(error_instruction)
    if latest_failure is not None or state.last_tool_error:
        fact_parts.append(_FAILURE_CONTINUATION)
    return "\n\n".join(fact_parts)


def assemble_stable_prompt(
    template: PromptTemplate,
    messages: tuple[Message, ...],
    tools: tuple[ToolSpec, ...],
    runtime_instruction: str,
) -> PromptAssembly:
    """Assemble the protected system prompt plus this turn's conversation."""

    return template.assemble(messages, tools, runtime_instruction)
