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
    async def test_unresolvable_reference_is_demoted_to_rubric(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                task_spec_planner_adapter=_ReferencePlanner("turn-922dbad9"),
            )
            await app.registry.start_all()
            try:
                task = await _executing_task(app, root, "report the phase")
                spec = await app.kernel.plan_task_spec(task.task_id)
                by_id = {c.criterion_id: c for c in spec.acceptance_criteria}
                # INV-11 adds the Runtime-authored alignment criterion.
                self.assertEqual(
                    set(by_id), {"ac_phase", "answer-alignment"}
                )
                self.assertIs(
                    by_id["ac_phase"].verification_kind,
                    TaskCriterionKind.RUBRIC,
                )
                self.assertIsNone(by_id["ac_phase"].evidence_reference)

                events = await app.kernel._dependencies.store.read_events(
                    task.task_id
                )
                demoted = [
                    event for event in events
                    if event.event_type == "task_spec.criteria_demoted"
                ]
                self.assertEqual(len(demoted), 1)
                payload = demoted[0].payload
                self.assertEqual(
                    payload["demoted"][0]["criterion_id"], "ac_phase",
                )
            finally:
                await app.registry.stop_all()

    async def test_resolvable_reference_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                task_spec_planner_adapter=_ReferencePlanner("event:1"),
            )
            await app.registry.start_all()
            try:
                task = await _executing_task(app, root, "report the phase")
                spec = await app.kernel.plan_task_spec(task.task_id)
                self.assertIs(
                    spec.acceptance_criteria[0].verification_kind,
                    TaskCriterionKind.EVIDENCE_REFERENCE,
                )
                events = await app.kernel._dependencies.store.read_events(
                    task.task_id
                )
                self.assertFalse(any(
                    event.event_type == "task_spec.criteria_demoted"
                    for event in events
                ))
            finally:
                await app.registry.stop_all()

    async def test_demoted_rubric_without_judge_is_blocked_not_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                task_spec_planner_adapter=_ReferencePlanner("turn-922dbad9"),
            )
            await app.registry.start_all()
            try:
                task = await _executing_task(app, root, "report the phase")
                spec = await app.kernel.plan_task_spec(task.task_id)
                by_id = {c.criterion_id: c for c in spec.acceptance_criteria}
                self.assertIs(
                    by_id["ac_phase"].verification_kind,
                    TaskCriterionKind.RUBRIC,
                )
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify",
                )
                verification = await app.kernel.verify_task_acceptance(
                    task.task_id
                )
                # INV-10: this fixture registers no judge, so a demoted rubric
                # (and the Runtime alignment criterion) must not vacuously pass.
                self.assertFalse(verification.passed, verification.to_data())
                self.assertEqual(verification.status.value, "blocked")
                blocked_ids = {
                    item.criterion_id for item in verification.criteria
                    if item.status.value == "blocked"
                }
                self.assertIn("ac_phase", blocked_ids)
            finally:
                await app.registry.stop_all()

    async def test_revise_rejects_turn_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application()
            await app.registry.start_all()
            try:
                task = await _executing_task(app, root, "report the phase")
                with self.assertRaises(ValueError):
                    await app.kernel.revise_task_spec(
                        task.task_id,
                        (await app.kernel.get_task_spec(task.task_id)).revision,
                        scope=(".",), constraints=(),
                        acceptance_criteria=(TaskAcceptanceCriterion(
                            "ac_bad", "bad reference",
                            TaskCriterionKind.EVIDENCE_REFERENCE,
                            "turn-922dbad9",
                        ),),
                        operation_id="revise-1", writer="test",
                    )
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
