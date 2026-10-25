"""Real stdio MCP coverage from configuration through a model tool call."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.mcp_python_sdk_client import create_mcp_sdk_client
from tsm_agt.adapters.mcp_tool_provider import MCPToolProvider
from tsm_agt.bootstrap import (
    compose_fixture_application,
    compose_openai_compatible_engineering_application_from_env,
)
from tsm_agt.core import AgentTurnResult, AgentTurnSuspended, ApprovalDecision
from tsm_agt.ports import MCPClientManager, MCPServerConfig, ToolResultBlock
from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response


def _demo_server() -> MCPServerConfig:
    return MCPServerConfig(
        id="demo",
        kind="executable",
        command=("python", "-m", "tsm_agt.adapters.mcp_demo_server_stdio"),
    )


class MCPSDKIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_mcp_is_advertised_to_model_and_invoked_after_approval(self):
        model = ScriptedModel([
            tool_response("mcp-call", "call-add", "mcp.demo.add", {"a": 2, "b": 5}),
            text_response("mcp-finished", "MCP returned 7."),
        ])
        provider = MCPToolProvider(MCPClientManager(
            (_demo_server(),), create_mcp_sdk_client,
        ))
        application = compose_fixture_application(
            model_adapter=model, tool_adapters=(provider,),
        )
        await application.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                outcome = await application.kernel.run_agent_turn(
                    task.task_id, "Add 2 and 5 using the configured MCP server.",
                )
                self.assertIsInstance(outcome, AgentTurnSuspended)
                assert isinstance(outcome, AgentTurnSuspended)
                self.assertIn(
                    "mcp.demo.add", [tool.name for tool in model.requests[0].tools],
                )

                outcome = await application.kernel.resolve_agent_approval(
                    outcome.approval_request_id, ApprovalDecision.APPROVE,
                    "approve configured MCP demo call",
                )
                self.assertIsInstance(outcome, AgentTurnResult)
                assert isinstance(outcome, AgentTurnResult)
                self.assertEqual(outcome.assistant_message.text, "MCP returned 7.")

                results = [
                    block.result for message in model.requests[-1].messages
                    for block in message.content if isinstance(block, ToolResultBlock)
                ]
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].ok)
                self.assertEqual(results[0].data["structuredContent"]["result"], 7)
                # SDK Pydantic objects must never leak into durable tool data.
                json.dumps(results[0].to_data())
        finally:
            await application.registry.stop_all()

    async def test_production_composition_loads_workspace_mcp_config(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            env_file = workspace / ".env"
            env_file.write_text(
                "TSM_AGT_MODEL=open\n"
                "TSM_AGT_MODEL_BASE_URL=http://127.0.0.1:5580\n"
                "TSM_AGT_OPEN_MODEL=test-model\n"
                "TSM_AGT_MODEL_API_KEY=test-secret\n",
                encoding="utf-8",
            )
            config_dir = workspace / ".tsm-agt"
            config_dir.mkdir()
            (config_dir / "mcp.json").write_text(json.dumps({
                "mcpServers": {
                    "demo": {
                        "kind": "executable",
                        "command": [
                            "python", "-m", "tsm_agt.adapters.mcp_demo_server_stdio",
                        ],
                    },
                },
            }), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                application = compose_openai_compatible_engineering_application_from_env(
                    env_file, database_path=workspace / ".agent" / "runtime.db",
                )
            await application.start()
            try:
                names = [tool.name for tool in await application.kernel.list_tools()]
                self.assertIn("mcp.demo.echo", names)
                self.assertIn("mcp.demo.add", names)
            finally:
                await application.registry.stop_all()

    async def test_sdk_session_can_be_closed_after_direct_round_trip(self):
        manager = MCPClientManager((_demo_server(),), create_mcp_sdk_client)
        client = await manager.get_client("demo")
        try:
            tools = await client.list_tools()
            self.assertEqual({tool.name for tool in tools}, {"echo", "add"})
            result = await client.call_tool("echo", {"message": "hello"})
            self.assertFalse(result.isError)
        finally:
            await manager.aclose()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await manager.get_client("demo")


if __name__ == "__main__":
    unittest.main()
