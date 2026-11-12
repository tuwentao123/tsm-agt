"""Attached roots on one Web workspace persist and reach the created Task."""

from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException


class _Result:
    def to_data(self) -> dict:
        return {"task_id": "task-1", "state": "CREATED"}


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def submit_task(self, goal, **kwargs):
        self.calls.append({"goal": goal, **kwargs})
        return _Result()


class _RecordingServer:
    def __init__(self, client) -> None:
        self._client = client

    def _call(self, awaitable):
        try:
            awaitable.send(None)
        except StopIteration as stop:
            return stop.value
        raise AssertionError("fake coroutines must not await")


class WebWorkspaceRootsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.web = importlib.import_module("tsm_agt.web.app")
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        self.shared = self.base / "shared"
        self.other = self.base / "other"
        for path in (self.project, self.shared, self.other):
            path.mkdir()
        self.registry: list[dict] = []
        self.registry_path = self.base / ".agent" / "web-workspaces.json"
        self._patches = [
            mock.patch.object(self.web, "workspace_registry", self.registry),
            mock.patch.object(self.web, "removed_workspace_paths", set()),
            mock.patch.object(
                self.web, "workspace_registry_path", lambda: self.registry_path
            ),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.temporary.cleanup)

    def _register(self) -> dict:
        workspace = self.web.register_workspace(str(self.project))
        self.assertEqual(workspace["roots"], [])
        return workspace

    async def test_attached_root_persists_and_is_passed_to_the_task(self) -> None:
        workspace = self._register()
        attached = await self.web.attach_workspace_root(
            workspace["id"], {"path": str(self.shared)}
        )
        self.assertEqual(attached["roots"], [str(self.shared.resolve())])
        self.assertTrue(self.registry_path.exists())

        # A restart reloads the registry from disk.
        self.registry.clear()
        self.web.load_workspace_registry()
        self.assertEqual(self.registry[0]["roots"], [str(self.shared.resolve())])

        client = _RecordingClient()
        with mock.patch.object(self.web, "runtime_server", _RecordingServer(client)):
            await self.web.create_task({
                "workspace_id": workspace["id"],
                "workspace_path": str(self.project),
                "goal": "hello",
            })
        self.assertEqual(
            client.calls[0]["workspace_roots"], (str(self.shared.resolve()),)
        )

    async def test_detaching_a_root_removes_it(self) -> None:
        workspace = self._register()
        await self.web.attach_workspace_root(
            workspace["id"], {"path": str(self.shared)}
        )
        await self.web.attach_workspace_root(
            workspace["id"], {"path": str(self.other)}
        )
        detached = await self.web.detach_workspace_root(
            workspace["id"], path=str(self.shared)
        )
        self.assertEqual(detached["roots"], [str(self.other.resolve())])
        self.registry.clear()
        self.web.load_workspace_registry()
        self.assertEqual(self.registry[0]["roots"], [str(self.other.resolve())])

    async def test_attaching_the_primary_directory_is_rejected(self) -> None:
        workspace = self._register()
        with self.assertRaises(HTTPException) as failure:
            await self.web.attach_workspace_root(
                workspace["id"], {"path": str(self.project)}
            )
        self.assertEqual(failure.exception.status_code, 400)
        self.assertEqual(self.registry[0]["roots"], [])

    async def test_attaching_a_missing_directory_is_rejected(self) -> None:
        workspace = self._register()
        with self.assertRaises(HTTPException) as failure:
            await self.web.attach_workspace_root(
                workspace["id"], {"path": str(self.base / "absent")}
            )
        self.assertEqual(failure.exception.status_code, 400)

    async def test_disappeared_root_is_dropped_on_reload(self) -> None:
        workspace = self._register()
        await self.web.attach_workspace_root(
            workspace["id"], {"path": str(self.shared)}
        )
        for entry in self.project.iterdir():
            pass
        import shutil

        shutil.rmtree(self.shared)
        self.registry.clear()
        self.web.load_workspace_registry()
        self.assertEqual(self.registry[0]["roots"], [])


if __name__ == "__main__":
    unittest.main()
