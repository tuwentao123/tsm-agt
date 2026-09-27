"""INV-9: authority is a data boundary, not a prompt request.

A Task's goal is only the user's current request. Facts about a source Task are
persisted separately as SCOPED_BACKGROUND, and any acceptance criterion that
copies background text verbatim is demoted to the advisory rubric channel.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    ContextAuthority,
    SessionTaskRelation,
    TaskAcceptanceCriterion,
    TaskCriterionKind,
)
from tsm_agt.ports import (
    AdapterDescriptor, HealthState, HealthStatus, RuntimeStorePort,
)

_BACKGROUND_GOAL = (
    "apply the requested UI styling change for input messages"
)
_REMAINING = "apply the requested UI styling change for input messages"


def _planning_context() -> dict:
    return {
        "related_task": {
            "task_id": "task-source",
            "state": "INTERRUPTED",
            "goal": _BACKGROUND_GOAL,
            "historical_remaining_work": [_REMAINING],
            "completed_work": [],
            "authority": ContextAuthority.SCOPED_BACKGROUND.value,
        },
        "session": {
            "working_state": {
                "goal": _BACKGROUND_GOAL,
                "remaining_work": [_REMAINING],
            },
        },
    }


class _LeakPlanner:
    descriptor = AdapterDescriptor(
        "fixture.leak-planner", "1.0", "TaskSpecPlannerPort", "1.0"
    )

    def __init__(self, criteria: tuple[dict, ...]) -> None:
        self._criteria = criteria

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
            "acceptance_criteria": list(self._criteria),
            "outcomes": [{
                "outcome_id": "answer",
                "description": "deliver the answer",
                "kind": "ANSWER",
                "required_effects": ["observe"],
                "required": True,
            }],
            "continuation_policy": {"mode": "NONE"},
        }


def _criterion(criterion_id: str, description: str) -> dict:
    return {
        "criterion_id": criterion_id,
        "description": description,
        "verification_kind": "workspace_integrity",
    }


class BackgroundLeakDemotionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.application = compose_fixture_application()
        await self.application.registry.start_all()
        self.kernel = self.application.kernel

    async def asyncTearDown(self) -> None:
        await self.application.registry.stop_all()

    def test_criterion_verbatim_from_background_is_demoted(self) -> None:
        criteria = (
            TaskAcceptanceCriterion(
                "ac_ui", _REMAINING, TaskCriterionKind.WORKSPACE_INTEGRITY,
            ),
        )

        kept, demoted = self.kernel._demote_background_leaks(
            criteria, _planning_context(), "继续啊",
        )

        self.assertEqual(kept[0].verification_kind, TaskCriterionKind.RUBRIC)
        self.assertEqual(len(demoted), 1)
        self.assertEqual(demoted[0]["criterion_id"], "ac_ui")
        self.assertEqual(
            demoted[0]["source"], ContextAuthority.SCOPED_BACKGROUND.value
        )

    def test_criterion_from_user_request_survives(self) -> None:
        # The user's own words are never treated as background, so an
        # authoritative criterion that restates the request must survive.
        request = "make the input messages light grey"
        criteria = (
            TaskAcceptanceCriterion(
                "ac_grey", request, TaskCriterionKind.WORKSPACE_INTEGRITY,
            ),
        )
        context = _planning_context()
        context["session"]["working_state"]["goal"] = request

        kept, demoted = self.kernel._demote_background_leaks(
            criteria, context, request,
        )

        self.assertEqual(
            kept[0].verification_kind, TaskCriterionKind.WORKSPACE_INTEGRITY
        )
        self.assertEqual(demoted, [])

    def test_unrelated_criterion_is_untouched(self) -> None:
        criteria = (
            TaskAcceptanceCriterion(
                "ac_other", "the build command exits with code 0",
                TaskCriterionKind.POST_MUTATION_COMMAND,
            ),
        )

        kept, demoted = self.kernel._demote_background_leaks(
            criteria, _planning_context(), "继续啊",
        )

        self.assertEqual(
            kept[0].verification_kind, TaskCriterionKind.POST_MUTATION_COMMAND
        )
        self.assertEqual(demoted, [])


class BackgroundLeakIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_leak_demotion_emits_event_and_keeps_criterion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            planner = _LeakPlanner((
                _criterion("ac_leak", _REMAINING),
                _criterion("ac_safe", "the build command exits with code 0"),
            ))
            application = compose_fixture_application(
                task_spec_planner_adapter=planner,
            )
            await application.registry.start_all()
            try:
                kernel = application.kernel
                source = await kernel.create_task(
                    _BACKGROUND_GOAL, workspace, task_id="task-source",
                    original_user_text=_BACKGROUND_GOAL,
                )
                follow_up = await kernel.create_task(
                    "继续啊", workspace, task_id="task-follow-up",
                    session_id=source.session_id,
                    source_task_id=source.task_id,
                    task_relation=SessionTaskRelation.FOLLOW_UP,
                    original_user_text="继续啊",
                    context_handoff={
                        "task_id": source.task_id,
                        "state": "INTERRUPTED",
                        "goal": _BACKGROUND_GOAL,
                        "historical_remaining_work": [_REMAINING],
                        "completed_work": [],
                        "authority": "SCOPED_BACKGROUND",
                    },
                )

                spec = await kernel.plan_task_spec(follow_up.task_id)

                by_id = {c.criterion_id: c for c in spec.acceptance_criteria}
                self.assertEqual(
                    by_id["ac_leak"].verification_kind,
                    TaskCriterionKind.RUBRIC,
                )
                self.assertEqual(
                    by_id["ac_safe"].verification_kind,
                    TaskCriterionKind.WORKSPACE_INTEGRITY,
                )
                store = application.registry.require(RuntimeStorePort)
                events = await store.read_events(follow_up.task_id)
                leak_events = [
                    event for event in events
                    if event.event_type == "task_spec.background_leak_demoted"
                ]
                self.assertEqual(len(leak_events), 1)
                self.assertEqual(
                    leak_events[0].payload["demoted"][0]["criterion_id"],
                    "ac_leak",
                )
                # The persisted handoff is non-authoritative by construction.
                created = next(
                    event for event in events
                    if event.event_type == "task.created"
                )
                self.assertEqual(
                    created.payload["context_handoff"]["authority"],
                    "SCOPED_BACKGROUND",
                )
            finally:
                await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
