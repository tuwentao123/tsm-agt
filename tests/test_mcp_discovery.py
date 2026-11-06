"""First-request visibility and late MCP registration through the real loop."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from tsm_agt.adapters.mcp_discovery import DISCOVERY_TOOL
from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.mcp_python_sdk_client import create_mcp_sdk_client
from tsm_agt.adapters.mcp_tool_provider import MCPToolProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.mcp_configuration import load_mcp_servers_from_configuration
from tsm_agt.core import AgentTurnSuspended, ApprovalDecision
from tsm_agt.ports import (
    AdapterContext, MCPClientManager, MCPServerConfig,
    ToolCall, ToolCallBlock, ToolResultBlock,
)
from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response


class Client:
    def __init__(self, count=1):
        self.count = count
        self.list_calls = 0

    async def list_tools(self):
        self.list_calls += 1
        return [SimpleNamespace(
            name=f"report_{index}", description=f"Read dashboard number {index}",
            inputSchema={"type": "object"},
        ) for index in range(self.count)]

    async def aclose(self):
        pass


class MCPDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def make_provider(self, client, *, gate=None, config=None):
        async def factory(_config):
            if gate is not None:
                await gate.wait()
            return client

        provider = MCPToolProvider(MCPClientManager((config or MCPServerConfig(
            "reports", "custom", description="Query data dashboards",
        ),), factory))
        await provider.start(AdapterContext({}, lambda *_: None))
        self.addAsyncCleanup(provider.stop, datetime.now(timezone.utc))
        return provider

    async def test_initial_request_advertises_connecting_server_without_credentials(self):
        gate = asyncio.Event()
        config = MCPServerConfig(
            "reports", "http", url="https://example.invalid/mcp?ACCESS_KEY=private-test-key",
            options={"headers": {"Authorization": "private-header"}},
            description="Query data dashboards",
        )
        provider = await self.make_provider(Client(), gate=gate, config=config)
        model = ScriptedModel([text_response("done", "Ready to help.")])
        app = compose_fixture_application(model_adapter=model, tool_adapters=(provider,))
        await app.start()
        self.addAsyncCleanup(app.registry.stop_all)
        with tempfile.TemporaryDirectory() as directory:
            task = await _executing(app, Path(directory))
            await app.kernel.run_agent_turn(task.task_id, "What can you query?")
        request = model.requests[0]
        self.assertIn(DISCOVERY_TOOL, {tool.name for tool in request.tools})
        context = next(message.text for message in request.messages if "Available MCP Servers" in message.text)
        self.assertIn('"state": "connecting"', context)
        self.assertIn("Query data dashboards", context)
        serialized = context + json.dumps([spec.to_data() for spec in request.tools])
        for private in ("example.invalid", "ACCESS_KEY", "private-test-key", "private-header"):
            self.assertNotIn(private, serialized)

    async def test_wait_timeout_and_cancellation_do_not_cancel_initialization(self):
        gate = asyncio.Event()
        provider = await self.make_provider(Client(), gate=gate)
        result = await provider.invoke(ToolCall("observe", DISCOVERY_TOOL, {"wait_seconds": 0.01}), None)
        self.assertEqual(result.data["entries"][0]["state"], "connecting")
        waiter = asyncio.create_task(provider.invoke(ToolCall("wait", DISCOVERY_TOOL, {"wait_seconds": 5}), None))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(provider._startup_tasks["reports"].done())
        gate.set()
        await provider.wait_ready()
        result = await provider.invoke(ToolCall("observe-again", DISCOVERY_TOOL, {}), None)
        self.assertEqual(result.data["entries"][0]["state"], "ready")

    async def test_pagination_query_and_native_purpose_use_cached_catalog(self):
        client = Client(count=52)
        provider = await self.make_provider(client)
        await provider.wait_ready()
        entries, offset = [], 0
        while offset is not None:
            result = await provider.invoke(ToolCall(f"page-{offset}", DISCOVERY_TOOL, {
                "server_id": "reports", "offset": offset, "limit": 7,
            }), None)
            self.assertLessEqual(len(result.data["entries"]), 7)
            entries.extend(result.data["entries"])
            offset = result.data["next_offset"]
        self.assertEqual(len(entries), 53)
        result = await provider.invoke(ToolCall("search", DISCOVERY_TOOL, {"query": "dashboard number 51"}), None)
        self.assertEqual([entry["name"] for entry in result.data["entries"]], ["mcp.reports.report_51"])
        self.assertEqual(client.list_calls, 1)
        spec = next(spec for spec in await provider.list_tools() if spec.name == "mcp.reports.report_51")
        self.assertIn("Read dashboard number 51", spec.model_description())
        unknown = await provider.invoke(ToolCall("unknown", DISCOVERY_TOOL, {"server_id": "absent"}), None)
        self.assertEqual(unknown.error_code, "MCP_SERVER_NOT_CONFIGURED")

    async def test_failed_optional_server_remains_discoverable_with_safe_reason(self):
        async def factory(_config):
            raise ConnectionError("https://private.invalid/?ACCESS_KEY=secret")

        provider = MCPToolProvider(MCPClientManager((MCPServerConfig("failed", "custom"),), factory))
        await provider.start(AdapterContext({}, lambda *_: None))
        self.addAsyncCleanup(provider.stop, datetime.now(timezone.utc))
        await provider.wait_ready()
        result = await provider.invoke(ToolCall("state", DISCOVERY_TOOL, {}), None)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["entries"][0]["state"], "failed")
        self.assertIn("ConnectionError", result.data["entries"][0]["detail"])
        self.assertNotIn("private.invalid", json.dumps(result.to_data()))

    async def test_discovery_refreshes_even_when_another_batch_result_is_last(self):
        gate = asyncio.Event()
        provider = await self.make_provider(Client(), gate=gate)
        discovery = tool_response("batch", "discover", DISCOVERY_TOOL, {})
        discovery = replace(discovery, message=replace(discovery.message, content=(
            *discovery.message.content,
            ToolCallBlock(ToolCall("files", "core.list_files", {"path": "."})),
        )))

        class LateModel(ScriptedModel):
            async def complete(self, request):
                if not self.requests:
                    gate.set()
                    await provider.wait_ready()
                return await super().complete(request)

        model = LateModel([discovery, text_response("done", "Tools discovered.")])
        app = compose_fixture_application(
            model_adapter=model, tool_adapters=(provider, CoreReadOnlyToolProvider()),
        )
        await app.start()
        self.addAsyncCleanup(app.registry.stop_all)
        with tempfile.TemporaryDirectory() as directory:
            task = await _executing(app, Path(directory))
            await app.kernel.run_agent_turn(task.task_id, "Inspect available tools and files")
        self.assertNotIn("mcp.reports.report_0", {spec.name for spec in model.requests[0].tools})
        self.assertIn("mcp.reports.report_0", {spec.name for spec in model.requests[1].tools})
        results = [block.result.call_id for message in model.requests[1].messages
                   for block in message.content if isinstance(block, ToolResultBlock)]
        self.assertEqual(results[-2:], ["discover", "files"])

    async def test_real_stdio_late_registration_calls_in_same_turn_and_resumes_after_restart(self):
        gate = asyncio.Event()
        config = MCPServerConfig(
            "demo", "executable", description="Local arithmetic tools",
            command=(sys.executable, "-m", "tsm_agt.adapters.mcp_demo_server_stdio"),
        )

        async def factory(config):
            await gate.wait()
            return await create_mcp_sdk_client(config)

        provider = MCPToolProvider(MCPClientManager((config,), factory))

        class LateModel(ScriptedModel):
            async def complete(self, request):
                if not self.requests:
                    gate.set()
                    await provider.wait_ready()
                return await super().complete(request)

        model = LateModel([
            tool_response("discover", "discover", DISCOVERY_TOOL, {"server_id": "demo"}),
            tool_response("add", "add", "mcp.demo.add", {"a": 2, "b": 5}),
        ])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=(provider,), store_adapter=SQLiteRuntimeStore(database),
            )
            async with asyncio.timeout(15):
                await app.start()
                try:
                    task = await _executing(app, root)
                    outcome = await app.kernel.run_agent_turn(task.task_id, "Add 2 and 5 using configured tools")
                    self.assertIsInstance(outcome, AgentTurnSuspended)
                    self.assertNotIn("mcp.demo.add", {spec.name for spec in model.requests[0].tools})
                    self.assertIn("mcp.demo.add", {spec.name for spec in model.requests[1].tools})
                    events = await app.kernel.dependencies.store.read_events(task.task_id)
                    refresh = next(event for event in events if event.event_type == "tools.catalog_refreshed")
                    self.assertIn("mcp.demo.add", refresh.payload["added_tools"])
                    self.assertTrue(any(event.event_type == "config.snapshot" and event.payload.get("reason") == "tool_catalog_refresh" for event in events))
                finally:
                    await app.registry.stop_all()

                resumed_model = ScriptedModel([text_response("done", "MCP returned 7.")])
                resumed_provider = MCPToolProvider(MCPClientManager((config,), create_mcp_sdk_client))
                resumed = compose_fixture_application(
                    model_adapter=resumed_model, tool_adapters=(resumed_provider,),
                    store_adapter=SQLiteRuntimeStore(database),
                )
                await resumed.start()
                try:
                    await resumed_provider.wait_ready()
                    result = await resumed.kernel.resolve_agent_approval(
                        outcome.approval_request_id, ApprovalDecision.APPROVE, "approve local fixture",
                    )
                    self.assertEqual(result.assistant_message.text, "MCP returned 7.")
                    tool_results = [block.result for message in resumed_model.requests[0].messages
                                    for block in message.content if isinstance(block, ToolResultBlock)]
                    addition = next(result for result in tool_results if result.call_id == "add")
                    self.assertTrue(addition.ok)
                    self.assertEqual(addition.data["structuredContent"]["result"], 7)
                finally:
                    await resumed.registry.stop_all()

    def test_optional_description_loads_without_transport_inference(self):
        _, configs = load_mcp_servers_from_configuration({"mcp": {"servers": {
            "reports": {"kind": "http", "description": "Query dashboards"},
        }}})
        self.assertEqual(configs[0].description, "Query dashboards")
