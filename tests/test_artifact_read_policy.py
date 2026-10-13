from __future__ import annotations

import unittest
from datetime import datetime

from tsm_agt.adapters.rule_based_artifact_read import RuleBasedArtifactReadPolicy
from tsm_agt.ports import ArtifactReadAction, ArtifactReadProbe, ArtifactReadState, ToolCall, ToolResult


def read_call(call_id: str, start_line: int = 1, max_lines: int = 2) -> ToolCall:
    return ToolCall(call_id, "core.read_file", {
        "path": "src/target.py", "start_line": start_line, "max_lines": max_lines,
    })


class RuleBasedArtifactReadPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.policy = RuleBasedArtifactReadPolicy()
        await self.policy.start(None)

    async def asyncTearDown(self) -> None:
        await self.policy.stop(datetime.now())

    async def _record(self) -> ArtifactReadState:
        call = read_call("read-1")
        update = await self.policy.after_result(
            call,
            ToolResult(call.call_id, True, {
                "path": "src/target.py", "sha256": "content-v1",
                "start_line": 1, "end_line": 2, "total_lines": 6,
            }),
            ArtifactReadProbe("path-hash", "", 1, 2), ArtifactReadState(),
        )
        return update.state

    async def test_same_tool_and_range_requires_reuse(self):
        state = await self._record()
        decision = await self.policy.before_call(
            read_call("read-2"), ArtifactReadProbe("path-hash", "content-v1", 1, 2), state,
        )
        self.assertEqual(decision.action, ArtifactReadAction.REQUIRE_REUSE)

    async def test_changed_range_has_distinct_fingerprint_and_is_allowed(self):
        state = await self._record()
        decision = await self.policy.before_call(
            read_call("read-3", start_line=3),
            ArtifactReadProbe("path-hash", "content-v1", 3, 2), state,
        )
        self.assertEqual(decision.action, ArtifactReadAction.ALLOW)
        self.assertEqual(decision.reason, "new_call_fingerprint")


if __name__ == "__main__":
    unittest.main()
