"""The rubric judge adapter: bounded, structured verdicts, fail-closed.

Nothing in the Runtime composes this adapter any more -- no ``rubric`` criterion
is evaluated, so ``RubricJudgePort`` is absent from ``KernelDependencies`` and
from both OpenAI-compatible compositions. The adapter and its port stay in the
tree so the capability can be re-wired by registering the port again; the
review-model override (SPEC §12.6 D5) that used to feed it was removed with the
wiring. This file therefore pins the adapter's own contract and nothing else.
"""

from __future__ import annotations

import unittest

from tsm_agt.adapters.model_rubric_judge import ModelRubricJudge
from tsm_agt.ports import (
    FinishReason,
    JudgeVerdict,
    Message,
    MessageRole,
    ModelResponse,
    ModelUsage,
    RubricEvidence,
    TextBlock,
)


class _StubModel:
    def __init__(self, text: str | None = None, error: Exception | None = None):
        self._text = text
        self._error = error
        self.requests: list = []

    async def complete(self, request) -> ModelResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return ModelResponse(
            Message("stub", MessageRole.ASSISTANT, (TextBlock(self._text or ""),)),
            FinishReason.STOP, ModelUsage(1, 1),
        )


class RubricJudgeAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def _judge_with(self, text=None, error=None):
        model = _StubModel(text=text, error=error)
        judge = ModelRubricJudge(model)
        await judge.start(None)
        judgement = await judge.judge(
            "c1", "explain the phase",
            (RubricEvidence("event:1", "the answer body"),),
        )
        return judgement, model

    async def test_satisfied_verdict(self):
        judgement, _ = await self._judge_with(
            '{"verdict":"satisfied","reason":"ok"}'
        )
        self.assertIs(judgement.verdict, JudgeVerdict.SATISFIED)
        self.assertTrue(judgement.passed)

    async def test_needs_revision_verdict(self):
        judgement, _ = await self._judge_with(
            '{"verdict":"needs_revision","reason":"missing"}'
        )
        self.assertIs(judgement.verdict, JudgeVerdict.NEEDS_REVISION)
        self.assertFalse(judgement.passed)

    async def test_unparseable_is_undecidable_not_an_exception(self):
        judgement, _ = await self._judge_with("not json at all")
        self.assertIs(judgement.verdict, JudgeVerdict.UNDECIDABLE)

    async def test_provider_error_is_fail_closed(self):
        judgement, _ = await self._judge_with(error=RuntimeError("provider down"))
        self.assertIs(judgement.verdict, JudgeVerdict.JUDGE_ERROR)
        self.assertFalse(judgement.passed)

    async def test_judge_receives_evidence_excerpt_not_only_reference(self):
        _, model = await self._judge_with('{"verdict":"satisfied"}')

        payload = model.requests[0].messages[-1].text
        self.assertIn('"evidence"', payload)
        self.assertIn("the answer body", payload)
        self.assertNotIn("evidence_references", payload)


if __name__ == "__main__":
    unittest.main()
