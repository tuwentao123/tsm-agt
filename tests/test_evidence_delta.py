from __future__ import annotations

import unittest

from tsm_agt.adapters.structured_evidence import StructuredEvidenceDeltaEvaluator
from tsm_agt.ports import AdapterContext, EvidenceInventory, ToolCall, ToolResult


class StructuredEvidenceDeltaEvaluatorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.evaluator = StructuredEvidenceDeltaEvaluator()
        await self.evaluator.start(AdapterContext({}, lambda *_: None))

    async def test_structured_results_are_deduplicated_by_call_fingerprint(self):
        call = ToolCall(
            "search-1", "core.search_text", {"query": "Kernel", "path": "src"},
        )
        result = ToolResult(call.call_id, True, data={
            "path": "src", "matches": [{"path": "src/a.py", "line": 3}],
        })
        first = await self.evaluator.evaluate(call, result, EvidenceInventory())
        second = await self.evaluator.evaluate(call, result, first.inventory)
        self.assertTrue(first.delta.question_id)
        self.assertEqual(first.delta.counts["new_paths"], 2)
        self.assertEqual(second.delta.total_new, 0)

    async def test_pagination_and_line_range_change_fingerprint(self):
        first = ToolCall("read-1", "core.read_file", {
            "path": "secret.txt", "start_line": 1, "max_lines": 2,
        })
        second = ToolCall("read-2", "core.read_file", {
            "path": "secret.txt", "start_line": 3, "max_lines": 2,
        })
        self.assertNotEqual(
            self.evaluator._call_fingerprint(first),
            self.evaluator._call_fingerprint(second),
        )

    async def test_recoverable_failure_preserves_zero_delta_streak(self):
        call = ToolCall("recover-path", "core.read_file", {"path": "src/a.py"})
        result = ToolResult(
            call.call_id, False, error_code="PATH_CONTEXT_REQUIRED",
            meta={"recoverable_input": True},
        )
        evaluation = await self.evaluator.evaluate(
            call, result, EvidenceInventory(consecutive_zero_delta=2)
        )
        self.assertEqual(evaluation.delta.consecutive_zero_delta, 2)
        self.assertEqual(evaluation.delta.result_status, "recoverable:PATH_CONTEXT_REQUIRED")


if __name__ == "__main__":
    unittest.main()
