"""Use the configured model to understand ordinary Session input."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import uuid4

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, FinishReason, HealthState, HealthStatus,
    Message, MessageRole, ModelProviderPort, ModelRequest, ModelResponse,
    ModelStreamCompleted, StreamingModelProviderPort, TextBlock,
    ModelCallPurpose, ToolCallBlock, ToolIdempotency, ToolRisk, ToolSpec,
    SessionRouteResolutionError,
)


SESSION_ROUTE_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "disposition": {
            "type": "string",
            "enum": ["ANSWER", "CREATE_TASK", "RESUME_TASK", "CLARIFY"],
        },
        "relation": {
            "type": "string",
            "enum": ["INDEPENDENT", "CONTINUE", "FOLLOW_UP",
                     "BRANCH", "UNCERTAIN"],
        },
        "source_task_id": {"type": ["string", "null"]},
        "input_grounding": {
            "type": "string",
            "enum": ["SELF_CONTAINED", "CONTEXT_DEPENDENT", "AMBIGUOUS"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason_code": {"type": "string"},
        "clarification": {"type": ["string", "null"]},
        "candidate_task_ids": {
            "type": "array", "items": {"type": "string"},
        },
    },
    # Every field here is checkable against an enum or the supplied catalog.
    # A goal is deliberately absent: Runtime builds it from the user's own words
    # plus the selected Task's bounded summary, so this resolver classifies the
    # message and never restates it.
    "required": [
        "disposition", "relation", "source_task_id",
        "input_grounding", "confidence", "reason_code",
        "clarification", "candidate_task_ids",
    ],
    "additionalProperties": False,
}


class ModelSessionInputResolver:
    """Semantic resolver only; it cannot execute or authorize anything."""

    descriptor = AdapterDescriptor(
        "model.session-input-resolver", "1.0",
        "SessionInputResolverPort", "1.0",
        frozenset({"semantic-session-routing"}),
    )

    def __init__(
        self, model: ModelProviderPort, *, timeout_seconds: float = 15.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Session routing timeout must be positive")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def resolve_session_input(
        self, text: str, context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if not self._started:
            raise RuntimeError("Session input resolver is not started")
        system = Message(
            f"session-input-system-{uuid4().hex}", MessageRole.SYSTEM,
            (TextBlock(
                "Propose how to route one user message inside an engineering-"
                "agent Session. Return disposition ANSWER, CREATE_TASK, "
                "RESUME_TASK, or CLARIFY, separately from relation INDEPENDENT, "
                "CONTINUE, FOLLOW_UP, BRANCH, or UNCERTAIN. ANSWER is only for "
                "a self-contained informational question that needs no workspace, "
                "runtime state, network, tool, file, command, or Task mutation; "
                "ANSWER must set relation INDEPENDENT and source_task_id null. "
                "CREATE_TASK+INDEPENDENT starts "
                "unrelated work. RESUME_TASK+CONTINUE resumes one unfinished "
                "Task. CREATE_TASK+FOLLOW_UP or +BRANCH creates new work grounded "
                "in a prior Task without reopening its checkpoint. CLARIFY is "
                "only for genuine ambiguity. Never infer approval, permission, "
                "or tool safety. Separately classify input_grounding as "
                "SELF_CONTAINED only when current_input alone states a complete "
                "new goal, CONTEXT_DEPENDENT when its meaning requires Session "
                "history or candidates, or AMBIGUOUS when uncertain. Return "
                "Resolve references from recent_messages and the explicit "
                "conversation_anchor_task_id before considering Task state. An "
                "unfinished Task is not automatically the referent; state is "
                "only an execution fact. For a context-dependent short message, "
                "prefer the latest coherent conversational thread, and CLARIFY "
                "when two threads remain similarly plausible. When an anchor "
                "exists, choose another source only if current_input explicitly "
                "identifies that Task by ID, goal, topic, artifact, or pending "
                "decision; mere unfinished status is never sufficient. "
                "When session.submit_route_proposal is supplied, call it exactly "
                "once and return no prose. Otherwise return exactly one JSON "
                "object with disposition, relation, source_task_id, "
                "input_grounding, confidence, reason_code, clarification, "
                "candidate_task_ids. Do not write a goal for the new work and do "
                "not restate the request: Runtime builds the goal from the user's "
                "own words plus the selected Task summary. source_task_id "
                "must be null for INDEPENDENT and must exactly match task_catalog "
                "for CONTINUE, FOLLOW_UP, or BRANCH. RESUME_TASK may select only "
                "a non-terminal catalog entry. A terminal Task (FAILED, SUCCEEDED, "
                "CANCELLED) is never a RESUME_TASK target, but it IS a valid "
                "CREATE_TASK+FOLLOW_UP or +BRANCH source: propose FOLLOW_UP with "
                "that source when current_input explicitly refers back to it, by "
                "continue/接着/那 phrasing or by naming its goal, topic, or artifact. "
                "Never treat a terminal Task as the default referent; a "
                "self-contained new goal stays CREATE_TASK+INDEPENDENT even when a "
                "terminal Task exists. Selecting an entry only identifies which "
                "Task the message refers to; it does not execute a checkpoint, "
                "approve an action, answer a clarification, or grant authority. "
                "Any supplied unfinished candidate may therefore be selected "
                "for CONTINUE, including AWAIT_USER_ACTION entries; Runtime applies its "
                "safety protocol after selection. candidate_index is the stable "
                "number shown by the Harness; ordinal references may use it. "
                "pending_interaction, when present, contains only authoritative "
                "identifiers from an earlier UI choice; it does not mean the "
                "current message answered that choice. "
                "If disposition is CLARIFY, candidate_task_ids may list only "
                "plausible supplied catalog entries; do not invent numbered "
                "options because the Harness renders authoritative choices. "
                "For compatibility, legacy action NEW_TASK/RESUME_TASK/CLARIFY "
                "is accepted, but prefer this v2 schema. confidence "
                "is 0..1."
            ),),
        )
        user = Message(
            f"session-input-user-{uuid4().hex}", MessageRole.USER,
            (TextBlock(json.dumps({
                "boundary": "untrusted_session_input_resolution",
                "current_input": text,
                "session_context": dict(context),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),),
        )
        supports_tools = self._model.capabilities.tools
        submit_tool = ToolSpec(
            "session.submit_route_proposal",
            "Submit one semantic Session route proposal. This does not execute "
            "or authorize any Task.",
            SESSION_ROUTE_PROPOSAL_SCHEMA, ToolRisk.R0, is_read_only=True,
            is_concurrency_safe=True, idempotency=ToolIdempotency.IDEMPOTENT,
            is_internal_state=True,
        )
        messages = (system, user)
        last_error: SessionRouteResolutionError | None = None
        for attempt in (1, 2):
            request = ModelRequest(
                turn_id=f"session-input-{uuid4().hex}",
                messages=messages, max_output_tokens=512,
                tools=(submit_tool,) if supports_tools else (),
                allow_tool_calls=supports_tools,
                require_evidence_questions=False,
                purpose=ModelCallPurpose.SESSION_ROUTING,
                timeout_seconds=self._timeout_seconds, max_provider_attempts=1,
            )
            response = await asyncio.wait_for(
                self._complete(request), timeout=self._timeout_seconds + 1.0,
            )
            try:
                proposal = self._extract_proposal(
                    response, submit_tool.name, attempt
                )
                proposal = self._normalize_legacy_proposal(proposal)
                self._validate_proposal(proposal, attempt)
                return proposal
            except SessionRouteResolutionError as error:
                last_error = error
                if attempt == 2:
                    raise
                correction = Message(
                    f"session-route-correction-{uuid4().hex}",
                    MessageRole.USER, (TextBlock(json.dumps({
                        "boundary": "session_route_protocol_correction",
                        "error": error.reason_code,
                        "required_fields": list(
                            SESSION_ROUTE_PROPOSAL_SCHEMA["required"]
                        ),
                        "instruction": (
                            "Resubmit exactly one route proposal. Use the supplied "
                            "tool call or one JSON object matching the same schema; "
                            "return no explanatory prose."
                        ),
                    }, sort_keys=True, separators=(",", ":"))),),
                )
                messages = messages + (response.message, correction)
        assert last_error is not None
        raise last_error

    async def _complete(self, request: ModelRequest) -> ModelResponse:
        """Consume streaming providers so session routing remains cancellable."""
        if not isinstance(self._model, StreamingModelProviderPort):
            return await self._model.complete(request)
        completed: ModelResponse | None = None
        async for event in self._model.stream_complete(request):
            if isinstance(event, ModelStreamCompleted):
                completed = event.response
        if completed is None:
            raise RuntimeError("session routing stream ended without a response")
        return completed

    @staticmethod
    def _extract_proposal(
        response, submit_tool_name: str, attempt: int,
    ) -> dict[str, Any]:
        calls = tuple(
            block.call for block in response.message.content
            if isinstance(block, ToolCallBlock)
        )
        if calls:
            if len(calls) != 1 or calls[0].name != submit_tool_name:
                raise SessionRouteResolutionError(
                    "provider_response", "ROUTE_TOOL_CALL_INVALID",
                    attempt=attempt, finish_reason=response.finish_reason.value,
                    tool_call_count=len(calls),
                    returned_tool_names=tuple(call.name for call in calls),
                )
            return dict(calls[0].arguments)
        raw = response.message.text.strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < start:
            raise SessionRouteResolutionError(
                "provider_response", "ROUTE_PROPOSAL_MISSING",
                attempt=attempt, finish_reason=response.finish_reason.value,
            )
        try:
            parsed = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as error:
            raise SessionRouteResolutionError(
                "json_parse", "ROUTE_JSON_INVALID", attempt=attempt,
                finish_reason=response.finish_reason.value,
            ) from error
        if not isinstance(parsed, Mapping):
            raise SessionRouteResolutionError(
                "json_parse", "ROUTE_JSON_NOT_OBJECT", attempt=attempt,
                finish_reason=response.finish_reason.value,
            )
        return dict(parsed)

    @staticmethod
    def _normalize_legacy_proposal(
        proposal: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Map the documented v1 envelope to v2 before strict validation."""
        if "disposition" in proposal:
            normalized = dict(proposal)
            # A goal is no longer part of the contract, but a model that still
            # volunteers one must not fail the whole proposal: strict validation
            # would degrade the route and discard a classification that is
            # otherwise usable. Dropping it is safe because Runtime builds the
            # goal itself and never reads this key.
            normalized.pop("resolved_goal", None)
            return normalized
        action = proposal.get("action")
        if action not in {"NEW_TASK", "RESUME_TASK", "CLARIFY"}:
            return dict(proposal)
        task_id = proposal.get("task_id")
        disposition = {
            "NEW_TASK": "CREATE_TASK",
            "RESUME_TASK": "RESUME_TASK",
            "CLARIFY": "CLARIFY",
        }[str(action)]
        relation = {
            "NEW_TASK": "INDEPENDENT",
            "RESUME_TASK": "CONTINUE",
            "CLARIFY": "UNCERTAIN",
        }[str(action)]
        return {
            "disposition": disposition,
            "relation": relation,
            "source_task_id": task_id if action == "RESUME_TASK" else None,
            "input_grounding": proposal.get(
                "input_grounding", "AMBIGUOUS"
            ),
            "confidence": proposal.get("confidence", 0.0),
            "reason_code": proposal.get("reason_code", "legacy_route"),
            "clarification": proposal.get("clarification"),
            "candidate_task_ids": proposal.get("candidate_task_ids", []),
        }

    @staticmethod
    def _validate_proposal(proposal: Mapping[str, Any], attempt: int) -> None:
        required = set(SESSION_ROUTE_PROPOSAL_SCHEMA["required"])
        actual = set(proposal)
        if actual != required:
            raise SessionRouteResolutionError(
                "schema_validation", "ROUTE_SCHEMA_FIELDS_INVALID",
                attempt=attempt, argument_keys=tuple(sorted(actual)),
            )
        if proposal["disposition"] not in {
            "ANSWER", "CREATE_TASK", "RESUME_TASK", "CLARIFY",
        } or proposal["relation"] not in {
            "INDEPENDENT", "CONTINUE", "FOLLOW_UP",
            "BRANCH", "UNCERTAIN",
        } or proposal["input_grounding"] not in {
            "SELF_CONTAINED", "CONTEXT_DEPENDENT", "AMBIGUOUS",
        }:
            raise SessionRouteResolutionError(
                "schema_validation", "ROUTE_SCHEMA_ENUM_INVALID",
                attempt=attempt, argument_keys=tuple(sorted(actual)),
            )
        nullable_strings = ("source_task_id", "clarification")
        if any(
            proposal[key] is not None and not isinstance(proposal[key], str)
            for key in nullable_strings
        ) or not isinstance(proposal["reason_code"], str):
            raise SessionRouteResolutionError(
                "schema_validation", "ROUTE_SCHEMA_TYPE_INVALID",
                attempt=attempt, argument_keys=tuple(sorted(actual)),
            )
        confidence = proposal["confidence"]
        candidates = proposal["candidate_task_ids"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
            or not isinstance(candidates, list)
            or not all(isinstance(item, str) for item in candidates)
        ):
            raise SessionRouteResolutionError(
                "schema_validation", "ROUTE_SCHEMA_TYPE_INVALID",
                attempt=attempt, argument_keys=tuple(sorted(actual)),
            )
