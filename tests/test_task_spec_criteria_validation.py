"""P0-1: authoring-time validation and demotion of unresolvable criteria.

See 《验收判定改造实施SPEC.md》P0-1. The critical compatibility rule under
test here is that validation must NOT live in ``__post_init__`` / ``from_data``:
the incident Task ``task-2c8f8fbb75714516a94ab442995626b5`` stored ``turn-...``
references in a durable event, so a hard failure on read would make it
impossible to replay.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    TaskAcceptanceCriterion,
    TaskCriterionKind,
    TaskState,
    validate_authored_reference,
)
from tsm_agt.ports import AdapterDescriptor, HealthState, HealthStatus


def _reference_spec(reference: str) -> dict:
    return {
        "schema_version": 1,
        "goal": "report which phase the implementation is in",
        "scope": ["."],
        "constraints": ["read only", "do not modify the workspace"],
        "acceptance_criteria": [{
            "criterion_id": "ac_phase",
            "description": "explain the current implementation phase",
            "verification_kind": "evidence_reference",
            "evidence_reference": reference,
        }],
        "outcomes": [{
            "outcome_id": "answer",
            "description": "deliver the phase assessment",
            "kind": "ANSWER",
            "required_effects": ["observe"],
            "required": True,
        }],
        "continuation_policy": {"mode": "NONE"},
    }


class _ReferencePlanner:
    descriptor = AdapterDescriptor(
        "fixture.reference-planner", "1.0", "TaskSpecPlannerPort", "1.0"
    )

    def __init__(self, reference: str) -> None:
        self._reference = reference

    async def start(self, context) -> None:
        pass

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY, "ready")

    async def stop(self, deadline) -> None:
        pass

    async def propose_task_spec(self, goal, context):
        return _reference_spec(self._reference)


async def _executing_task(app, root: Path, goal: str):
    task = await app.kernel.create_task(goal, root)
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await app.kernel.transition_task(task.task_id, state, state.value)
    return task


class AuthoredReferenceValidationTest(unittest.TestCase):
    def test_turn_reference_is_rejected_at_authoring_time(self):
        with self.assertRaises(ValueError):
            validate_authored_reference(
                TaskCriterionKind.EVIDENCE_REFERENCE,
                "turn-922dbad959d54ee3a8cfc4b752187228",
            )

    def test_machine_reference_is_accepted(self):
        for reference in ("event:1", "tool_call:abc", "mutation:xyz"):
            validate_authored_reference(
                TaskCriterionKind.EVIDENCE_REFERENCE, reference,
            )

    def test_non_evidence_kinds_are_untouched(self):
        validate_authored_reference(TaskCriterionKind.WORKSPACE_INTEGRITY, None)
        validate_authored_reference(TaskCriterionKind.RUBRIC, None)

    def test_historical_snapshot_with_turn_reference_still_reads(self):
        """Replay safety: the incident Task's stored event must remain readable."""
        criterion = TaskAcceptanceCriterion.from_data({
            "criterion_id": "ac_current_phase_assessed",
            "description": "explain the current phase",
            "verification_kind": "evidence_reference",
            "evidence_reference": "turn-922dbad959d54ee3a8cfc4b752187228",
        })
        self.assertEqual(
            criterion.evidence_reference,
            "turn-922dbad959d54ee3a8cfc4b752187228",
        )


class CriteriaDemotionTest(unittest.IsolatedAsyncioTestCase):
    async def _demote(self, reference):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = compose_fixture_application()
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing_task(app, Path(directory.name), "report the phase")
        events = await app.kernel.dependencies.store.read_events(task.task_id)
        criterion = TaskAcceptanceCriterion(
            "ac_phase", "report phase", TaskCriterionKind.EVIDENCE_REFERENCE,
            evidence_reference=reference,
        )
        return app.kernel._demote_unresolvable_criteria(task, events, (criterion,))

    async def test_unresolvable_reference_is_demoted_to_rubric(self):
        criteria, demoted = await self._demote("turn-922dbad9")
        self.assertIs(criteria[0].verification_kind, TaskCriterionKind.RUBRIC)
        self.assertIsNone(criteria[0].evidence_reference)
        self.assertEqual(demoted[0]["criterion_id"], "ac_phase")

    async def test_resolvable_reference_is_kept(self):
        criteria, demoted = await self._demote("event:1")
        self.assertIs(criteria[0].verification_kind, TaskCriterionKind.EVIDENCE_REFERENCE)
        self.assertFalse(demoted)
