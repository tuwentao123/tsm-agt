"""Compare model-visible protocol with and without an approval suspension."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import AgentTurnResult, AgentTurnSuspended, ApprovalDecision, TaskState
from tsm_agt.ports import (
    AdapterDescriptor, HealthState, HealthStatus, MessageRole, ToolIdempotency,
    ToolResult, ToolResultBlock, ToolRisk, ToolSpec,
)
from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response


class ChangeProvider:
    descriptor = AdapterDescriptor("fixture.change", "1.0", "ToolProviderPort", "1.0")

    def __init__(self, risk):
        self.risk = risk
        self.calls = []

    async def start(self, context):
        pass

    async def stop(self, deadline):
        pass

    async def health(self):
        return HealthStatus(HealthState.HEALTHY)

    async def list_tools(self):
        return (ToolSpec(
            "fixture.change", "Apply one simulated change.",
            {"type": "object", "properties": {"item": {"type": "string"}},
             "required": ["item"], "additionalProperties": False},
            self.risk, self.risk is ToolRisk.R0, False, ToolIdempotency.NON_IDEMPOTENT,
        ),)

    async def invoke(self, call, context):
        self.calls.append(call.arguments["item"])
        return ToolResult(call.call_id, True, data={"changed": call.arguments["item"]})


class ApprovalContextContinuityTest(unittest.IsolatedAsyncioTestCase):
    async def _run_case(self, risk):
        model = ScriptedModel([
            text_response("prior", "I can implement change A and change B."),
            tool_response("first", "change-a", "fixture.change", {"item": "A"}),
            tool_response("second", "change-b", "fixture.change", {"item": "B"}),
            text_response("done", "Both changes were made."),
        ])
        provider = ChangeProvider(risk)
        app = compose_fixture_application(model_adapter=model, tool_adapters=(provider,))
        await app.registry.start_all()
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(app, Path(directory))
                await app.kernel.run_agent_turn(task.task_id, "What changes are needed?")
                await app.kernel.transition_task(task.task_id, TaskState.EXECUTING, "follow-up")
                outcome = await app.kernel.run_agent_turn(task.task_id, "Implement both changes.")
                approvals = 0
                while isinstance(outcome, AgentTurnSuspended):
                    approvals += 1
                    outcome = await app.kernel.resolve_agent_approval(
                        outcome.approval_request_id, ApprovalDecision.APPROVE, "approved"
                    )
                self.assertIsInstance(outcome, AgentTurnResult)
                self.assertEqual(provider.calls, ["A", "B"])
                request = model.requests[2]
                history = next(message for message in request.messages
                               if message.message_id.startswith("session-context-"))
                history_data = json.loads(history.text)
                self.assertTrue(any(row.get("text") == "I can implement change A and change B."
                                    for row in history_data["recent_messages"]))
                self.assertTrue(any(message.role is MessageRole.USER and
                                    message.text == "Implement both changes."
                                    for message in request.messages))
                results = [block.result for message in request.messages
                           for block in message.content if isinstance(block, ToolResultBlock)]
                self.assertEqual([(result.call_id, result.ok, result.data.get("changed"))
                                  for result in results], [("change-a", True, "A")])
                self.assertIn("fixture.change", [tool.name for tool in request.tools])
                return approvals
        finally:
            await app.registry.stop_all()

    async def test_context_survives_with_and_without_approval(self):
        self.assertEqual(await self._run_case(ToolRisk.R0), 0)
        self.assertEqual(await self._run_case(ToolRisk.R1), 2)
