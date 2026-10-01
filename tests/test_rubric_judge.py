"""P2: rubric criterion + bounded judge.

See 《验收判定改造实施SPEC.md》P2. A judgement criterion must be evaluated by a
bounded judge that always terminates, and it must never enter the per-turn
required-gap path (which would recreate the original dead-lock).
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tsm_agt.adapters.model_rubric_judge import ModelRubricJudge
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    TaskAcceptanceCriterion,
    TaskCriterionKind,
    TaskState,
)
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


async def _executing_task(app, root: Path, task_id: str):
    task = await app.kernel.create_task("deliver a phase assessment", root, task_id=task_id)
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await app.kernel.transition_task(task.task_id, state, state.value)
    return task


async def _set_rubric(app, task) -> None:
    current = await app.kernel.get_task_spec(task.task_id)
    spec = replace(
        current, revision=current.revision + 1, content_hash="",
        acceptance_criteria=(TaskAcceptanceCriterion(
            "ac_phase", "Explain which implementation phase the work is in",
            TaskCriterionKind.RUBRIC,
        ),),
    )
    await app.kernel._append_events(task.task_id, ((
        "task_spec.revised", {"snapshot": spec.to_data()},
    ),))


class RubricJudgeAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def _judge_with(self, text=None, error=None, *, judge=None):
        model = _StubModel(text=text, error=error)
        judge = judge or ModelRubricJudge(model)
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


