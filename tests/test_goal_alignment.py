"""INV-10 / INV-11: judged criteria need a judge, and answers must be on-topic.

An answer-bearing Task gets a Runtime-authored ``goal_alignment`` criterion, the
judge reads bounded evidence excerpts (never only opaque references), and a
judged criterion with no available judge can never pass.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    TASK_SPEC_PROPOSAL_SCHEMA_V1,
    AcceptanceStatus,
    TaskAcceptanceCriterion,
    TaskCriterionKind,
    TaskState,
)
from tsm_agt.ports import (
    AdapterDescriptor, HealthState, HealthStatus, JudgeVerdict,
    RubricEvidence, RubricJudgement,
)


class _SpecPlanner:
    descriptor = AdapterDescriptor(
        "fixture.outcome-planner", "1.0", "TaskSpecPlannerPort", "1.0"
    )

    def __init__(self, outcome_kind: str) -> None:
        self._outcome_kind = outcome_kind

    async def start(self, context) -> None:
        pass

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY, "ready")

    async def stop(self, deadline) -> None:
        pass

    async def propose_task_spec(self, goal, context):
        return {
            "schema_version": 1,
            "goal": goal,
            "scope": ["."],
            "constraints": [],
            "acceptance_criteria": [{
                "criterion_id": "ac_delivery",
                "description": "deliver the requested result",
                "verification_kind": "workspace_integrity",
            }],
            "outcomes": [{
                "outcome_id": "outcome",
                "description": "deliver the result",
                "kind": self._outcome_kind,
                "required_effects": (
                    ["observe"] if self._outcome_kind == "ANSWER" else ["mutate"]
                ),
                "required": True,
            }],
            "continuation_policy": {"mode": "NONE"},
        }


class _RecordingJudge:
    descriptor = AdapterDescriptor(
        "fixture.recording-judge", "2.0", "RubricJudgePort", "2.0"
    )

    def __init__(self, verdict: JudgeVerdict = JudgeVerdict.SATISFIED) -> None:
        self._verdict = verdict
        self.calls: list[tuple[str, str, tuple[RubricEvidence, ...]]] = []

    async def start(self, context) -> None:
        pass

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY, "ready")

    async def stop(self, deadline) -> None:
        pass

    async def judge(self, criterion_id, assertion, evidence) -> RubricJudgement:
        self.calls.append((criterion_id, assertion, tuple(evidence)))
        return RubricJudgement(criterion_id, self._verdict, "fixture verdict")


async def _executing_task(app, root: Path, task_id: str):
    task = await app.kernel.create_task(
        "explain the runtime", root, task_id=task_id,
    )
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await app.kernel.transition_task(task.task_id, state, state.value)
    return task


class GoalAlignmentCriterionTest(unittest.IsolatedAsyncioTestCase):
    async def _planned(self, outcome_kind: str, task_id: str):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = compose_fixture_application(
            task_spec_planner_adapter=_SpecPlanner(outcome_kind),
        )
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing_task(app, Path(directory.name), task_id)
        spec = await app.kernel.plan_task_spec(task.task_id)
        return app, task, spec

    async def test_answer_task_gets_goal_alignment_criterion(self) -> None:
        _, _, spec = await self._planned("ANSWER", "task-answer")

        by_id = {c.criterion_id: c for c in spec.acceptance_criteria}
        self.assertIn("answer-alignment", by_id)
        self.assertIs(
            by_id["answer-alignment"].verification_kind,
            TaskCriterionKind.GOAL_ALIGNMENT,
        )
        self.assertIn("explain the runtime",
                      by_id["answer-alignment"].description)

    async def test_non_answer_task_gets_no_alignment_criterion(self) -> None:
        _, _, spec = await self._planned(
            "WORKSPACE_DELIVERY", "task-workspace",
        )

        self.assertFalse(any(
            c.verification_kind is TaskCriterionKind.GOAL_ALIGNMENT
            for c in spec.acceptance_criteria
        ))

    def test_alignment_criterion_absent_from_planner_schema(self) -> None:
        enum = TASK_SPEC_PROPOSAL_SCHEMA_V1["properties"][
            "acceptance_criteria"
        ]["items"]["properties"]["verification_kind"]["enum"]

        self.assertNotIn("goal_alignment", enum)
        self.assertIn("rubric", enum)


class GoalAlignmentJudgingTest(unittest.IsolatedAsyncioTestCase):
    async def _verify(self, verdict: JudgeVerdict, answer: str, task_id: str):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        judge = _RecordingJudge(verdict)
        app = compose_fixture_application(
            rubric_judge_adapter=judge,
        )
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing_task(app, Path(directory.name), task_id)
        current = await app.kernel.get_task_spec(task.task_id)
        spec = replace(
            current, revision=current.revision + 1, content_hash="",
            acceptance_criteria=(TaskAcceptanceCriterion(
                "answer-alignment",
                "The delivered answer directly addresses the user's current "
                "goal: explain the runtime",
                TaskCriterionKind.GOAL_ALIGNMENT,
            ),),
        )
        await app.kernel._append_events(task.task_id, ((
            "task_spec.revised", {"snapshot": spec.to_data()},
        ),))
        await app.kernel._append_events(task.task_id, ((
            "llm.completed", {
                "turn_id": "turn-fixture",
                "message": {
                    "message_id": "assistant-fixture",
                    "role": "assistant",
                    "content": [{"type": "text", "text": answer}],
                },
            },
        ),))
        await app.kernel.transition_task(
            task.task_id, TaskState.VERIFYING, "verify",
        )
        verification = await app.kernel.verify_task_acceptance(task.task_id)
        return verification, judge

    async def test_alignment_judged_with_answer_and_goal_text(self) -> None:
        verification, judge = await self._verify(
            JudgeVerdict.SATISFIED, "the runtime is an event loop", "task-ok",
        )

        self.assertTrue(verification.passed, verification.to_data())
        self.assertEqual(len(judge.calls), 1)
        criterion_id, assertion, evidence = judge.calls[0]
        self.assertEqual(criterion_id, "answer-alignment")
        self.assertIn("explain the runtime", assertion)
        # The judge read the answer body, not just an opaque reference.
        self.assertTrue(evidence)
        self.assertTrue(any("event loop" in item.excerpt for item in evidence))

    async def test_unrelated_answer_fails_alignment(self) -> None:
        verification, judge = await self._verify(
            JudgeVerdict.NEEDS_REVISION,
            "Jersey Giant chickens are a dual-purpose breed",
            "task-offtopic",
        )

        self.assertFalse(verification.passed, verification.to_data())
        self.assertEqual(
            verification.status.value, AcceptanceStatus.BLOCKED.value
        )
        self.assertEqual(judge.calls[0][0], "answer-alignment")

    async def test_judged_criterion_without_judge_cannot_pass(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = compose_fixture_application()
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing_task(
            app, Path(directory.name), "no-judge",
        )
        current = await app.kernel.get_task_spec(task.task_id)
        spec = replace(
            current, revision=current.revision + 1, content_hash="",
            acceptance_criteria=(TaskAcceptanceCriterion(
                "answer-alignment", "answer the request",
                TaskCriterionKind.GOAL_ALIGNMENT,
            ),),
        )
        await app.kernel._append_events(task.task_id, ((
            "task_spec.revised", {"snapshot": spec.to_data()},
        ),))
        await app.kernel.transition_task(
            task.task_id, TaskState.VERIFYING, "verify",
        )

        verification = await app.kernel.verify_task_acceptance(task.task_id)

        self.assertFalse(verification.passed, verification.to_data())
        self.assertEqual(
            verification.status.value, AcceptanceStatus.BLOCKED.value
        )

    async def test_readiness_gap_reports_missing_judge(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = compose_fixture_application()
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing_task(
            app, Path(directory.name), "no-judge-gap",
        )
        current = await app.kernel.get_task_spec(task.task_id)
        spec = replace(
            current, revision=current.revision + 1, content_hash="",
            acceptance_criteria=(TaskAcceptanceCriterion(
                "answer-alignment", "answer the request",
                TaskCriterionKind.GOAL_ALIGNMENT,
            ),),
        )
        await app.kernel._append_events(task.task_id, ((
            "task_spec.revised", {"snapshot": spec.to_data()},
        ),))

        gaps = await app.kernel._completion_readiness_gaps(task.task_id, ())

        matching = [
            gap for gap in gaps
            if gap.kind == "JUDGED_CRITERION_WITHOUT_JUDGE"
        ]
        self.assertEqual(len(matching), 1)
        # INV-3 must not demote it: it is a configuration gap, not work.
        self.assertTrue(matching[0].required)


if __name__ == "__main__":
    unittest.main()
