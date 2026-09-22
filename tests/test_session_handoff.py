import unittest

from tsm_agt.core import (
    SessionResumeSafety, SessionTaskCatalogEntry, build_session_follow_up_goal,
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

    def test_contains_current_request_and_authority_free_source_summary(self):
        goal = build_session_follow_up_goal(
            "continue and push now", self.entry()
        )

        self.assertIn("continue and push now", goal)
        self.assertIn("task-source", goal)
        self.assertIn("push commit", goal)
        self.assertIn("remote and branch verified", goal)
        self.assertIn("verification: blocked", goal)
        self.assertIn("Revalidate current workspace", goal)
        self.assertIn("Do not inherit", goal)

    def test_is_deterministic_and_bounded(self):
        entry = self.entry(
            goal="g" * 4000,
            completed_work=tuple("c" * 500 for _ in range(20)),
            remaining_work=tuple("r" * 500 for _ in range(20)),
            outcome_summaries=tuple("o" * 500 for _ in range(20)),
        )
        first = build_session_follow_up_goal("u" * 5000, entry)
        second = build_session_follow_up_goal("u" * 5000, entry)

        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 2000)
        self.assertLessEqual(first.count("\n- " + "r"), 4)
        self.assertLessEqual(first.count("\n- " + "c"), 3)

    def test_never_contains_privileged_runtime_payload_fields(self):
        goal = build_session_follow_up_goal("continue", self.entry())

        for forbidden in (
            "payload_hash", "resume_token", "tool_batch",
            "workspace_access_grant", "process_handle",
        ):
            self.assertNotIn(forbidden, goal)


if __name__ == "__main__":
    unittest.main()
