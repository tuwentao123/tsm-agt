"""Real SDK Streamable HTTP round trips with cross-Task client ownership."""
import asyncio
import socket
import unittest

from mcp.server.fastmcp import FastMCP
import uvicorn

from tsm_agt.adapters.mcp_python_sdk_client import create_mcp_sdk_client
from tsm_agt.ports import MCPCallNotSentError, MCPServerConfig


class MCPHTTPIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_streamable_http_stateful_and_stateless_lifetime(self):
        for stateless in (False, True):
            with self.subTest(stateless=stateless):
                mcp = FastMCP("local-p0", stateless_http=stateless, json_response=stateless)

                @mcp.tool()
                def add(a: int, b: int) -> int:
                    return a + b

                listener = socket.socket()
                try:
                    listener.bind(("127.0.0.1", 0))
                except BaseException:
                    listener.close()
                    raise
                listener.listen()
                listener.setblocking(False)
                port = listener.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(
                    mcp.streamable_http_app(), log_level="critical", ws="none",
                    timeout_graceful_shutdown=1,
                ))
                serving = asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    async with asyncio.timeout(10):
                        while not server.started:
                            if serving.done():
                                await serving
                            await asyncio.sleep(0.01)
                        client = await asyncio.create_task(create_mcp_sdk_client(MCPServerConfig(
                            "http_fixture", "streamable_http",
                            url=f"http://127.0.0.1:{port}/mcp",
                        )))
                        try:
                            self.assertEqual([tool.name for tool in await client.list_tools()], ["add"])
                            result = await asyncio.create_task(client.call_tool("add", {"a": 3, "b": 4}))
                            self.assertEqual(result.structuredContent["result"], 7)
                        finally:
                            await asyncio.gather(client.aclose(), client.aclose())
                        with self.assertRaises(MCPCallNotSentError):
                            await client.call_tool("add", {"a": 1, "b": 1})
                finally:
                    server.should_exit = True
                    try:
                        await asyncio.wait_for(serving, 5)
                    finally:
                        listener.close()
