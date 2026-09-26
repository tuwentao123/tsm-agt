"""One client process must be able to serve several workspaces.

A Web UI keeps a workspace per conversation while sharing a single Runtime
client. If the workspace only reaches the Runtime as text inside the goal, every
Task silently runs in the client's own directory, and a Session about project B
reports facts about project A. These tests pin the workspace as a real Task
parameter, and pin the invariant that one Session stays in one workspace.
"""

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.sdk import EngineeringAgentClient


class MultiWorkspaceTaskTest(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, root: Path) -> EngineeringAgentClient:
        app = compose_fixture_application(
            model_adapter=EchoModelProvider(), tool_adapters=(),
        )
        client = EngineeringAgentClient(root, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        return client

    @staticmethod
    def _workspaces(directory: str) -> tuple[Path, Path]:
        primary = Path(directory) / "primary"
        secondary = Path(directory) / "secondary"
        primary.mkdir()
        secondary.mkdir()
        return primary, secondary

    async def test_task_runs_in_the_named_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary, secondary = self._workspaces(directory)
            client = await self.make_client(primary)

            task = await client.create_task(
                "inspect the other project", command_id="request-other",
                workspace=secondary,
            )

            self.assertEqual(Path(task.workspace), secondary.resolve())

    async def test_omitting_the_workspace_keeps_the_client_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary, _ = self._workspaces(directory)
            client = await self.make_client(primary)

            task = await client.create_task(
                "inspect this project", command_id="request-default",
            )

            self.assertEqual(Path(task.workspace), primary.resolve())

    async def test_session_text_routes_into_the_named_workspace(self) -> None:
        """The high-level Session ingress must honour the workspace too."""
        with tempfile.TemporaryDirectory() as directory:
            primary, secondary = self._workspaces(directory)
            client = await self.make_client(primary)
            session = await client.application.kernel.create_session("other project")

            result = await client.submit_session_text(
                session.session_id, "find the review list endpoint",
                command_id="request-session", workspace=secondary,
            )

            self.assertEqual(result.result["kind"], "task")
            task = await client.application.kernel.get_task(
                result.result["task"]["task_id"]
            )
            self.assertEqual(Path(task.workspace), secondary.resolve())

    async def test_session_cannot_switch_workspace_midway(self) -> None:
        """A Session's own history is the source of truth for its workspace."""
        with tempfile.TemporaryDirectory() as directory:
            primary, secondary = self._workspaces(directory)
            client = await self.make_client(primary)
            session = await client.application.kernel.create_session("bound once")
            await client.create_task(
                "first task", command_id="request-first",
                session_id=session.session_id, workspace=secondary,
            )

            with self.assertRaises(ValueError) as caught:
                await client.create_task(
                    "second task", command_id="request-second",
                    session_id=session.session_id, workspace=primary,
                )

            message = str(caught.exception)
            self.assertIn("session workspace mismatch", message)
            self.assertIn(str(secondary.resolve()), message)

    async def test_two_sessions_can_use_two_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary, secondary = self._workspaces(directory)
            client = await self.make_client(primary)
            here = await client.application.kernel.create_session("here")
            there = await client.application.kernel.create_session("there")

            first = await client.create_task(
                "task here", command_id="request-here",
                session_id=here.session_id, workspace=primary,
            )
            second = await client.create_task(
                "task there", command_id="request-there",
                session_id=there.session_id, workspace=secondary,
            )

            self.assertEqual(Path(first.workspace), primary.resolve())
            self.assertEqual(Path(second.workspace), secondary.resolve())


class _RecordingClient:
    """Capture what the Web layer actually forwards to the Runtime."""

    def __init__(self) -> None:
        self.session_calls: list[dict] = []
        self.task_calls: list[dict] = []
        kernel = type("_Kernel", (), {
            "get_session": self._get_session,
            "create_session": self._create_session,
        })()
        self.application = type("_App", (), {"kernel": kernel})()

    async def _get_session(self, _session_id: str):
        return object()

    async def _create_session(self, _title: str, *, session_id: str):
        return object()

    async def submit_session_text(
        self, session_id, text, *, command_id, images=(), workspace=None,
    ):
        self.session_calls.append({
            "session_id": session_id, "text": text, "workspace": workspace,
        })
        return type("_Result", (), {"result": {"kind": "task"}})()

    async def submit_user_input(
        self, session_id, text, *, input_id, explicit_intent=None,
        target_task_id=None, images=(), workspace=None,
    ):
        self.session_calls.append({
            "session_id": session_id, "text": text, "workspace": workspace,
        })
        return type("_Result", (), {"result": {"kind": "task"}})()

    async def submit_task(
        self, goal, *, command_id, session_id=None, images=(), workspace=None,
    ):
        self.task_calls.append({"goal": goal, "workspace": workspace})
        return type("_Result", (), {"to_data": lambda self: {"task_id": "t-1"}})()


class _RecordingServer:
    def __init__(self, client: _RecordingClient) -> None:
        self._client = client

    def _call(self, awaitable, *, timeout: float = 70):
        try:
            awaitable.send(None)
        except StopIteration as stop:
            return stop.value
        raise AssertionError("recording coroutines must not await")


class WebWorkspaceForwardingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.web_app = importlib.import_module("tsm_agt.web.app")
        self.client = _RecordingClient()
        self.workspace = "/tmp/AppHitchMainH5"
        self._patches = [
            mock.patch.object(
                self.web_app, "runtime_server", _RecordingServer(self.client)
            ),
            mock.patch.object(
                self.web_app, "workspace_registry",
                [{"id": "ws-1", "name": "AppHitchMainH5", "path": self.workspace}],
            ),
            mock.patch.object(self.web_app, "session_workspace_bindings", {}),
            mock.patch.object(
                self.web_app, "save_session_workspace_bindings", lambda: None
            ),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    async def test_session_input_forwards_the_bound_workspace(self) -> None:
        await self.web_app.submit_session_input({
            "workspace_id": "ws-1", "session_id": "session-1",
            "request_id": "request-1", "text": "find the review list endpoint",
        })

        self.assertEqual(len(self.client.session_calls), 1)
        self.assertEqual(
            self.client.session_calls[0]["workspace"], Path(self.workspace)
        )

    async def test_task_ingress_forwards_workspace_instead_of_goal_text(self) -> None:
        await self.web_app.create_task({
            "workspace_id": "ws-1", "workspace_path": self.workspace,
            "session_id": "session-2", "goal": "implement the endpoint",
        })

        self.assertEqual(len(self.client.task_calls), 1)
        call = self.client.task_calls[0]
        self.assertEqual(call["workspace"], Path(self.workspace))
        # The goal must stay the user's request; the workspace is a parameter.
        self.assertEqual(call["goal"], "implement the endpoint")
        self.assertNotIn("workspace-context", call["goal"])


if __name__ == "__main__":
    unittest.main()
