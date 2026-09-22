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
    source_event_sequence: int = 1


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
    goal: str


@dataclass
class _SessionEvent:
    sequence: int
    event_type: str
    payload: dict


class _FakeStore:
    def __init__(self, events) -> None:
        self._events = tuple(events)

    async def read_session_events(self, _session_id: str):
        return self._events


class _FakeKernel:
    def __init__(self, sessions, tasks, projection) -> None:
        self._sessions = sessions
        self._tasks = tasks
        self._projection = projection
        events = []
        sequence = 1
        seen = set()
        for session in sessions:
            for task_id in session.task_ids:
                if task_id in seen:
                    continue
                seen.add(task_id)
                events.append(_SessionEvent(
                    sequence, "session.task_attached", {"task_id": task_id}
                ))
                sequence += 3
        self.dependencies = type(
            "_Dependencies", (), {"store": _FakeStore(events)}
        )()

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
    def __init__(self, kernel: _FakeKernel, result) -> None:
        self.application = type("_App", (), {"kernel": kernel})()
        self._result = result

    async def get_task_result(self, task_id: str) -> RuntimeTaskResult:
        if isinstance(self._result, dict):
            return self._result[task_id]
        return self._result

    def latest_progress_sequence(self, _task_id: str) -> int:
        return 7


class _FakeRuntimeServer:
    def __init__(self, kernel, result) -> None:
        self._client = _FakeClient(kernel, result)

    def _call(self, awaitable, *, timeout: float = 70):
        try:
            awaitable.send(None)
        except StopIteration as stop:
            return stop.value
        raise AssertionError("fake coroutines must not await")



    def test_restore_conversation_draft_prefers_explicit_draft_only(self) -> None:
        """Refresh hydration must restore only persisted draft state."""

        persisted = "用户原始输入"
        derived_title = "被错误投影后的标题"

        resolve = """
function resolveConversationDraft(activeConversation, persistedDraft) {
  if (typeof persistedDraft === 'string') {
    return persistedDraft;
  }

  if (typeof activeConversation?.draft === 'string') {
    return activeConversation.draft;
  }

  return '';
}
"""

        self.assertIn("return persistedDraft", resolve)
        self.assertNotIn("title", resolve)
        self.assertNotIn("summary", resolve)
        self.assertNotEqual(persisted, derived_title)
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
            registry_path = root / ".agent" / "web-workspaces.json"
            with mock.patch.object(
                web_app, "workspace_registry_path", return_value=registry_path
            ):
                registered = web_app.register_workspace(str(workspace))
                self.assertTrue(registry_path.exists())
                # Simulate a fresh process: memory empty, file on disk.
                web_app.workspace_registry.clear()
                web_app.load_workspace_registry()

        self.assertEqual(len(web_app.workspace_registry), 1)
        self.assertEqual(web_app.workspace_registry[0]["id"], registered["id"])
        self.assertEqual(
            web_app.workspace_registry[0]["path"], str(workspace.resolve())
        )

    def test_runtime_history_rebuilds_a_missing_workspace_index(self) -> None:
        previous_server = web_app.runtime_server
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "recovered-project"
            workspace.mkdir()
            session = _Session(
                "session-recover", "recover", ("task-recover",),
                "task-recover", datetime.now(timezone.utc),
            )
            kernel = _FakeKernel(
                (session,),
                {"task-recover": _Task(str(workspace), "recover goal")},
                _Projection(()),
            )
            result = RuntimeTaskResult(
                task_id="task-recover", state="SUCCEEDED",
                phase1_state="DONE", status="completed", cursor=1,
            )
            web_app.runtime_server = _FakeRuntimeServer(kernel, result)
            registry_path = root / ".agent" / "web-workspaces.json"
            try:
                with mock.patch.object(
                    web_app, "workspace_registry_path",
                    return_value=registry_path,
                ):
                    web_app.recover_workspace_registry_from_runtime()
            finally:
                web_app.runtime_server = previous_server

        self.assertEqual(
            [item["path"] for item in web_app.workspace_registry],
            [str(workspace)],
        )
        self.assertEqual(
            web_app.workspace_registry[0]["id"],
            web_app.workspace_identifier(str(workspace)),
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
            registry_path = root / ".agent" / "web-workspaces.json"
            with mock.patch.object(
                web_app, "workspace_registry_path", return_value=registry_path
            ):
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
                "session-new", "最近的会话", ("task-old", "task-new"),
                "task-new",
                datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
            ),
        ]
        tasks = {
            "task-old": _Task("/workspace/alpha", "old goal"),
            "task-new": _Task("/workspace/beta", "new goal"),
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
        self.assertEqual(payload["active_task_id"], "task-new")
        self.assertEqual(
            [item["task_id"] for item in payload["tasks"]],
            ["task-old", "task-new"],
        )
        self.assertEqual(payload["tasks"][1]["progress_cursor"], 7)
        self.assertEqual(
            [item["attached_sequence"] for item in payload["tasks"]],
            [1, 4],
        )
        self.assertEqual(
            [item["source_event_sequence"] for item in payload["messages"]],
            [1, 1],
        )

    async def test_unanswered_approval_comes_back_with_the_session(self) -> None:
        web_app.runtime_server = self._server(
            waiting_approval=True, phase1_state="WAITING"
        )

        payload = await web_app.get_session("session-new")

        self.assertEqual(payload["waiting"]["kind"], "APPROVAL")
        self.assertEqual(
            payload["waiting"]["approval"]["request_id"], "approval-1"
        )

    async def test_old_waiting_task_and_active_running_task_remain_separate(
        self,
    ) -> None:
        server = self._server()
        server._client._result = {
            "task-old": RuntimeTaskResult(
                task_id="task-old", state="AWAITING_APPROVAL",
                phase1_state="WAITING", status="awaiting_approval", cursor=20,
                approval={
                    "request_id": "approval-old",
                    "action": "git push origin branch",
                },
            ),
            "task-new": RuntimeTaskResult(
                task_id="task-new", state="EXECUTING",
                phase1_state="RUNNING", status="running", cursor=4,
            ),
        }
        web_app.runtime_server = server

        payload = await web_app.get_session("session-new")

        by_id = {item["task_id"]: item for item in payload["tasks"]}
        self.assertEqual(by_id["task-old"]["phase1_state"], "WAITING")
        self.assertEqual(by_id["task-old"]["waiting"]["kind"], "APPROVAL")
        self.assertEqual(by_id["task-new"]["phase1_state"], "RUNNING")
        self.assertIsNone(by_id["task-new"]["waiting"])
        self.assertEqual(payload["active_task_id"], "task-new")

    async def test_unknown_session_is_reported_as_missing(self) -> None:
        web_app.runtime_server = self._server()

        with self.assertRaises(HTTPException) as caught:
            await web_app.get_session("session-missing")

        self.assertEqual(caught.exception.status_code, 404)


class StreamResilienceUiTest(unittest.TestCase):
    def test_failed_task_without_result_is_sorted_by_attach_sequence(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("task.attached_sequence", html)
        self.assertIn("message.source_event_sequence", html)
        self.assertIn("timeline.sort((left, right)", html)
        self.assertIn("left.sequence - right.sequence", html)
        self.assertNotIn("restored.push({", html)
        # A failed Task without a result message is still inserted at its
        # session.task_attached position, never appended after current chat.
        self.assertIn("order: 1", html)

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
        self.assertIn("const activeTask = tasksById.get(data.active_task_id)", html)
        self.assertIn("followTask(activeTask.task_id, conversationId)", html)
        self.assertIn("['PREPARING', 'RUNNING'].includes", html)
        self.assertNotIn("followTask(data.latest_task_id, conversationId)", html)


class _ProgressEnvelope:
    sequence = 9

    def to_data(self):
        return {
            "task_id": "task-stream", "sequence": self.sequence,
            "progress": {"kind": "tool", "tool_name": "core.read_file"},
        }


class _StreamClient:
    def __init__(self) -> None:
        self.afters: list[int] = []

    def read_progress(self, _task_id: str, *, after: int = 0):
        self.afters.append(after)
        return (_ProgressEnvelope(),) if after < 9 else ()

    async def get_task_result(self, task_id: str):
        return RuntimeTaskResult(
            task_id=task_id, state="SUCCEEDED", phase1_state="DONE",
            status="completed", cursor=12,
        )


class _StreamServer:
    def __init__(self) -> None:
        self._client = _StreamClient()

    def _call(self, awaitable, *, timeout: float = 70):
        try:
            awaitable.send(None)
        except StopIteration as stop:
            return stop.value
        raise AssertionError("fake coroutines must not await")


class StreamCursorProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_starts_after_the_requested_progress_cursor(self) -> None:
        previous = web_app.runtime_server
        server = _StreamServer()
        web_app.runtime_server = server
        try:
            response = await web_app.stream("task-stream", after=8)
            chunk = await anext(response.body_iterator)
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8")
            self.assertIn("id: 9\n", chunk)
            self.assertIn('"sequence": 9', chunk)
            self.assertEqual(server._client.afters[0], 8)
            await response.body_iterator.aclose()
        finally:
            web_app.runtime_server = previous

    async def test_negative_cursor_is_rejected(self) -> None:
        previous = web_app.runtime_server
        web_app.runtime_server = _StreamServer()
        try:
            with self.assertRaises(HTTPException) as caught:
                await web_app.stream("task-stream", after=-1)
            self.assertEqual(caught.exception.status_code, 400)
        finally:
            web_app.runtime_server = previous


class TaskCardIsolationUiTest(unittest.TestCase):
    def test_progress_is_written_to_a_task_card_not_session_system_text(self):
        html = web_app.INDEX_HTML
        self.assertIn("function appendTaskProgress(taskId, progress, conversationId)", html)
        self.assertIn("appendTaskProgress(taskId, payload, conversationId)", html)
        self.assertNotIn("if (text) appendMessage('system', text", html)
        self.assertIn("message.role === 'task'", html)

    def test_each_reconnect_uses_its_own_task_cursor(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("`/stream/${taskId}?after=${after}`", html)
        self.assertIn("event.lastEventId", html)
        self.assertIn("item.task?.taskId === taskId", html)

    def test_trace_state_uses_structured_progress_not_display_text(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("function traceStateForProgress(progress)", html)
        self.assertIn("kind === 'tool_started'", html)
        self.assertIn("kind === 'tool_completed'", html)
        self.assertIn("progress.ok === false || progress.error_code", html)
        self.assertIn("const progress = item.progressData || {}", html)
        # Patch contents and filenames are untrusted display text. In particular,
        # `approval-request-bound` must never turn a completed patch into waiting.
        self.assertNotIn("lower.includes('approval')", html)
        self.assertNotIn("lower.includes('fail')", html)
        self.assertNotIn("lower.includes('错误')", html)

    def test_tool_and_model_start_completion_events_are_paired(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("const pendingTools = []", html)
        self.assertIn("pendingTools.splice(index, 1)", html)
        self.assertIn("step.state = state", html)
        self.assertIn("let pendingModel = null", html)
        self.assertIn("kind === 'model_completed' && pendingModel !== null", html)
        self.assertIn("lines.push({ sequence, text, progressData })", html)

    def test_new_active_task_stops_older_followers_in_the_same_session(self):
        html = web_app.INDEX_HTML
        self.assertIn("stopConversationFollowers(conversationId, taskId)", html)
        self.assertIn("follower.conversationId === conversationId", html)
        self.assertIn("task.phase1State === 'WAITING'", html)
        self.assertIn("不会自动重放历史执行日志", html)


if __name__ == "__main__":
    unittest.main()
