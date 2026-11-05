"""Removing a Session or a workspace must survive a reload and a restart."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from tsm_agt.web import app as web_app


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
    goal: str = "goal"


class _FakeKernel:
    def __init__(self, sessions, tasks) -> None:
        self._sessions = tuple(sessions)
        self._tasks = dict(tasks)

    async def list_sessions(self):
        return self._sessions

    async def get_task(self, task_id: str):
        if task_id not in self._tasks:
            raise LookupError(task_id)
        return self._tasks[task_id]


class _FakeRuntimeServer:
    def __init__(self, kernel) -> None:
        wrapper = type("_Application", (), {"kernel": kernel})()
        self._client = type("_Client", (), {"application": wrapper})()

    def _call(self, awaitable, *, timeout: float = 70):
        try:
            awaitable.send(None)
        except StopIteration as stop:
            return stop.value
        raise AssertionError("fake coroutines must not await")


def _session(session_id: str, task_id: str) -> _Session:
    return _Session(
        session_id, session_id, (task_id,), task_id, datetime.now(timezone.utc),
    )


class _RemovalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_server = web_app.runtime_server
        self._saved_registry = list(web_app.workspace_registry)
        self._saved_bindings = dict(web_app.session_workspace_bindings)
        web_app.runtime_server = None
        web_app.workspace_registry.clear()
        web_app.session_workspace_bindings.clear()
        web_app.removed_workspace_paths.clear()
        web_app.removed_session_ids.clear()

    def tearDown(self) -> None:
        web_app.runtime_server = self._saved_server
        web_app.workspace_registry[:] = self._saved_registry
        web_app.session_workspace_bindings.clear()
        web_app.session_workspace_bindings.update(self._saved_bindings)
        web_app.removed_workspace_paths.clear()
        web_app.removed_session_ids.clear()


class RemovedSessionTest(_RemovalTestCase):
    def test_removed_session_is_hidden_and_stays_hidden_after_a_restart(
        self,
    ) -> None:
        kernel = _FakeKernel(
            [_session("s-1", "t-1"), _session("s-2", "t-2")],
            {"t-1": _Task("/tmp/alpha"), "t-2": _Task("/tmp/beta")},
        )
        web_app.runtime_server = _FakeRuntimeServer(kernel)

        with tempfile.TemporaryDirectory() as directory:
            removals = Path(directory) / ".agent" / "web-removals.json"
            with mock.patch.object(
                web_app, "web_removals_path", return_value=removals
            ):
                asyncio.run(web_app.remove_session("s-1"))
                listing = asyncio.run(web_app.list_sessions(40))
                self.assertEqual(
                    [item["session_id"] for item in listing["sessions"]], ["s-2"],
                )
                # The hide list is the only durable state; a fresh process reads
                # it back instead of trusting the in-memory set.
                web_app.removed_session_ids.clear()
                web_app.load_web_removals()
                again = asyncio.run(web_app.list_sessions(40))
                self.assertEqual(
                    [item["session_id"] for item in again["sessions"]], ["s-2"],
                )

    def test_unknown_session_is_reported_as_missing(self) -> None:
        web_app.runtime_server = _FakeRuntimeServer(
            _FakeKernel([_session("s-1", "t-1")], {"t-1": _Task("/tmp/alpha")})
        )
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(web_app.remove_session("missing"))
        self.assertEqual(caught.exception.status_code, 404)

    def test_removal_without_a_runtime_reports_unavailable(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(web_app.remove_session("s-1"))
        self.assertEqual(caught.exception.status_code, 503)


class RemovedWorkspaceTest(_RemovalTestCase):
    def test_removed_workspace_is_not_recovered_from_durable_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            registry = root / ".agent" / "web-workspaces.json"
            removals = root / ".agent" / "web-removals.json"
            with mock.patch.object(
                web_app, "workspace_registry_path", return_value=registry
            ), mock.patch.object(
                web_app, "web_removals_path", return_value=removals
            ):
                registered = web_app.register_workspace(str(workspace))
                workspace_path = registered["path"]
                asyncio.run(web_app.remove_workspace(registered["id"]))
                self.assertEqual(web_app.workspace_registry, [])

                # Startup rebuilds the index from Task ownership; the deny-list
                # is what keeps a removed directory out of the sidebar.
                web_app.runtime_server = _FakeRuntimeServer(
                    _FakeKernel(
                        [_session("s-1", "t-1")],
                        {"t-1": _Task(workspace_path)},
                    )
                )
                web_app.recover_workspace_registry_from_runtime()
                self.assertEqual(web_app.workspace_registry, [])

                # Picking the same directory again is an explicit revival.
                revived = web_app.register_workspace(workspace_path)
                self.assertEqual(revived["path"], workspace_path)
                self.assertNotIn(workspace_path, web_app.removed_workspace_paths)

    def test_unknown_workspace_is_reported_as_missing(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(web_app.remove_workspace("missing"))
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
