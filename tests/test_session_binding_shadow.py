"""The legacy resolver keeps its audit without a second shadow authority."""
import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import SessionInputAction, TaskState


class SessionBindingShadowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = compose_fixture_application(model_adapter=EchoModelProvider(), tool_adapters=())
        await self.app.registry.start_all()
        self.addAsyncCleanup(self.app.registry.stop_all)

    async def test_resolver_audit_is_redacted_without_retired_shadow_event(self):
        secret = "正文绝不能写入 shadow event"
        with tempfile.TemporaryDirectory() as directory:
            session = await self.app.kernel.create_session("audit")
            decision = await self.app.kernel.resolve_session_input(session.session_id, secret, Path(directory))
            events = await self.app.kernel.dependencies.store.read_session_events(session.session_id)
        self.assertIs(decision.action, SessionInputAction.NEW_TASK)
        resolved = next(e for e in events if e.event_type == "session.input_resolved")
        self.assertEqual(resolved.payload["disposition"], "CREATE_TASK")
        self.assertNotIn(secret, json.dumps(resolved.payload, ensure_ascii=False))
        self.assertFalse(any(e.event_type == "session.single_loop_binding_shadow" for e in events))

    async def test_resolving_input_does_not_execute_or_transition_running_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = await self.app.kernel.create_session("running")
            task = await self.app.kernel.create_task("running work", root, session_id=session.session_id)
            for state in (TaskState.INTAKE, TaskState.RESOLVING_PROJECT, TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING):
                task = await self.app.kernel.transition_task(task.task_id, state, state.value)
            await self.app.kernel.resolve_session_input(session.session_id, "more context", root)
            self.assertIs((await self.app.kernel.get_task(task.task_id)).state, TaskState.EXECUTING)
            self.assertEqual(len(await self.app.kernel.list_session_tasks(session.session_id)), 1)
