"""Goal alignment is authored by the Runtime, never by the Planner.

An answer-bearing Task gets a Runtime-authored ``goal_alignment`` criterion, and
that kind is excluded from the model-facing proposal schema. Nothing evaluates
the criterion any more -- the judge that used to do so is no longer composed --
so this file pins the authoring contract only.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    TASK_SPEC_PROPOSAL_SCHEMA_V1,
    TaskCriterionKind,
    TaskState,
)
from tsm_agt.ports import (
    RuntimeStorePort,
    AdapterDescriptor, HealthState, HealthStatus,
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

    async def test_conversation_keeps_advisory_spec_without_fixed_alignment_contract(self) -> None:
        app, task, spec = await self._planned("ANSWER", "task-answer")
        self.assertEqual(spec.acceptance_criteria, ())
        self.assertEqual(spec.outcomes, ())
        events = await app.registry.require(RuntimeStorePort).read_events(task.task_id)
        self.assertFalse(any(e.event_type == "task_spec.alignment_criterion_added" for e in events))

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


