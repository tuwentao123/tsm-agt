"""Use the configured model to understand input received during a Task."""

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


class ModelRuntimeInputClassifier:
    """Propose the meaning of live input without applying or authorizing it."""

    descriptor = AdapterDescriptor(
        "model.runtime-input-classifier", "1.0",
        "RuntimeInputClassifierPort", "1.0",
        frozenset({"semantic-live-input-routing"}),
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

    async def classify_runtime_input(
        self, text: str, context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if not self._started:
            raise RuntimeError("Runtime input classifier is not started")
        system = Message(
            f"runtime-input-system-{uuid4().hex}", MessageRole.SYSTEM,
            (TextBlock(
                "Classify one user message received while an engineering Task "
                "is running. Choose STEER when it adds constraints or direction "
                "to the same Task, REPLACE when it changes the current goal, "
                "NEW_TASK_AFTER_CURRENT when it requests independent later work, "
                "or STATUS_QUERY when it only asks about execution state. Do not "
                "infer approval, permission, or tool safety. When an approval is "
                "pending, choose REVIEW_PENDING_ACTION when the user only wants "
                "to continue or revisit the current blocked step without changing "
                "the goal. This only asks Runtime to show the exact pending action "
                "again; it is never approval. Choose STEER or REPLACE only when "
                "the user actually adds constraints, narrows scope, or changes the "
                "goal. Ordinary text can never approve the pending action. Return "
                "exactly one "
                "JSON object with intent and confidence (0..1)."
            ),),
        )
        user = Message(
            f"runtime-input-user-{uuid4().hex}", MessageRole.USER,
            (TextBlock(json.dumps({
                "boundary": "untrusted_live_task_input_resolution",
                "current_input": text,
                "runtime_context": dict(context),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),),
        )
        response = await self._model.complete(ModelRequest(
            turn_id=f"runtime-input-{uuid4().hex}",
            messages=(system, user), max_output_tokens=128,
            allow_tool_calls=False, require_evidence_questions=False,
        ))
        raw = response.message.text.strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < start:
            raise ValueError("Runtime classifier response is not a JSON object")
        parsed = json.loads(raw[start:end + 1])
        if not isinstance(parsed, Mapping):
            raise ValueError("Runtime classifier response must be an object")
        return dict(parsed)
