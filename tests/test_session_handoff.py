import unittest

from tsm_agt.core import (
    SessionResumeSafety, SessionTaskCatalogEntry,
    build_session_follow_up_goal, build_session_follow_up_handoff,
    find_background_leak,
)


class SessionFollowUpHandoffTest(unittest.TestCase):
    def entry(self, **overrides):
        values = {
            "task_id": "task-source",
            "goal": "push the verified commit to origin",
            "task_state": "AWAITING_USER",
            "workspace": "/workspace",
            "completed_work": ("remote and branch verified",),
            "remaining_work": ("push commit", "verify remote branch"),
            "verification_status": "blocked",
            "outcome_summaries": ("remote push",),
            "resume_safety": SessionResumeSafety.REQUIRES_VALIDATION,
            "phase1_state": "WAITING",
        }
        values.update(overrides)
        return SessionTaskCatalogEntry(**values)

    def test_goal_is_only_the_current_request(self):
        goal = build_session_follow_up_goal(
            "continue and push now", self.entry()
        )

        self.assertEqual(goal, "continue and push now")
        # The source facts must not leak into the authoritative goal.
        for forbidden in (
            "task-source", "push commit", "remote and branch verified",
            "verification: blocked", "Revalidate current workspace",
            "Do not inherit", "[session-follow-up]",
        ):
            self.assertNotIn(forbidden, goal)

    def test_background_is_separate_and_marked_non_authoritative(self):
        handoff = build_session_follow_up_handoff(
            "continue and push now", self.entry()
        )

        self.assertEqual(handoff.goal, "continue and push now")
        self.assertEqual(handoff.background_task["task_id"], "task-source")
        self.assertEqual(
            handoff.background_task["authority"], "SCOPED_BACKGROUND"
        )
        self.assertEqual(
            handoff.background_task["historical_remaining_work"],
            ["push commit", "verify remote branch"],
        )
        self.assertEqual(
            handoff.background_task["completed_work"],
            ["remote and branch verified"],
        )
        # Background texts are exactly what a leak check may compare against.
        self.assertIn("push commit", handoff.background_texts)
        self.assertIn(
            "push the verified commit to origin", handoff.background_texts
        )

    def test_is_deterministic_and_bounded(self):
        entry = self.entry(
            goal="g" * 4000,
            completed_work=tuple("c" * 500 for _ in range(20)),
            remaining_work=tuple("r" * 500 for _ in range(20)),
            outcome_summaries=tuple("o" * 500 for _ in range(20)),
        )
        first = build_session_follow_up_handoff("u" * 5000, entry)
        second = build_session_follow_up_handoff("u" * 5000, entry)

        self.assertEqual(first, second)
        self.assertLessEqual(len(first.goal), 2000)
        self.assertLessEqual(
            len(first.background_task["historical_remaining_work"]), 4
        )
        self.assertLessEqual(len(first.background_task["completed_work"]), 3)

    def test_never_contains_privileged_runtime_payload_fields(self):
        handoff = build_session_follow_up_handoff("continue", self.entry())
        rendered = repr(handoff)

        for forbidden in (
            "payload_hash", "resume_token", "tool_batch",
            "workspace_access_grant", "process_handle",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_find_background_leak_detects_verbatim_copy(self):
        background = (
            "apply the requested UI styling change for input messages",
        )

        self.assertIsNotNone(find_background_leak(
            "criterion: apply the requested UI styling change for input "
            "messages",
            background,
        ))
        self.assertIsNone(find_background_leak(
            "criterion: the input messages use a light grey background",
            background,
        ))
        # Short incidental overlap is not a leak.
        self.assertIsNone(find_background_leak(
            "criterion: change input styling", ("input styling",),
        ))

    def test_find_background_leak_is_case_and_whitespace_insensitive(self):
        background = ("Apply The Requested UI   Styling Change For Input",)

        self.assertIsNotNone(find_background_leak(
            "APPLY THE REQUESTED UI STYLING CHANGE FOR INPUT",
            background,
        ))


if __name__ == "__main__":
    unittest.main()
