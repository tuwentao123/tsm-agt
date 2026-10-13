from __future__ import annotations

import unittest

from tsm_agt.adapters.evidence_guided import BuiltinEvidenceRelationProvider
from tsm_agt.ports import EvidenceInventory, EvidenceRelationState, ToolCall


class EvidenceRelationRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_call_fingerprint_is_json_encoded_at_runtime(self) -> None:
        provider = BuiltinEvidenceRelationProvider()
        await provider.start(None)
        try:
            relations = await provider.discover(
                ToolCall("call-1", "web.search", {"query": "codex source"}),
                None,
                EvidenceRelationState(),
                EvidenceInventory(),
            )
        finally:
            await provider.stop(None)

        self.assertEqual(len(relations), 1)
        self.assertIn('"tool_name":"web.search"', relations[0].subject_hash)


if __name__ == "__main__":
    unittest.main()
