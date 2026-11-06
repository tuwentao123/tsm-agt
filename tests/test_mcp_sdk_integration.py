"""Real stdio MCP coverage from configuration through a model tool call."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.mcp_python_sdk_client import create_mcp_sdk_client
from tsm_agt.adapters.mcp_tool_provider import MCPToolProvider
from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.bootstrap import (
    compose_fixture_application,
    compose_openai_compatible_engineering_application_from_env,
)
from tsm_agt.core import (
    AgentClarificationSuspended, AgentTurnResult, AgentTurnSuspended,
    ApprovalDecision, ApprovalRequired, ToolCommitState,
)
from tsm_agt.ports import (
    MCPClientManager, MCPServerConfig, RuntimeStorePort,
    ToolCall, ToolResultBlock,
)
from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response


def _demo_server() -> MCPServerConfig:
    return MCPServerConfig(
        id="demo", required=True,
        kind="executable",
        command=("python", "-m", "tsm_agt.adapters.mcp_demo_server_stdio"),
    )


def _p0_server(*args: str) -> MCPServerConfig:
    return MCPServerConfig(
        id="p0", required=True, kind="executable",
        command=(sys.executable, str(Path(__file__).parent / "fixtures" / "mcp_p0_server.py"), *args),
    )


class MCPSDKIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_cross_task_close_and_single_flight_connection(self):
        manager = MCPClientManager((_demo_server(),), create_mcp_sdk_client)
        async with asyncio.timeout(10):
            clients = await asyncio.gather(*(manager.get_client("demo") for _ in range(3)))
            try:
                self.assertTrue(all(client is clients[0] for client in clients))
                result = await asyncio.create_task(clients[0].call_tool("add", {"a": 2, "b": 3}))
                self.assertEqual(result.structuredContent["result"], 5)
            finally:
                await asyncio.gather(manager.aclose(), manager.aclose())

    async def test_real_stdio_discovers_second_page_and_keeps_explicit_tool_error(self):
        manager = MCPClientManager((_p0_server(),), create_mcp_sdk_client)
        provider = MCPToolProvider(manager)
        application = compose_fixture_application(tool_adapters=(provider,))
        async with asyncio.timeout(10):
            await application.start()
            try:
                names = {tool.name for tool in await provider.list_tools()}
                self.assertEqual(names, {"core.mcp_discover", *(f"mcp.p0.{name}" for name in ("echo", "write", "fail", "bad_output"))})
                with tempfile.TemporaryDirectory() as directory:
                    task = await _executing(application, Path(directory))
                    call = ToolCall("fail", "mcp.p0.fail", {})
                    with self.assertRaises(ApprovalRequired) as raised:
                        await application.kernel.invoke_tool(task.task_id, "turn", call)
                    request = raised.exception.request
                    result = await application.kernel.resolve_approval(
                        task.task_id, request.request_id, request.payload_hash,
                        ApprovalDecision.APPROVE, "approve local fixture",
                    )
                    self.assertEqual(result.error_code, "MCP_TOOL_ERROR")
                    execution = (await application.kernel.get_task(task.task_id)).tool_executions["turn:fail"]
                    self.assertEqual(execution.state, ToolCommitState.FAILED)
            finally:
                await application.registry.stop_all()

    async def test_applied_write_with_lost_or_invalid_response_is_unknown_and_not_replayed(self):
        for tool in ("write", "bad_output"):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as directory:
                receipt = Path(directory) / "receipt"
                provider = MCPToolProvider(MCPClientManager(
                    (_p0_server("--receipt", str(receipt)),), create_mcp_sdk_client,
                ))
                model = ScriptedModel([
                    tool_response("writes", "apply-once", f"mcp.p0.{tool}", {}),
                    tool_response("inspect", "observe-state", "core.read_file", {"path": "receipt"}),
                    text_response("observed", "The receipt records the write."),
                ])
                application = compose_fixture_application(model_adapter=model,
                    tool_adapters=(provider, CoreReadOnlyToolProvider()))
                async with asyncio.timeout(12):
                    await application.start()
                    try:
                        task = await _executing(application, Path(directory))
                        approval = await application.kernel.run_agent_turn(task.task_id, "Use the local MCP fixture.")
                        self.assertIsInstance(approval, AgentTurnSuspended)
                        outcome = await application.kernel.resolve_agent_approval(
                            approval.approval_request_id, ApprovalDecision.APPROVE, "approve local write once",
                        )
                        self.assertIsInstance(outcome, AgentTurnResult)
                        self.assertEqual(len(model.requests), 3)
                        self.assertTrue(any(isinstance(block, ToolResultBlock)
                            and block.result.error_code == "UNKNOWN_OUTCOME"
                            for message in model.requests[1].messages for block in message.content))
                        live = await application.kernel.get_task(task.task_id)
                        self.assertIsNone(live.pending_clarification)
                        execution = next(iter((await application.kernel.get_task(task.task_id)).tool_executions.values()))
                        self.assertEqual(execution.state, ToolCommitState.UNKNOWN_OUTCOME)
                        self.assertEqual(execution.result.error_code, "UNKNOWN_OUTCOME")
                        self.assertFalse(execution.result.retryable)
                        self.assertEqual(receipt.read_text(), f"{tool}\n")
                        # Recovery must return the recorded uncertainty without
                        # another RPC, even after a server process has exited.
                        reused = await application.kernel.recover_tool_execution(
                            task.task_id, execution.turn_id, execution.call.call_id,
                        )
                        self.assertEqual(reused.error_code, "UNKNOWN_OUTCOME")
                        self.assertEqual(receipt.read_text(), f"{tool}\n")
                        events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
                        failure = next(event for event in events if event.event_type == "tool.failed")
                        self.assertEqual(failure.payload["commit_state"], "UNKNOWN_OUTCOME")
                        self.assertFalse(any(event.event_type == "clarification.requested" for event in events))
                    finally:
                        await application.registry.stop_all()

    @unittest.skipUnless(os.name == "posix", "process liveness check uses POSIX pid semantics")
    async def test_cancel_during_real_stdio_initialize_reaps_child(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "pid"
            async with asyncio.timeout(12):
                starting = asyncio.create_task(create_mcp_sdk_client(
                    _p0_server("--pid", str(pid_path), "--stall"),
                ))
                try:
                    while not pid_path.exists():
                        if starting.done():
                            await starting
                        await asyncio.sleep(0.01)
                    pid = int(pid_path.read_text())
                finally:
                    starting.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await starting
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

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
            configuration_file = workspace / "config.yaml"
            configuration_file.write_text(
                '''
version: 1
model:
  default_profile: primary
  providers:
    local:
      protocol: openai_chat
      base_url: http://127.0.0.1:5580
      auth:
        api_key: test-secret
  profiles:
    primary:
      provider: local
      model: test-model
''',
                encoding="utf-8",
            )
            with configuration_file.open("a", encoding="utf-8") as stream:
                stream.write("\nmcp:\n  servers:\n    demo:\n      required: true\n      kind: executable\n      command: [python, -m, tsm_agt.adapters.mcp_demo_server_stdio]\n")
            with patch.dict(os.environ, {}, clear=True):
                application = compose_openai_compatible_engineering_application_from_env(
                    configuration_file, database_path=workspace / ".agent" / "runtime.db",
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
