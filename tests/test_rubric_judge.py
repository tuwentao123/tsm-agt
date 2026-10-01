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


class ReviewerModelConfigurationTest(unittest.TestCase):
    """SPEC §12.6 D5: the reviewer is a model *name*, never a second endpoint."""

    def configuration(self, *, review=None):
        import tempfile
        from pathlib import Path

        from tsm_agt.bootstrap.model_configuration import load_model_configuration

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            lines = [
                "TSM_AGT_MODEL_BASE_URL=https://models.example.test/v1",
                "TSM_AGT_MODEL=open",
                "TSM_AGT_OPEN_MODEL=primary-model",
                "TSM_AGT_MODEL_API_KEY=test-secret",
            ]
            if review is not None:
                lines.append(f"TSM_AGT_REVIEW_MODEL={review}")
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return load_model_configuration(env_file, {})

    def test_absent_review_model_reuses_the_primary_name(self) -> None:
        configuration = self.configuration()
        self.assertEqual(configuration.review_model, configuration.model)

    def test_review_model_overrides_only_the_name(self) -> None:
        configuration = self.configuration(review="smaller-model")
        self.assertEqual(configuration.review_model, "smaller-model")
        # Same endpoint, same key, same protocol: D5 by construction.
        self.assertEqual(configuration.model, "primary-model")
        self.assertEqual(configuration.protocol, "open")
        self.assertEqual(configuration.base_url, "https://models.example.test/v1")

    def test_empty_review_model_falls_back_to_the_primary_name(self) -> None:
        # An empty value is "not configured", not "model named empty".
        configuration = self.configuration(review="")
        self.assertEqual(configuration.review_model, configuration.model)

    def test_review_model_is_never_a_required_setting(self) -> None:
        # Omitting it entirely must load cleanly; it is an override, not a
        # second provider configuration.
        configuration = self.configuration()
        self.assertTrue(configuration.base_url)
        self.assertTrue(configuration.api_key)


class ReviewerProviderWiringTest(unittest.IsolatedAsyncioTestCase):
    """D5 wiring: no override means the *same* provider object is reused."""

    async def _judge_provider(self, *, review_model):
        import tempfile
        from pathlib import Path

        from tsm_agt.bootstrap import (
            compose_openai_compatible_engineering_application_from_env,
        )
        from tsm_agt.ports import ModelProviderPort, RubricJudgePort

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            lines = [
                "TSM_AGT_MODEL_BASE_URL=https://models.example.test/v1",
                "TSM_AGT_MODEL=open",
                "TSM_AGT_OPEN_MODEL=primary-model",
                "TSM_AGT_MODEL_API_KEY=test-secret",
            ]
            if review_model:
                lines.append(f"TSM_AGT_REVIEW_MODEL={review_model}")
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            import os
            from unittest.mock import patch

            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_env(
                        env_file
                    )
                )
        await application.registry.start_all()
        try:
            return (
                application.registry.require(ModelProviderPort),
                application.registry.require(RubricJudgePort),
            )
        finally:
            await application.registry.stop_all()

    async def test_absent_override_reuses_the_primary_provider(self) -> None:
        primary, judge = await self._judge_provider(review_model="")
        # Identity, not equality: no second instance means no second retry
        # counter or connection state -- the core flow is untouched.
        self.assertIs(judge._model, primary)

    async def test_override_builds_a_separate_provider_with_shared_credentials(
        self,
    ) -> None:
        primary, judge = await self._judge_provider(review_model="smaller-model")
        self.assertIsNot(judge._model, primary)
        # Same endpoint, same key, same protocol -- only the model name differs.
        # The primary registration wraps the physical provider in the resilient
        # layer, so compare the physical providers underneath.
        physical_primary = getattr(primary, "_provider", primary)
        physical_reviewer = getattr(judge._model, "_provider", judge._model)
        self.assertEqual(physical_reviewer._base_url, physical_primary._base_url)
        self.assertEqual(physical_reviewer._api_key, physical_primary._api_key)
        self.assertEqual(physical_reviewer._model, "smaller-model")
        self.assertEqual(physical_primary._model, "primary-model")
