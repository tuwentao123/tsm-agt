"""Use the configured model to understand ordinary Session input."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import uuid4

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus, Message,
    MessageRole, ModelProviderPort, ModelRequest, TextBlock,
)


class ModelSessionInputResolver:
    """Semantic resolver only; it cannot execute or authorize anything."""

    descriptor = AdapterDescriptor(
        "model.session-input-resolver", "1.0",
        "SessionInputResolverPort", "1.0",
        frozenset({"semantic-session-routing"}),
    )

    def __init__(self, model: ModelProviderPort) -> None:
        self._model = model
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
                "Route one user message inside an engineering-agent Session. "
                "Choose NEW_TASK for an independent request, RESUME_TASK only "
                "when it semantically refers to one unfinished candidate, or "
                "CLARIFY when ambiguous. Never infer approval, permission, or "
                "tool safety. Return exactly one JSON object with action, "
                "task_id, confidence, reason_code, clarification. task_id must "
                "be null unless action is RESUME_TASK and exactly match a "
                "supplied candidate. Only candidates whose safety is "
                "EXACT_RESUME or REBASE_REQUIRED may be selected for "
                "RESUME_TASK. confidence is 0..1."
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
        response = await self._model.complete(ModelRequest(
            turn_id=f"session-input-{uuid4().hex}",
            messages=(system, user), max_output_tokens=256,
            allow_tool_calls=False, require_evidence_questions=False,
        ))
        raw = response.message.text.strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < start:
            raise ValueError("Session resolver response is not a JSON object")
        parsed = json.loads(raw[start:end + 1])
        if not isinstance(parsed, Mapping):
            raise ValueError("Session resolver response must be an object")
        return dict(parsed)
