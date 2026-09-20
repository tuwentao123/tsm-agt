from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tsm_agt.core import (
    ApprovalRequest, InvalidTaskTransition, Phase1TaskState, TaskSnapshot,
    TaskState,
    WorkspaceAccessCapability, WorkspaceAccessGrant,
)
from tsm_agt.ports import ToolCall, ToolRisk


class TaskStateMachineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 31, tzinfo=timezone.utc)
        self.task = TaskSnapshot.create("task-1", "fix tests", "/workspace", self.now)

    def test_phase1_state_projection_maps_all_legacy_states(self) -> None:
        expected = {
            TaskState.CREATED: Phase1TaskState.PREPARING,
            TaskState.INTAKE: Phase1TaskState.PREPARING,
            TaskState.RESOLVING_PROJECT: Phase1TaskState.PREPARING,
            TaskState.SELECTING_EXTENSIONS: Phase1TaskState.PREPARING,
            TaskState.ROUTING: Phase1TaskState.PREPARING,
            TaskState.PLANNING: Phase1TaskState.RUNNING,
            TaskState.RUNNING_WORKFLOW: Phase1TaskState.RUNNING,
            TaskState.EXECUTING: Phase1TaskState.RUNNING,
            TaskState.AWAITING_APPROVAL: Phase1TaskState.WAITING,
            TaskState.AWAITING_USER: Phase1TaskState.WAITING,
            TaskState.INTERRUPTING: Phase1TaskState.INTERRUPTED,
            TaskState.INTERRUPTED: Phase1TaskState.INTERRUPTED,
            TaskState.RESUMING: Phase1TaskState.RUNNING,
            TaskState.CONFLICT: Phase1TaskState.RUNNING,
            TaskState.VERIFYING: Phase1TaskState.RUNNING,
            TaskState.FINALIZING: Phase1TaskState.RUNNING,
            TaskState.SUCCEEDED: Phase1TaskState.DONE,
            TaskState.CANCELLED: Phase1TaskState.CANCELLED,
            TaskState.FAILED: Phase1TaskState.FAILED,
        }
        self.assertEqual(set(expected), set(TaskState))
        for legacy, phase1 in expected.items():
            with self.subTest(legacy=legacy):
                self.assertIs(legacy.phase1_state, phase1)

    def test_created_can_move_to_intake(self) -> None:
        updated = self.task.transition(TaskState.INTAKE, self.now)

        self.assertEqual(updated.state, TaskState.INTAKE)
        self.assertEqual(updated.task_id, self.task.task_id)
        self.assertEqual(updated.created_at, self.task.created_at)

    def test_created_cannot_skip_to_executing(self) -> None:
        with self.assertRaisesRegex(
            InvalidTaskTransition, "CREATED -> EXECUTING"
        ):
            self.task.transition(TaskState.EXECUTING, self.now)

    def test_terminal_state_cannot_transition(self) -> None:
        terminal = TaskSnapshot(
            self.task.task_id,
            self.task.goal,
            self.task.workspace,
            TaskState.SUCCEEDED,
            self.now,
            self.now,
        )

        self.assertTrue(terminal.state.is_terminal)
        with self.assertRaises(InvalidTaskTransition):
            terminal.transition(TaskState.EXECUTING, self.now)

    def test_interrupted_checkpoint_can_surface_identity_conflict(self) -> None:
        current = self.task
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING, TaskState.INTERRUPTING,
            TaskState.INTERRUPTED,
        ):
            current = current.transition(state, self.now)

        conflicted = current.transition(TaskState.CONFLICT, self.now)

        self.assertEqual(conflicted.state, TaskState.CONFLICT)

    def test_serialization_round_trip(self) -> None:
        data = self.task.to_data()
        self.assertEqual(data["phase1_state"], Phase1TaskState.PREPARING.value)
        self.assertEqual(TaskSnapshot.from_data(data), self.task)

    def test_workspace_read_grant_round_trip_and_legacy_default(self) -> None:
        grant = WorkspaceAccessGrant(
            "grant-1", "/external/project", WorkspaceAccessCapability.READ,
            "approval-1", self.now,
        )
        granted = self.task.with_workspace_access_grant(grant, self.now)
        self.assertEqual(TaskSnapshot.from_data(granted.to_data()), granted)
        legacy = self.task.to_data()
        legacy.pop("workspace_access_grants")
        self.assertEqual(TaskSnapshot.from_data(legacy).workspace_access_grants, ())

    def test_pending_approval_serialization_round_trip(self) -> None:
        request = ApprovalRequest(
            request_id="approval-1",
            task_id=self.task.task_id,
            turn_id="turn-1",
            invocation_id="inv-1",
            policy_decision_id="policy-1",
            payload_hash="abc123",
            risk=ToolRisk.R1,
            call=ToolCall("call-1", "fixture.write", {"text": "change"}),
            action="write fixture data",
            target="text=change",
            preview='{"text": "change"}',
            network_access="none",
            data_transmission="none",
            rollback="restore previous value",
            created_at=self.now,
        )
        executing = self.task
        for state in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            executing = executing.transition(state, self.now)
        awaiting = executing.await_approval(request, self.now)

        self.assertEqual(TaskSnapshot.from_data(awaiting.to_data()), awaiting)

    def test_legacy_approval_without_preview_still_loads(self) -> None:
        request = {
            "request_id": "approval-legacy",
            "task_id": "task-legacy",
            "turn_id": "turn-1",
            "invocation_id": "inv-1",
            "policy_decision_id": "policy-1",
            "payload_hash": "legacy-hash",
            "risk": "R1",
            "call": {
                "call_id": "call-1",
                "name": "fixture.write",
                "arguments": {"text": "change"},
            },
            "action": "legacy write",
            "target": "text=change",
            "network_access": "not required",
            "rollback": "restore previous value",
            "created_at": self.now.isoformat(),
        }
        restored = ApprovalRequest.from_data(request)

        self.assertIn("legacy approval", restored.preview)
        self.assertEqual(restored.data_transmission, "not declared")


if __name__ == "__main__":
    unittest.main()
