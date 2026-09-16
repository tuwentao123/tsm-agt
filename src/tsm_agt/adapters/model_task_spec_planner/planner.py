"""Obtain a bounded Task SPEC proposal through a dedicated tool call."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import uuid4

from tsm_agt.core import TASK_SPEC_PROPOSAL_SCHEMA_V1
from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, FinishReason, HealthState, HealthStatus,
    Message, MessageRole, ModelProviderPort, ModelRequest, TextBlock,
    ModelResponse, ModelStreamCompleted, StreamingModelProviderPort,
    ToolCallBlock, ToolIdempotency, ToolRisk, ToolSpec,
)


class ModelTaskSpecPlanner:
    """Semantic proposal only; Kernel validates and owns the final Snapshot."""

    descriptor = AdapterDescriptor(
        "model.task-spec-planner", "1.0", "TaskSpecPlannerPort", "1.0",
        frozenset({"tool-call-proposal", "bounded-correction"}),
    )

    def __init__(self, model: ModelProviderPort) -> None:
        self._model = model
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "task planner ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def propose_task_spec(
        self, goal: str, context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if not self._started:
            raise RuntimeError("Task SPEC planner is not started")
        supports_tools = self._model.capabilities.tools
        tool = ToolSpec(
            "planner.submit_task_spec",
            "Submit the outcomes required to satisfy the current user task.",
            TASK_SPEC_PROPOSAL_SCHEMA_V1, ToolRisk.R0, is_read_only=True,
            is_concurrency_safe=True, idempotency=ToolIdempotency.IDEMPOTENT,
            is_internal_state=True,
        )
        correction = ""
        for attempt in range(2):
            system = Message(
                f"task-planner-system-{uuid4().hex}", MessageRole.SYSTEM,
                (TextBlock(
                    "Translate the user request into a project-neutral Task SPEC. "
                    + (
                        "Call planner.submit_task_spec exactly once. "
                        if supports_tools else
                        "Return exactly one JSON object matching the supplied "
                        "Task SPEC schema, with no prose or Markdown. "
                    ) +
                    "Outcomes describe "
                    "observable results, not steps or promises. ANSWER and "
                    "USER_DECISION may have no required effects; other outcomes "
                    "must name the effects needed. A pure knowledge answer may use "
                    "ANSWER with no effects. An answer that depends on the current "
                    "workspace, files, runtime state, or other tool-observable facts "
                    "must require observe (or use a separate EVIDENCE outcome). "
                    "Read-only observation may support an ANSWER without directly "
                    "completing it. Use the default EXPLICIT_ACCEPTANCE for "
                    "analysis, implementation, multi-file changes, commands plus "
                    "explanation, and every dynamically discovered workflow. Use "
                    "ATOMIC_ACTION only when one exact tool call is the entire user "
                    "result; then include atomic_action.tool_name and the exact "
                    "atomic_action.arguments. Never use REQUIRED_EFFECTS for new "
                    "work. Never claim completion, status, "
                    "IDs owned by Runtime, approval, or permission." + correction
                ),),
            )
            user = Message(
                f"task-planner-user-{uuid4().hex}", MessageRole.USER,
                (TextBlock(json.dumps({
                    "boundary": "untrusted_task_spec_planning",
                    "goal": goal, "runtime_context": dict(context),
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),),
            )
            response = await self._complete(ModelRequest(
                turn_id=f"task-planner-{uuid4().hex}",
                messages=(system, user),
                tools=(tool,) if supports_tools else (),
                max_output_tokens=1200,
                allow_tool_calls=supports_tools,
                require_evidence_questions=False,
            ))
            calls = tuple(
                block.call for block in response.message.content
                if isinstance(block, ToolCallBlock)
            )
            if (
                not supports_tools
                and response.finish_reason in {FinishReason.STOP, FinishReason.LENGTH}
            ):
                try:
                    from tsm_agt.core import TaskSpecProposal
                    parsed = json.loads(response.message.text)
                    if not isinstance(parsed, Mapping):
                        raise ValueError("Task SPEC text must be a JSON object")
                    return TaskSpecProposal.from_data(parsed).to_data()
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    pass
            elif (
                response.finish_reason is FinishReason.TOOL_CALL
                and len(calls) == 1
                and calls[0].name == tool.name
            ):
                try:
                    # Validate here so malformed arguments get the one bounded
                    # protocol correction instead of escaping to Kernel first.
                    from tsm_agt.core import TaskSpecProposal
                    proposal = TaskSpecProposal.from_data(calls[0].arguments)
                    return proposal.to_data()
                except (KeyError, TypeError, ValueError):
                    pass
            correction = (
                " Previous output violated the protocol. Return no prose and "
                + (
                    "make exactly one planner.submit_task_spec tool call."
                    if supports_tools else
                    "return exactly one valid Task SPEC JSON object."
                )
            )
        raise ValueError("model did not submit one valid Task SPEC proposal")

    async def _complete(self, request: ModelRequest) -> ModelResponse:
        """Use cancellable streaming when the Provider exposes it."""
        if not isinstance(self._model, StreamingModelProviderPort):
            return await self._model.complete(request)
        completed: ModelResponse | None = None
        async for event in self._model.stream_complete(request):
            if isinstance(event, ModelStreamCompleted):
                completed = event.response
        if completed is None:
            raise RuntimeError("Task planner model stream ended without a response")
        return completed
