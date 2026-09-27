"""Bounded LLM-as-judge adapter for a ``rubric`` acceptance criterion.

The judge cannot run tools and only reads the criterion plus the durable
evidence references, so it never becomes an authority. Every failure mode
(unavailable provider, unparseable output) maps to a non-satisfied verdict
rather than an exception, which keeps final acceptance fail-closed but bounded.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus, JudgeVerdict,
    Message, MessageRole, ModelProviderPort, ModelRequest, ModelResponse,
    RubricEvidence, RubricJudgePort, RubricJudgement, TextBlock,
)


class ModelRubricJudge:
    descriptor = AdapterDescriptor(
        "model.rubric-judge", "2.0", "RubricJudgePort", "2.0",
        frozenset({"bounded-judgement", "structured-verdict", "fail-closed"}),
    )

    def __init__(self, model: ModelProviderPort) -> None:
        self._model = model
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "rubric judge ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def judge(
        self, criterion_id: str, assertion: str,
        evidence: tuple[RubricEvidence, ...],
    ) -> RubricJudgement:
        if not self._started:
            raise RuntimeError("rubric judge is not started")
        request = ModelRequest(
            turn_id=f"rubric-{uuid4().hex}",
            messages=(
                Message(
                    f"rubric-system-{uuid4().hex}", MessageRole.SYSTEM,
                    (TextBlock(
                        "You are a strict acceptance judge. Decide only whether "
                        "the delivered work satisfies the stated criterion. You "
                        "cannot run tools. Return exactly one JSON object: "
                        '{"verdict": "satisfied"|"needs_revision"|"undecidable", '
                        '"reason": "<short reason>"}. '
                        "Use needs_revision only when the criterion is clearly "
                        "unmet; use undecidable when the evidence is insufficient."
                    ),),
                ),
                Message(
                    f"rubric-user-{uuid4().hex}", MessageRole.USER,
                    (TextBlock(json.dumps({
                        "criterion_id": criterion_id,
                        "criterion": assertion,
                        "evidence": [item.to_data() for item in evidence],
                    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),),
                ),
            ),
            max_output_tokens=256,
            require_evidence_questions=False,
        )
        try:
            response = await self._model.complete(request)
        except Exception as error:
            return RubricJudgement(
                criterion_id, JudgeVerdict.JUDGE_ERROR,
                f"judge unavailable: {type(error).__name__}",
            )
        return self._parse(criterion_id, response)

    @staticmethod
    def _parse(criterion_id: str, response: ModelResponse) -> RubricJudgement:
        try:
            payload: Any = json.loads(response.message.text.strip())
        except (ValueError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            return RubricJudgement(
                criterion_id, JudgeVerdict.UNDECIDABLE,
                "judge returned no parseable verdict",
            )
        raw = str(payload.get("verdict", "")).strip().lower()
        verdict = {
            "satisfied": JudgeVerdict.SATISFIED,
            "needs_revision": JudgeVerdict.NEEDS_REVISION,
            "undecidable": JudgeVerdict.UNDECIDABLE,
        }.get(raw, JudgeVerdict.UNDECIDABLE)
        return RubricJudgement(
            criterion_id, verdict, str(payload.get("reason", ""))[:500],
        )


__all__ = ["ModelRubricJudge"]
