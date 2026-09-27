"""§11/§8 entry points for explicit cancel: CLI `/cancel` and Web button."""

from __future__ import annotations

import unittest
from pathlib import Path

from tsm_agt import cli
from tsm_agt.web import app as web_app


class CliCancelCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_reaches_the_cancel_branch(self) -> None:
        # /cancel must not be swallowed by the read-only command handler, or the
        # control loop would never reach its cancel branch.
        handled = await cli._handle_active_chat_command(
            "/cancel", None, None, "task-1", Path("."), lambda *_: None,
        )
        self.assertFalse(handled)


class WebCancelEntryPointTest(unittest.TestCase):
    def test_cancel_route_is_registered(self) -> None:
        paths = {route.path for route in web_app.app.routes}
        self.assertIn("/tasks/{task_id}/cancel", paths)

    def test_task_card_exposes_a_cancel_button(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("async function cancelTask(taskId, conversationId)", html)
        self.assertIn("/tasks/${taskId}/cancel", html)
        self.assertIn("取消任务", html)


if __name__ == "__main__":
    unittest.main()
