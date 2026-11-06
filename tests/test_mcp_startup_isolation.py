"""Optional MCP startup cannot gate core chat or healthy MCP peers."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import anyio
import httpx

from tsm_agt.adapters.mcp_errors import mcp_failure_detail
from tsm_agt.adapters.mcp_tool_provider import MCPToolProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.mcp_configuration import load_mcp_servers_from_configuration
from tsm_agt.ports import (
    AdapterContext, HealthState, MCPClientManager, MCPServerConfig, MCPServerStartupError,
)
from tsm_agt.web import app as web
from tests.test_conversation_loop import ScriptedModel, _executing, text_response


class Client:
    def __init__(self, *, invalid=False):
        self.closed = 0
        self.invalid = invalid

    async def list_tools(self):
        good = SimpleNamespace(name="echo", inputSchema={"type": "object"})
        return [good, SimpleNamespace(name="bad", inputSchema={})] if self.invalid else [good]

    async def aclose(self):
        self.closed += 1


CONTEXT = AdapterContext({}, lambda *_: None)


class MCPStartupIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_completes_while_optional_server_is_still_initializing(self):
        gate = asyncio.Event()
        entered = asyncio.Event()
        client = Client()

        async def factory(config):
            entered.set()
            await gate.wait()
            return client

        provider = MCPToolProvider(MCPClientManager((MCPServerConfig("slow", "custom"),), factory))
        model = ScriptedModel([text_response("chat", "Chat works before MCP is ready.")])
        application = compose_fixture_application(model_adapter=model, tool_adapters=(provider,))
        async with asyncio.timeout(3):
            await application.start()
            try:
                await entered.wait()
                self.assertEqual(provider.startup_status()[0]["state"], "connecting")
                with tempfile.TemporaryDirectory() as root:
                    task = await _executing(application, Path(root))
                    result = await application.kernel.run_agent_turn(task.task_id, "Hello")
                self.assertEqual(result.assistant_message.text, "Chat works before MCP is ready.")
                self.assertFalse(any(spec.name.startswith("mcp.") for spec in model.requests[0].tools))
                gate.set()
                await provider.wait_ready()
                self.assertIn("mcp.slow.echo", {spec.name for spec in await application.kernel.list_tools()})
            finally:
                await application.registry.stop_all()
        self.assertEqual(client.closed, 1)

    async def test_failed_server_does_not_hide_healthy_peer_or_publish_partial_catalog(self):
        clients = {"good": Client(), "bad": Client(invalid=True)}

        async def factory(config):
            return clients[config.id]

        provider = MCPToolProvider(MCPClientManager(
            tuple(MCPServerConfig(name, "custom") for name in clients), factory,
        ))
        await provider.start(CONTEXT)
        try:
            await provider.wait_ready()
            self.assertEqual([spec.name for spec in await provider.list_tools()], ["core.mcp_discover", "mcp.good.echo"])
            states = {row["id"]: row["state"] for row in provider.startup_status()}
            self.assertEqual(states, {"good": "ready", "bad": "failed"})
            self.assertEqual((await provider.health()).state, HealthState.DEGRADED)
            self.assertEqual(clients["bad"].closed, 1)
        finally:
            await provider.stop(datetime.now(timezone.utc))
        self.assertTrue(all(client.closed == 1 for client in clients.values()))

    async def test_timeout_and_stop_join_initialization_cleanup(self):
        for stop_early in (False, True):
            with self.subTest(stop_early=stop_early):
                entered = asyncio.Event()
                cleaned = asyncio.Event()

                async def factory(config):
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        await asyncio.sleep(0)
                        cleaned.set()

                provider = MCPToolProvider(MCPClientManager((MCPServerConfig(
                    "slow", "custom", startup_timeout_seconds=0.03,
                ),), factory))
                await provider.start(CONTEXT)
                await entered.wait()
                if not stop_early:
                    await provider.wait_ready()
                    status = provider.startup_status()[0]
                    self.assertEqual(status["state"], "failed")
                    self.assertIn("timeout", status["detail"])
                await provider.stop(datetime.now(timezone.utc))
                self.assertTrue(cleaned.is_set())
                self.assertTrue(all(task.done() for task in provider._startup_tasks.values()))

    async def test_explicit_required_failure_still_blocks_start_and_cleans_peers(self):
        client = Client()

        async def factory(config):
            if config.id == "required":
                await asyncio.sleep(0)
                raise ConnectionError("private remote credential")
            return client

        provider = MCPToolProvider(MCPClientManager((
            MCPServerConfig("good", "custom"),
            MCPServerConfig("required", "custom", required=True),
        ), factory))
        with self.assertRaises(MCPServerStartupError) as caught:
            await provider.start(CONTEXT)
        self.assertIn("required", str(caught.exception))
        self.assertNotIn("private remote credential", str(caught.exception))
        self.assertEqual(client.closed, 1)
        await provider.stop(datetime.now(timezone.utc))
        self.assertEqual(client.closed, 1)

    async def test_diagnostics_observe_running_instance_without_extra_connections(self):
        attempts = 0

        async def factory(config):
            nonlocal attempts
            attempts += 1
            raise ConnectionError("private remote credential")

        provider = MCPToolProvider(MCPClientManager((MCPServerConfig("bad", "custom"),), factory))
        application = compose_fixture_application(tool_adapters=(provider,))
        await application.start()
        await provider.wait_ready()

        # Real LocalEventApi._call dispatches to its owner loop; this test is
        # already running on that loop, so await the submitted coroutine here.
        async def call(awaitable):
            return await awaitable

        async def inline_to_thread(function, awaitable):
            return await function(awaitable)

        server = SimpleNamespace(_client=SimpleNamespace(application=application), _call=call)
        try:
            with patch.object(web, "runtime_server", server), patch.object(web, "runtime_error", None), \
                 patch.object(web.asyncio, "to_thread", inline_to_thread):
                result = await web.diagnostics()
            self.assertIsNone(result["runtime_error"])
            self.assertEqual(result["mcp_servers"][0]["state"], "failed")
            self.assertNotIn("private remote credential", str(result))
            self.assertEqual(attempts, 1)
        finally:
            await application.registry.stop_all()


class MCPStartupDiagnosticsTest(unittest.TestCase):
    def test_http_error_keeps_server_and_status_instead_of_anyio_control_exception(self):
        request = httpx.Request("POST", "http://example.test/mcp?token=secret")
        error = httpx.HTTPStatusError("secret remote body", request=request,
                                     response=httpx.Response(502, request=request))
        error.__context__ = anyio.WouldBlock()
        group = ExceptionGroup("secret", [error])
        self.assertEqual(mcp_failure_detail(group), "HTTP 502")
        outer = MCPServerStartupError("Figma", "initialization", mcp_failure_detail(group))
        outer.__cause__ = group
        wrapper = RuntimeError("Runtime failed")
        wrapper.__cause__ = outer
        self.assertEqual(web._root_cause_message(wrapper),
                         "MCP server 'Figma' failed during initialization: HTTP 502")

    def test_configuration_defaults_and_opt_in_are_explicit_and_validated(self):
        _, servers = load_mcp_servers_from_configuration({"mcp": {"servers": {
            "optional": {"kind": "custom"},
            "required": {"kind": "custom", "required": True, "startup_timeout_seconds": 2},
        }}})
        self.assertFalse(servers[0].required)
        self.assertTrue(servers[1].required)
        self.assertEqual(servers[1].startup_timeout_seconds, 2)
        for value in (0, -1, True, float("inf"), float("nan"), "30"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MCPServerConfig("bad", "custom", startup_timeout_seconds=value)
        with self.assertRaises(ValueError):
            MCPServerConfig("bad", "custom", required="false")
