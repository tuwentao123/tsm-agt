"""Phase 2: SSE disconnect release and opt-in cancel (§12 #7/#8)."""

from __future__ import annotations

import os
import unittest

from tsm_agt.web import app as web_app


class _DisconnectRequest:
    def __init__(self, disconnected: bool) -> None:
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


class _FakeClient:
    def __init__(self) -> None:
        self.cancels: list[tuple[str, str, str]] = []

    def read_progress(self, task_id: str, after: int = 0):
        raise AssertionError("disconnect must be handled before reading progress")

    def get_task_result(self, task_id: str):
        raise AssertionError("disconnect must be handled before reading a result")

    async def cancel(self, task_id: str, *, command_id: str, reason: str):
        self.cancels.append((task_id, command_id, reason))


class _FakeServer:
    def __init__(self) -> None:
        self._client = _FakeClient()


class StreamDisconnectTest(unittest.IsolatedAsyncioTestCase):
    async def _drain(self, response):
        return [chunk async for chunk in response.body_iterator]

    async def test_disconnect_releases_stream_without_cancelling(self) -> None:
        previous = web_app.runtime_server
        server = _FakeServer()
        web_app.runtime_server = server
        os.environ.pop("TSM_AGT_CANCEL_ON_DISCONNECT", None)
        try:
            response = await web_app.stream(
                "task-stream", after=0, request=_DisconnectRequest(True),
            )
            self.assertEqual(await self._drain(response), [])
            # Default: the Task keeps running (durable sessions are the point).
            self.assertEqual(server._client.cancels, [])
        finally:
            web_app.runtime_server = previous

    async def test_disconnect_cancels_only_when_opted_in(self) -> None:
        previous = web_app.runtime_server
        server = _FakeServer()
        web_app.runtime_server = server
        os.environ["TSM_AGT_CANCEL_ON_DISCONNECT"] = "true"
        try:
            response = await web_app.stream(
                "task-stream", after=0, request=_DisconnectRequest(True),
            )
            self.assertEqual(await self._drain(response), [])
            self.assertEqual(len(server._client.cancels), 1)
            task_id, command_id, reason = server._client.cancels[0]
            self.assertEqual(task_id, "task-stream")
            self.assertEqual(command_id, "disconnect-task-stream")
            self.assertEqual(reason, "client disconnected")
        finally:
            os.environ.pop("TSM_AGT_CANCEL_ON_DISCONNECT", None)
            web_app.runtime_server = previous


if __name__ == "__main__":
    unittest.main()
