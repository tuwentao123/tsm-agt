"""Only explicit no-dispatch evidence permits an ordinary MCP call failure."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tsm_agt.adapters.mcp_tool_provider import MCPToolProvider
from tsm_agt.ports import (
    AdapterContext, MCPCallNotSentError, MCPClientManager, MCPServerConfig,
    ToolCall, ToolInvocationContext, ToolRecoveryKind,
)


class Client:
    def __init__(self):
        self.error = None
        self.result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
        self.calls = 0
        self.closes = 0

    async def list_tools(self):
        return [SimpleNamespace(name="write", description="write", inputSchema={"type": "object"})]

    async def call_tool(self, name, arguments):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result

    async def aclose(self):
        self.closes += 1


class MCPCallOutcomeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = Client()
        self.manager = MCPClientManager(
            (MCPServerConfig("fixture", "custom"),), AsyncMock(return_value=self.client),
        )
        self.provider = MCPToolProvider(self.manager)
        await self.provider.start(AdapterContext({}, lambda *_: None))
        await self.provider.wait_ready()
        self.call = ToolCall("call", "mcp.fixture.write", {})
        self.context = ToolInvocationContext(
            "inv", "task", "turn", Path.cwd(), datetime.now(timezone.utc),
        )

    async def asyncTearDown(self):
        await self.provider.stop(datetime.now(timezone.utc))

    async def test_connection_error_before_call_is_explicitly_not_dispatched(self):
        with patch.object(self.manager, "get_client", side_effect=ConnectionError("private detail")):
            result = await self.provider.invoke(self.call, self.context)
        self.assertEqual(result.error_code, "MCP_CONNECTION_ERROR")
        self.assertIn("was not dispatched", result.message)
        self.assertNotIn("private detail", result.message)
        self.assertEqual(self.client.calls, 0)

    async def test_client_can_prove_call_was_not_sent(self):
        self.client.error = MCPCallNotSentError("closed before dispatch")
        result = await self.provider.invoke(self.call, self.context)
        self.assertEqual(result.error_code, "MCP_CALL_NOT_SENT")
        self.assertEqual(result.effective_recovery_kind, ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE)

    async def test_rpc_exceptions_are_unknown_even_for_key_error_or_timeout(self):
        for error in (ConnectionError("response lost"), KeyError("server error"), TimeoutError(), ValueError("bad response")):
            with self.subTest(error=type(error).__name__):
                self.client.error = error
                result = await self.provider.invoke(self.call, self.context)
                self.assertEqual(result.error_code, "UNKNOWN_OUTCOME")
                self.assertEqual(result.effective_recovery_kind, ToolRecoveryKind.UNKNOWN_OUTCOME)
                self.assertFalse(result.retryable)
                self.assertIn("Do not repeat", result.message)
                self.assertEqual(result.meta["error_type"], type(error).__name__)

    async def test_result_serialization_error_is_also_unknown(self):
        class InvalidResult:
            def model_dump(self, **_):
                raise ValueError("invalid result")

        self.client.result = InvalidResult()
        result = await self.provider.invoke(self.call, self.context)
        self.assertEqual(result.error_code, "UNKNOWN_OUTCOME")
        self.assertFalse(result.retryable)

    async def test_explicit_is_error_is_a_known_failure(self):
        self.client.result = {"content": [{"type": "text", "text": "cannot perform"}], "isError": True}
        result = await self.provider.invoke(self.call, self.context)
        self.assertEqual(result.error_code, "MCP_TOOL_ERROR")
        self.assertNotEqual(result.effective_recovery_kind, ToolRecoveryKind.UNKNOWN_OUTCOME)
        self.assertEqual(self.client.calls, 1)

    async def test_cancellation_remains_cancellation(self):
        self.client.error = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.provider.invoke(self.call, self.context)


class MCPShutdownFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_peers_are_closed_in_reverse_order_despite_one_failure(self):
        closes = []

        class Peer:
            def __init__(self, name):
                self.name = name

            async def aclose(self):
                closes.append(self.name)
                if self.name == "b":
                    raise ValueError("close error")

        async def factory(config):
            return Peer(config.id)

        manager = MCPClientManager(tuple(MCPServerConfig(name, "custom") for name in "abc"), factory)
        await manager.ensure_all_started()
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "shutdown failed"):
                await manager.aclose()
        self.assertEqual(closes, ["c", "b", "a"])
