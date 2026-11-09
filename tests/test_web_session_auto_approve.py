"""Web session auto-approve preference: stored per Session, applied by the client."""

from __future__ import annotations

import datetime
import importlib
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

web_app = importlib.import_module("tsm_agt.web.app")


class SessionAutoApproveTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = Path(directory.name) / "auto-approvals.json"
        patcher = mock.patch.object(
            web_app, "session_auto_approvals_path", lambda: target,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        web_app.session_auto_approvals.clear()
        self.addCleanup(web_app.session_auto_approvals.clear)

    async def test_switch_survives_a_reload_and_clears_when_off(self) -> None:
        enabled = await web_app.update_session_auto_approve(
            "session-1", {"auto_approve": True},
        )
        self.assertEqual(enabled, {"session_id": "session-1", "auto_approve": True})

        web_app.session_auto_approvals.clear()
        web_app.load_session_auto_approvals()
        self.assertTrue(web_app.session_auto_approvals.get("session-1"))

        disabled = await web_app.update_session_auto_approve(
            "session-1", {"auto_approve": False},
        )
        self.assertEqual(disabled, {"session_id": "session-1", "auto_approve": False})
        self.assertNotIn("session-1", web_app.session_auto_approvals)

    async def test_a_draft_session_is_accepted_before_it_exists(self) -> None:
        await web_app.update_session_auto_approve("draft-1", {"auto_approve": True})
        self.assertTrue(web_app.session_auto_approvals.get("draft-1"))

    async def test_non_boolean_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await web_app.update_session_auto_approve(
                "session-1", {"auto_approve": "yes"},
            )
        self.assertEqual(caught.exception.status_code, 400)

    async def test_session_list_reports_each_sessions_switch(self) -> None:
        """The sidebar reloads from ``/sessions``, so the switch must round-trip."""
        snapshots = [
            types.SimpleNamespace(
                session_id=session_id, title=session_id,
                updated_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
                active_task_id=None, task_ids=[],
                model_profile_id=None, reasoning_effort=None,
            )
            for session_id in ("session-on", "session-off")
        ]
        stub = types.SimpleNamespace(
            _client=types.SimpleNamespace(
                application=types.SimpleNamespace(
                    kernel=types.SimpleNamespace(list_sessions=lambda: snapshots),
                ),
            ),
            _call=lambda value: value,
        )
        await web_app.update_session_auto_approve("session-on", {"auto_approve": True})
        with mock.patch.object(web_app, "runtime_server", stub):
            listed = await web_app.list_sessions()
        reported = {item["session_id"]: item["auto_approve"] for item in listed["sessions"]}
        self.assertEqual(reported, {"session-on": True, "session-off": False})


if __name__ == "__main__":
    unittest.main()
