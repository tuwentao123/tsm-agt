"""Exploration heuristics must not replace an I/O invocation's deadline."""
import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.evidence_guided import (
    BuiltinEvidenceRelationProvider, BoundedRejectionLoopPolicy,
    RuleBasedEvidenceRelationPolicy, RuleBasedExplorationOutcomePolicy,
)
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import AgentTurnResult, ToolCommitState
from tsm_agt.core.exploration_coordinator import ExplorationCoordinator, ExplorationCoordinatorAction
from tsm_agt.ports import EvidenceInventory, EvidenceRelationState, RejectionLoopState, ToolCall
from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response


class SlowFileReader(CoreReadOnlyToolProvider):
    async def invoke(self, call, context):
        # Simulate a slow filesystem while yielding to the loop. This must
        # survive the former 10-second exploration deadline without retries.
        await asyncio.sleep(10.1)
        return await super().invoke(call, context)


class FileReadDeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_probe_keeps_configured_deadline_and_returns_one_read(self):
        call = ToolCall("read", "core.read_file", {"path": "README.md", "max_lines": 160})
        model = ScriptedModel([
            tool_response("read-call", call.call_id, call.name, dict(call.arguments)),
            text_response("answer", "Read completed."),
        ])
        app = compose_fixture_application(model_adapter=model, tool_adapters=(SlowFileReader(),))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("A delayed filesystem read.\n")
            await app.start()
            adapters = (BuiltinEvidenceRelationProvider(), RuleBasedEvidenceRelationPolicy(),
                        BoundedRejectionLoopPolicy(), RuleBasedExplorationOutcomePolicy())
            for adapter in adapters:
                await adapter.start(None)
            try:
                task = await _executing(app, root)
                coordinator = ExplorationCoordinator((adapters[0],), *adapters[1:])
                decision = await coordinator.before_call(call, None,
                    EvidenceRelationState(("Q1",), (), 2), RejectionLoopState(), EvidenceInventory())
                self.assertEqual(decision.action, ExplorationCoordinatorAction.ALLOW_BOUNDED_PROBE)
                with patch.object(app.kernel, "_exploration_coordinator", coordinator), \
                        patch.object(coordinator, "before_call", new=AsyncMock(return_value=decision)):
                    result = await app.kernel.run_agent_turn(task.task_id, "Inspect implementation", tool_timeout_seconds=30.0)
                self.assertIsInstance(result, AgentTurnResult)
                live = await app.kernel.get_task(task.task_id)
                self.assertEqual(len(live.tool_executions), 1)
                execution = live.tool_executions[f"{result.turn_id}:read"]
                self.assertEqual(execution.state, ToolCommitState.COMMITTED)
                self.assertTrue(execution.result.ok)
                self.assertIn("A delayed filesystem read.", execution.result.data["content"])
            finally:
                for adapter in reversed(adapters):
                    await adapter.stop(datetime.now())
                await app.registry.stop_all()
