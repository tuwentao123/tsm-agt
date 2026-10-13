import unittest

from tsm_agt.core.conversation_orchestration import (
    TaskBindingKind,
    TaskBindingRequest,
    TaskBindingTask,
    bind_task_input,
)


class TaskBinderTest(unittest.TestCase):
    def bind(self, *states: tuple[str, str], **kwargs):
        return bind_task_input(TaskBindingRequest(
            current_input_event_ref="input:abc123",
            tasks=tuple(
                TaskBindingTask(
                    task_id,
                    state,
                    has_unfinished_work=state not in {"SUCCEEDED", "FAILED"},
                )
                for task_id, state in states
            ),
            **kwargs,
        ))

    def test_explicit_task_id_wins_over_new_and_active_cursor(self) -> None:
        decision = self.bind(
            ("task-explicit", "SUCCEEDED"),
            ("task-active", "EXECUTING"),
            explicit_task_id="task-explicit",
            new_task_requested=True,
        )

        self.assertEqual(decision.binding_kind, TaskBindingKind.EXPLICIT_REFERENCE)
        self.assertEqual(decision.task_id, "task-explicit")
        self.assertTrue(decision.requires_runtime_inspection)

    def test_new_request_creates_new_binding(self) -> None:
        decision = self.bind(
            ("task-active", "EXECUTING"), new_task_requested=True
        )

        self.assertEqual(decision.binding_kind, TaskBindingKind.NEW)
        self.assertIsNone(decision.task_id)

    def test_unique_executing_task_is_active_continuation(self) -> None:
        decision = self.bind(("task-running", "EXECUTING"))

        self.assertEqual(decision.binding_kind, TaskBindingKind.ACTIVE_CONTINUATION)
        self.assertEqual(decision.task_id, "task-running")

    def test_unique_running_workflow_task_is_active_continuation(self) -> None:
        decision = self.bind(("task-running", "RUNNING_WORKFLOW"))

        self.assertEqual(decision.binding_kind, TaskBindingKind.ACTIVE_CONTINUATION)
        self.assertEqual(decision.task_id, "task-running")

    def test_awaiting_user_matches_typed_pending_interaction(self) -> None:
        decision = self.bind(
            ("task-waiting", "AWAITING_USER"),
            selected_interaction_task_id="task-waiting",
        )

        self.assertEqual(decision.binding_kind, TaskBindingKind.USER_INTERACTION)
        self.assertEqual(decision.task_id, "task-waiting")

    def test_awaiting_user_without_typed_match_requires_choice(self) -> None:
        decision = self.bind(("task-waiting", "AWAITING_USER"))

        self.assertEqual(decision.binding_kind, TaskBindingKind.CHOICE_REQUIRED)
        self.assertIsNone(decision.task_id)

    def test_awaiting_approval_accepts_only_typed_resolution(self) -> None:
        accepted = self.bind(
            ("task-approval", "AWAITING_APPROVAL"),
            approval_resolution_task_id="task-approval",
        )
        queued = self.bind(("task-approval", "AWAITING_APPROVAL"))

        self.assertEqual(accepted.binding_kind, TaskBindingKind.USER_INTERACTION)
        self.assertEqual(accepted.task_id, "task-approval")
        self.assertEqual(queued.binding_kind, TaskBindingKind.CHOICE_REQUIRED)
        self.assertIsNone(queued.task_id)

    def test_no_active_or_terminal_only_tasks_create_new_binding(self) -> None:
        no_tasks = self.bind()
        terminal_only = self.bind(
            ("task-done", "SUCCEEDED"), ("task-failed", "FAILED")
        )

        self.assertEqual(no_tasks.binding_kind, TaskBindingKind.NEW)
        self.assertEqual(terminal_only.binding_kind, TaskBindingKind.NEW)

    def test_multiple_active_candidates_require_choice_without_semantic_guess(self) -> None:
        decision = self.bind(
            ("task-running", "EXECUTING"),
            ("task-awaiting", "AWAITING_USER"),
        )

        self.assertEqual(decision.binding_kind, TaskBindingKind.CHOICE_REQUIRED)
        self.assertIsNone(decision.task_id)

    def test_unknown_explicit_task_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not in the Session"):
            self.bind(("task-known", "EXECUTING"), explicit_task_id="task-missing")

    def test_explicit_continuation_uses_unique_unfinished_resumable_task(self) -> None:
        decision = self.bind(
            ("task-interrupted", "INTERRUPTED"),
            continuation_requested=True,
        )

        self.assertEqual(decision.binding_kind, TaskBindingKind.EXPLICIT_CONTINUATION)
        self.assertEqual(decision.task_id, "task-interrupted")
        self.assertTrue(decision.continuation_requested)
        self.assertEqual(decision.candidate_task_ids, ("task-interrupted",))

    def test_ambiguous_explicit_continuation_requires_choice_not_new_task(self) -> None:
        decision = self.bind(
            ("task-one", "EXECUTING"),
            ("task-two", "INTERRUPTED"),
            continuation_requested=True,
        )

        self.assertEqual(decision.binding_kind, TaskBindingKind.CHOICE_REQUIRED)
        self.assertIsNone(decision.task_id)
        self.assertEqual(
            decision.candidate_task_ids,
            ("task-one", "task-two"),
        )
        self.assertEqual(decision.reason_code, "ambiguous_unfinished_continuation")

    def test_explicit_continuation_cannot_bypass_pending_approval(self) -> None:
        decision = self.bind(
            ("task-approval", "AWAITING_APPROVAL"),
            continuation_requested=True,
        )

        self.assertEqual(decision.binding_kind, TaskBindingKind.CHOICE_REQUIRED)
        self.assertIsNone(decision.task_id)
