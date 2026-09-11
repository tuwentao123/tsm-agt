from __future__ import annotations

import unittest

from tsm_agt.ports import ToolRecoveryKind, ToolResult


class ToolRecoveryContractTest(unittest.TestCase):
    """Recovery metadata remains explicit and old stored results still load."""

    def test_legacy_retryable_failure_maps_to_retry_same(self) -> None:
        result = ToolResult.from_data({
            "call_id": "legacy", "ok": False, "data": None,
            "error_code": "TIMEOUT", "message": "try again",
            "hint": None, "retryable": True, "truncated": False,
            "meta": {},
        })
        self.assertEqual(
            result.effective_recovery_kind, ToolRecoveryKind.RETRY_SAME
        )

    def test_state_change_recovery_round_trips(self) -> None:
        original = ToolResult(
            "scope", False, error_code="TOOL_SCOPE_MISMATCH",
            message="wrong root",
            recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
            recovery_action={
                "required_change": "select_intended_filesystem_scope",
                "same_call_safe": False,
            },
        )
        restored = ToolResult.from_data(original.to_data())
        self.assertEqual(restored, original)
        self.assertFalse(restored.recovery_action["same_call_safe"])

    def test_success_does_not_pay_context_cost_for_empty_recovery(self) -> None:
        data = ToolResult("ok", True, data={"value": 1}).to_data()
        self.assertNotIn("recovery_kind", data)
        self.assertNotIn("recovery_action", data)

    def test_unknown_outcome_is_never_plain_retryable(self) -> None:
        result = ToolResult(
            "unknown", False, error_code="UNKNOWN_OUTCOME",
            recovery_kind=ToolRecoveryKind.UNKNOWN_OUTCOME,
        )
        self.assertFalse(result.retryable)
        self.assertEqual(
            result.effective_recovery_kind, ToolRecoveryKind.UNKNOWN_OUTCOME
        )


if __name__ == "__main__":
    unittest.main()
