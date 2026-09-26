from __future__ import annotations

import unittest

from tsm_agt.sdk.runtime import RuntimeTaskResult


class RuntimeTaskResultTest(unittest.TestCase):
    def test_runtime_api_result_exposes_phase1_state_with_legacy_state(self) -> None:
        result = RuntimeTaskResult(
            task_id="task-1",
            state="AWAITING_APPROVAL",
            phase1_state="WAITING",
            status="awaiting_approval",
            cursor=4,
        )

        self.assertEqual(result.to_data(), {
            "task_id": "task-1",
            "state": "AWAITING_APPROVAL",
            "phase1_state": "WAITING",
            "status": "awaiting_approval",
            "cursor": 4,
            "assistant_text": None,
            "approval": None,
            "clarification": None,
            "verification": None,
            "evidence_level": None,
            "projection": None,
            "latest_answer_event_ref": None,
            "conclusion_claims": [],
            "conclusion_validation": None,
            "completion_diagnostics": None,
        })


if __name__ == "__main__":
    unittest.main()
