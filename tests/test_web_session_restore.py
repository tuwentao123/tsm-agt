"""Web state that must survive a reload or a server restart."""

from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from tsm_agt.ports import MessageRole
from tsm_agt.sdk.runtime import RuntimeTaskResult

web_app = importlib.import_module("tsm_agt.web.app")


@dataclass
class _Message:
    role: MessageRole
    text: str
    task_id: str | None


@dataclass
class _Projection:
    messages: tuple[_Message, ...]


@dataclass
class _Session:
    session_id: str
    title: str
    task_ids: tuple[str, ...]
    active_task_id: str | None
    updated_at: datetime


@dataclass
class _Task:
    workspace: str


class _FakeKernel:
    def __init__(self, sessions, tasks, projection) -> None:
        self._sessions = sessions
        self._tasks = tasks
        self._projection = projection

    async def list_sessions(self):
        return tuple(self._sessions)

    async def get_session(self, session_id: str):
        for session in self._sessions:
            if session.session_id == session_id:
                return session
        raise LookupError(f"session not found: {session_id}")

    async def get_task(self, task_id: str):
        if task_id not in self._tasks:
            raise LookupError(f"task not found: {task_id}")
        return self._tasks[task_id]

    async def get_session_conversation(self, _session_id: str):
        return self._projection


class _FakeClient:
    def __init__(self, kernel: _FakeKernel, result: RuntimeTaskResult) -> None:
        self.application = type("_App", (), {"kernel": kernel})()
        self._result = result

    async def get_task_result(self, _task_id: str) -> RuntimeTaskResult:
        return self._result


class _FakeRuntimeServer:
    def __init__(self, kernel, result) -> None:
        self._client = _FakeClient(kernel, result)

    def _call(self, awaitable, *, timeout: float = 70):
        try:
            awaitable.send(None)
        except StopIteration as stop:
            return stop.value
        raise AssertionError("fake coroutines must not await")


class WorkspaceRegistryPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original = list(web_app.workspace_registry)
        web_app.workspace_registry.clear()

    def tearDown(self) -> None:
        web_app.workspace_registry[:] = self.original

    def test_identifier_is_derived_from_the_path_not_from_insertion_order(
        self,
    ) -> None:
        first = web_app.workspace_identifier("/tmp/alpha")
        again = web_app.workspace_identifier("/tmp/alpha")
        other = web_app.workspace_identifier("/tmp/beta")

        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        # A positional id would let a restart hand the same id to a different
        # directory, silently retargeting a stored client selection.
        self.assertNotIn(first, {"1", "2"})

    def test_registered_directories_survive_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            with mock.patch.object(web_app.Path, "cwd", return_value=root):
                registered = web_app.register_workspace(str(workspace))
                self.assertTrue(
                    (root / ".agent" / "web-workspaces.json").exists()
                )
                # Simulate a fresh process: memory empty, file on disk.
                web_app.workspace_registry.clear()
                web_app.load_workspace_registry()

        self.assertEqual(len(web_app.workspace_registry), 1)
        self.assertEqual(web_app.workspace_registry[0]["id"], registered["id"])
        self.assertEqual(
            web_app.workspace_registry[0]["path"], str(workspace.resolve())
        )

    def test_a_directory_that_disappeared_is_not_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".agent").mkdir()
            (root / ".agent" / "web-workspaces.json").write_text(
                json.dumps([
                    {"id": "stale", "name": "gone", "path": str(root / "gone")},
                ]),
                encoding="utf-8",
            )
            with mock.patch.object(web_app.Path, "cwd", return_value=root):
                web_app.load_workspace_registry()

        self.assertEqual(web_app.workspace_registry, [])


class SessionRestoreEndpointTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous = web_app.runtime_server

    def tearDown(self) -> None:
        web_app.runtime_server = self.previous

    @staticmethod
    def _server(*, waiting_approval: bool = False, phase1_state: str = "RUNNING"):
        sessions = [
            _Session(
                "session-old", "较早的会话", ("task-old",), None,
                datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
            ),
            _Session(
                "session-new", "最近的会话", ("task-new",), "task-new",
                datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
            ),
        ]
        tasks = {
            "task-old": _Task("/workspace/alpha"),
            "task-new": _Task("/workspace/beta"),
        }
        projection = _Projection((
            _Message(MessageRole.USER, "帮我看下这个文档", "task-new"),
            _Message(MessageRole.ASSISTANT, "已新增架构方案文档", "task-new"),
        ))
        result = RuntimeTaskResult(
            task_id="task-new", state="AWAITING_APPROVAL",
            phase1_state=phase1_state,
            status="awaiting_approval" if waiting_approval else "running",
            cursor=4,
            approval=(
                {"request_id": "approval-1", "action": "core.apply_patch"}
                if waiting_approval else None
            ),
        )
        return _FakeRuntimeServer(_FakeKernel(sessions, tasks, projection), result)

    async def test_sessions_are_listed_newest_first_with_their_workspace(
        self,
    ) -> None:
        web_app.runtime_server = self._server()

        payload = await web_app.list_sessions()

        self.assertEqual(
            [item["session_id"] for item in payload["sessions"]],
            ["session-new", "session-old"],
        )
        self.assertEqual(
            payload["sessions"][0]["workspace_path"], "/workspace/beta"
        )
        self.assertEqual(payload["sessions"][0]["latest_task_id"], "task-new")

    async def test_session_returns_durable_messages_for_a_reloaded_page(
        self,
    ) -> None:
        web_app.runtime_server = self._server()

        payload = await web_app.get_session("session-new")

        self.assertEqual(
            [(item["role"], item["content"]) for item in payload["messages"]],
            [
                ("user", "帮我看下这个文档"),
                ("assistant", "已新增架构方案文档"),
            ],
        )
        self.assertEqual(payload["latest_task_state"], "RUNNING")
        self.assertIsNone(payload["waiting"])

    async def test_unanswered_approval_comes_back_with_the_session(self) -> None:
        web_app.runtime_server = self._server(
            waiting_approval=True, phase1_state="WAITING"
        )

        payload = await web_app.get_session("session-new")

        self.assertEqual(payload["waiting"]["kind"], "APPROVAL")
        self.assertEqual(
            payload["waiting"]["approval"]["request_id"], "approval-1"
        )

    async def test_unknown_session_is_reported_as_missing(self) -> None:
        web_app.runtime_server = self._server()

        with self.assertRaises(HTTPException) as caught:
            await web_app.get_session("session-missing")

        self.assertEqual(caught.exception.status_code, 404)


class StreamResilienceUiTest(unittest.TestCase):
    def test_stream_reconnects_and_reconciles_against_the_task(self) -> None:
        html = web_app.INDEX_HTML
        # Closing the stream on error left the page silent while the Task kept
        # running, so a retry plus a durable catch-up is the contract.
        self.assertIn("MAX_STREAM_RETRIES", html)
        self.assertIn("const reconcile = async () =>", html)
        self.assertIn("setTimeout(connect,", html)
        self.assertIn("任务仍在后台运行", html)

    def test_page_load_restores_sessions_and_reattaches_running_tasks(
        self,
    ) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("async function loadSessions()", html)
        self.assertIn("await loadSessions();", html)
        self.assertIn("ensureConversationLoaded(", html)
        self.assertIn("followTask(data.latest_task_id, conversationId)", html)


if __name__ == "__main__":
    unittest.main()
