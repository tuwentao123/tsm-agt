from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tsm_agt.core import (
    TaskDisplayStatus, TaskExecutionStatus, TaskRuntimePhase,
    TaskRuntimeProjector, TaskState, TaskVerificationStatus,
)
from tsm_agt.core.task import TaskSnapshot
from tsm_agt.ports import RuntimeEvent


BASE = datetime(2026, 9, 21, tzinfo=timezone.utc)


def event(sequence: int, event_type: str, payload: dict | None = None) -> RuntimeEvent:
    return RuntimeEvent(
        f"evt-{sequence}", "task-projection", sequence, event_type,
        payload or {}, BASE + timedelta(seconds=sequence), 1,
    )


def task(state: TaskState) -> TaskSnapshot:
    snapshot = TaskSnapshot.create(
        "task-projection", "verify composer", "/workspace", BASE,
    )
    return TaskSnapshot(
        task_id=snapshot.task_id,
        goal=snapshot.goal,
        workspace=snapshot.workspace,
        state=state,
        created_at=snapshot.created_at,
        updated_at=BASE,
        session_id=snapshot.session_id,
    )


class TaskRuntimeProjectionTest(unittest.TestCase):
    def test_terminal_failure_overrides_old_running_trace(self) -> None:
        projection = TaskRuntimeProjector.project(task(TaskState.FAILED), (
            event(1, "task.created"),
            event(2, "task.state_changed", {
                "previous_state": "EXECUTING", "next_state": "FAILED",
            }),
            event(3, "verify.started", {"criterion_count": 1}),
            event(4, "verify.criterion_completed", {
                "criterion_id": "layout",
                "status": "failed",
                "evidence": [{"observed": "current hash differs from journal"}],
            }),
            event(5, "verify.completed", {"status": "failed"}),
        ))

        self.assertEqual(projection.display_status, TaskDisplayStatus.FAILED)
        self.assertEqual(projection.execution_status, TaskExecutionStatus.STOPPED)
        self.assertEqual(projection.phase, TaskRuntimePhase.VERIFYING)
        self.assertEqual(projection.verification_status, TaskVerificationStatus.FAILED)
        self.assertEqual(projection.trace_cursor, 5)
        self.assertEqual(projection.failure.code, "layout")
        self.assertEqual(
            projection.failure.summary, "verification criterion did not pass",
        )

    def test_waiting_approval_is_not_waiting_for_input(self) -> None:
        snapshot = task(TaskState.AWAITING_APPROVAL)
        projection = TaskRuntimeProjector.project(snapshot, (
            event(1, "task.created"),
            event(2, "task.state_changed", {
                "previous_state": "EXECUTING", "next_state": "AWAITING_APPROVAL",
            }),
        ))

        self.assertEqual(projection.display_status, TaskDisplayStatus.WAITING)
        self.assertEqual(
            projection.execution_status, TaskExecutionStatus.WAITING_APPROVAL,
        )
        self.assertEqual(projection.waiting_kind, "approval")
        self.assertEqual(projection.phase, TaskRuntimePhase.EXECUTING)

    def test_v3_conclusion_projection_is_independent_from_domain_check(self) -> None:
        projection = TaskRuntimeProjector.project(task(TaskState.SUCCEEDED), (
            event(1, "verify.completed", {"status": "passed"}),
            event(2, "conclusion.recorded", {
                "latest_answer_event_ref": "evt-answer",
                "conclusion_claims": [{
                    "claim_id": "claim-1", "kind": "verified",
                    "summary": "targeted tests passed",
                }],
                "conclusion_validation": {
                    "status": "invalid",
                    "claims": [{"claim_id": "claim-1", "status": "invalid"}],
                },
            }),
        ))

        self.assertEqual(projection.verification_status, TaskVerificationStatus.PASSED)
        self.assertEqual(projection.latest_answer_event_ref, "evt-answer")
        self.assertEqual(projection.conclusion_claims[0]["claim_id"], "claim-1")
        self.assertEqual(projection.conclusion_validation["status"], "invalid")

    def test_legacy_conclusion_event_never_derives_facts_from_assistant_text(self) -> None:
        projection = TaskRuntimeProjector.project(task(TaskState.SUCCEEDED), (
            event(1, "llm.completed", {
                "assistant_text": "验证通过，引用完全有效",
            }),
            event(2, "conclusion.references_validated", {
                "conclusion": {
                    "claims": [{
                        "claim_id": "claim-legacy", "kind": "checked",
                        "summary": "checked persisted fact",
                    }],
                },
                "validation": {
                    "status": "valid",
                    "claims": [{"claim_id": "claim-legacy", "status": "valid"}],
                },
            }),
        ))

        self.assertIsNone(projection.latest_answer_event_ref)
        self.assertEqual(projection.conclusion_claims[0]["claim_id"], "claim-legacy")
        self.assertEqual(projection.conclusion_validation["status"], "valid")
        self.assertNotIn("验证通过", str(projection.to_data()))

    def test_progress_does_not_participate_in_projection_input(self) -> None:
        projection = TaskRuntimeProjector.project(task(TaskState.SUCCEEDED), (
            event(1, "task.created"),
            event(2, "task.state_changed", {
                "previous_state": "FINALIZING", "next_state": "SUCCEEDED",
            }),
        ))

        self.assertEqual(projection.display_status, TaskDisplayStatus.COMPLETED)
        self.assertEqual(projection.execution_status, TaskExecutionStatus.STOPPED)
        self.assertEqual(projection.trace_cursor, 2)


if __name__ == "__main__":
    unittest.main()
