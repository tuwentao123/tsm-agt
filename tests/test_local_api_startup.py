"""Do not publish the API merely because its Runtime loop was allocated."""
import asyncio
from pathlib import Path
import threading
import unittest
from unittest.mock import AsyncMock, patch

from tsm_agt.local_api import LocalEventApiServer


class LocalAPIStartupTest(unittest.TestCase):
    def test_startup_timeout_cancels_and_cleans_on_runtime_loop_without_binding(self):
        cleaned = threading.Event()

        async def start():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        server = LocalEventApiServer(Path.cwd(), port=0, startup_timeout_seconds=0.03)
        server._client = AsyncMock()
        server._client.start.side_effect = start
        with patch("tsm_agt.local_api.ThreadingHTTPServer") as http:
            with self.assertRaisesRegex(RuntimeError, "startup timed out"):
                server.start()
            http.assert_not_called()
        self.assertTrue(cleaned.is_set())
        server._client.close.assert_awaited_once()
        self.assertIsNone(server._loop_thread)
        server.stop()

    def test_failed_start_closes_client_and_preserves_original_cause(self):
        server = LocalEventApiServer(Path.cwd(), port=0)
        server._client = AsyncMock()
        original = ValueError("configuration failed")
        server._client.start.side_effect = original
        with self.assertRaises(RuntimeError) as caught:
            server.start()
        self.assertIs(caught.exception.__cause__, original)
        server._client.close.assert_awaited_once()
        self.assertIsNone(server._loop_thread)
        server.stop()
