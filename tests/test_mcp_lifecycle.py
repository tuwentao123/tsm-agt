"""MCP owner lifetime, cancellation, argv and complete discovery contracts."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from tsm_agt.adapters import mcp_python_sdk_client as sdk
from tsm_agt.ports import MCPCallNotSentError, MCPClientManager, MCPServerConfig


class SessionFixture:
    def __init__(self) -> None:
        self.records: list[tuple[str, asyncio.Task]] = []
        self.initializing = asyncio.Event()
        self.initialize_release = asyncio.Event()
        self.initialize_release.set()
        self.closing = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.initialize_error: Exception | None = None
        self.close_error: Exception | None = None
        self.pages = {None: SimpleNamespace(tools=[SimpleNamespace(name="first")], nextCursor=None)}
        self.cursors: list[str | None] = []
        self.call_count = 0

    def record(self, action: str) -> None:
        self.records.append((action, asyncio.current_task()))

    @asynccontextmanager
    async def transport(self):
        self.record("transport_enter")
        try:
            yield None, None
        finally:
            self.closing.set()
            await self.close_release.wait()
            self.record("transport_exit")
            if self.close_error:
                raise self.close_error

    async def __aenter__(self):
        self.record("session_enter")
        return self

    async def __aexit__(self, *_):
        self.record("session_exit")

    async def initialize(self):
        self.initializing.set()
        await self.initialize_release.wait()
        if self.initialize_error:
            raise self.initialize_error

    async def list_tools(self, cursor=None):
        self.cursors.append(cursor)
        return self.pages[cursor]

    async def call_tool(self, name, arguments):
        self.call_count += 1
        return {"name": name, "arguments": arguments}


async def open_fixture(fixture: SessionFixture):
    with patch.object(sdk, "_ClientSession", return_value=fixture):
        return await sdk._session_client(
            kind="fixture", transport=fixture.transport(), server_id="fixture",
        )


class MCPLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Bound broken lifecycle implementations as well as failed assertions.
        self.timeout = asyncio.timeout(5)
        await self.timeout.__aenter__()

    async def asyncTearDown(self):
        await self.timeout.__aexit__(None, None, None)

    async def test_context_owner_survives_factory_task_and_cross_task_close(self):
        fixture = SessionFixture()
        client = await asyncio.create_task(open_fixture(fixture))
        try:
            result = await asyncio.create_task(client.call_tool("echo", {"x": 1}))
            self.assertEqual(result["arguments"], {"x": 1})
        finally:
            await asyncio.create_task(client.aclose())
        self.assertEqual([action for action, _ in fixture.records], [
            "transport_enter", "session_enter", "session_exit", "transport_exit",
        ])
        owners = {task for _, task in fixture.records}
        self.assertEqual(len(owners), 1)
        self.assertNotIn(asyncio.current_task(), owners)
        with self.assertRaises(MCPCallNotSentError):
            await client.call_tool("echo", {})
        self.assertEqual(fixture.call_count, 1)

    async def test_concurrent_calls_do_not_serialize_on_connection_owner(self):
        fixture = SessionFixture()
        both_started = asyncio.Event()

        async def call(name, arguments):
            fixture.call_count += 1
            if fixture.call_count == 2:
                both_started.set()
            await both_started.wait()
            return arguments

        fixture.call_tool = call
        client = await open_fixture(fixture)
        try:
            results = await asyncio.gather(
                client.call_tool("echo", {"x": 1}), client.call_tool("echo", {"x": 2}),
            )
            self.assertEqual(results, [{"x": 1}, {"x": 2}])
        finally:
            await client.aclose()

    async def test_repeated_startup_cancel_joins_cleanup_and_remains_cancelled(self):
        fixture = SessionFixture()
        fixture.initialize_release.clear()
        fixture.close_release.clear()
        starting = asyncio.create_task(open_fixture(fixture))
        await fixture.initializing.wait()
        starting.cancel()
        await fixture.closing.wait()
        starting.cancel()
        await asyncio.sleep(0)
        self.assertFalse(starting.done())
        fixture.close_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await starting
        self.assertEqual([action for action, _ in fixture.records][-2:], [
            "session_exit", "transport_exit",
        ])
        self.assertEqual(len({task for _, task in fixture.records}), 1)

    async def test_startup_failure_cleans_up_and_preserves_cause(self):
        fixture = SessionFixture()
        fixture.initialize_error = ValueError("bad handshake")
        with self.assertRaisesRegex(RuntimeError, "initialization") as raised:
            await open_fixture(fixture)
        self.assertIs(raised.exception.__cause__, fixture.initialize_error)
        self.assertEqual(fixture.records[-1][0], "transport_exit")

    async def test_cancel_during_failed_startup_cleanup_does_not_interrupt_cleanup(self):
        fixture = SessionFixture()
        fixture.initialize_error = ValueError("bad handshake")
        fixture.close_release.clear()
        starting = asyncio.create_task(open_fixture(fixture))
        await fixture.closing.wait()
        starting.cancel()
        await asyncio.sleep(0)
        self.assertFalse(starting.done())
        fixture.close_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await starting
        self.assertEqual(fixture.records[-1][0], "transport_exit")

    async def test_cancelled_close_and_second_close_wait_for_same_cleanup(self):
        fixture = SessionFixture()
        client = await open_fixture(fixture)
        fixture.close_release.clear()
        first = asyncio.create_task(client.aclose())
        await fixture.closing.wait()
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        second = asyncio.create_task(client.aclose())
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        fixture.close_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        await client.aclose()
        self.assertEqual([name for name, _ in fixture.records].count("transport_exit"), 1)

    async def test_shutdown_failure_is_visible_after_transport_exit(self):
        fixture = SessionFixture()
        client = await open_fixture(fixture)
        fixture.close_error = ValueError("close failed")
        with self.assertRaisesRegex(RuntimeError, "session failed") as raised:
            await client.aclose()
        self.assertIs(raised.exception.__cause__, fixture.close_error)
        with self.assertRaises(MCPCallNotSentError):
            await client.call_tool("echo", {})

    async def test_discovery_collects_every_page_in_order(self):
        fixture = SessionFixture()
        fixture.pages = {
            None: SimpleNamespace(tools=["a"], nextCursor="two"),
            "two": SimpleNamespace(tools=[], nextCursor="three"),
            "three": SimpleNamespace(tools=["b"], nextCursor=None),
        }
        client = await open_fixture(fixture)
        try:
            self.assertEqual(await client.list_tools(), ["a", "b"])
            self.assertEqual(fixture.cursors, [None, "two", "three"])
        finally:
            await client.aclose()

    async def test_invalid_or_cyclic_cursor_fails_without_partial_catalog(self):
        for cursor in ("same", "", 12):
            with self.subTest(cursor=cursor):
                fixture = SessionFixture()
                fixture.pages = {
                    None: SimpleNamespace(tools=["a"], nextCursor=cursor),
                    "same": SimpleNamespace(tools=["b"], nextCursor="same"),
                }
                client = await open_fixture(fixture)
                try:
                    with self.assertRaisesRegex(RuntimeError, "cursor"):
                        await client.list_tools()
                    self.assertLessEqual(len(fixture.cursors), 2)
                finally:
                    await client.aclose()

    async def test_discovery_limits_stop_unbounded_server(self):
        fixture = SessionFixture()
        fixture.pages = {
            None: SimpleNamespace(tools=["a"], nextCursor="two"),
            "two": SimpleNamespace(tools=["b"], nextCursor="three"),
        }
        client = await open_fixture(fixture)
        try:
            with patch.object(sdk, "_MAX_TOOL_LIST_PAGES", 2):
                with self.assertRaisesRegex(RuntimeError, "page discovery limit"):
                    await client.list_tools()
            with patch.object(sdk, "_MAX_DISCOVERED_TOOLS", 1):
                with self.assertRaisesRegex(RuntimeError, "tool discovery limit"):
                    await client.list_tools()
        finally:
            await client.aclose()

    async def test_sequence_fixture_clients_remain_supported(self):
        fixture = SessionFixture()
        fixture.pages = {None: ["a", "b"]}
        client = await open_fixture(fixture)
        try:
            self.assertEqual(await client.list_tools(), ["a", "b"])
        finally:
            await client.aclose()

    async def test_argv_keeps_program_and_arguments_with_explicit_python_compatibility(self):
        for command, program in (
            (("npx",), "npx"),
            (("/some/server.py",), "/some/server.py"),
            (("node", "server.js"), "node"),
            (("/some/venv/python", "-m", "server"), "/some/venv/python"),
            (("python", "-m", "server"), sys.executable),
            (("python3",), sys.executable),
        ):
            with self.subTest(command=command):
                fixture = SessionFixture()
                with (
                    patch.object(sdk, "_ClientSession", return_value=fixture),
                    patch.object(sdk, "_stdio_client", return_value=fixture.transport()) as transport,
                ):
                    client = await sdk.create_mcp_sdk_client(MCPServerConfig(
                        "fixture", "executable", command=command,
                        options={"cwd": "/workspace", "env": {"EXAMPLE": "value"}},
                    ))
                try:
                    params = transport.call_args.args[0]
                    self.assertEqual(params.command, program)
                    self.assertEqual(params.args, list(command[1:]))
                    self.assertEqual(params.cwd, "/workspace")
                    self.assertEqual(params.env, {"EXAMPLE": "value"})
                finally:
                    await client.aclose()


class MCPManagerLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_joins_start_and_repeated_close_even_when_caller_cancelled(self):
        entered, release, closing, closed = (asyncio.Event() for _ in range(4))
        closes = []

        class Client:
            async def aclose(self):
                closes.append(True)
                closing.set()
                await closed.wait()

        async def factory(_):
            entered.set()
            await release.wait()
            return Client()

        manager = MCPClientManager((MCPServerConfig("fixture", "custom"),), factory)
        async with asyncio.timeout(5):
            starting = asyncio.create_task(manager.get_client("fixture"))
            await entered.wait()
            first = asyncio.create_task(manager.aclose())
            await asyncio.sleep(0)
            first.cancel()
            second = asyncio.create_task(manager.aclose())
            release.set()
            await closing.wait()
            self.assertFalse(second.done())
            closed.set()
            with self.assertRaisesRegex(RuntimeError, "closed"):
                await starting
            with self.assertRaises(asyncio.CancelledError):
                await first
            await second
            await manager.aclose()
        self.assertEqual(len(closes), 1)
