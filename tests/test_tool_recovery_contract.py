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

    def test_multiple_recoverable_tool_results_form_one_batch(self) -> None:
        from tsm_agt.core.kernel import Kernel
        from tsm_agt.ports import (
            Message, MessageRole, TextBlock, ToolCall, ToolCallBlock,
            ToolResultBlock,
        )

        messages = (
            Message("call-batch", MessageRole.ASSISTANT, (ToolCallBlock(
                ToolCall("read", "core.read_file", {"path": "app.py"}),
            ), ToolCallBlock(
                ToolCall("patch", "core.apply_patch", {"path": "app.py"}),
            ))),
            Message("result-read", MessageRole.TOOL, (ToolResultBlock(
                ToolResult("read", False, error_code="TIMEOUT", retryable=True),
            ),)),
            Message("result-patch", MessageRole.TOOL, (ToolResultBlock(
                ToolResult(
                    "patch", False, error_code="CONFLICT",
                    recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
                ),
            ),)),
        )

        self.assertEqual(
            [item.call_id for item in Kernel._latest_recoverable_tool_batch(messages)],
            ["read", "patch"],
        )

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
