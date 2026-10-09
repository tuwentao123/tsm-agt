import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import SessionInputAction, TaskBindingKind, TaskState


class SessionBindingShadowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.app = compose_fixture_application(
            model_adapter=EchoModelProvider(), tool_adapters=()
        )
        await self.app.registry.start_all()
        self.addAsyncCleanup(self.app.registry.stop_all)

    async def test_shadow_event_is_hash_only_and_preserves_legacy_resolution(self) -> None:
        secret = "正文绝不能写入 shadow event"
        with tempfile.TemporaryDirectory() as directory:
            session = await self.app.kernel.create_session("shadow")
            decision = await self.app.kernel.resolve_session_input(
                session.session_id, secret, Path(directory)
            )

            events = await self.app.kernel.dependencies.store.read_session_events(
                session.session_id
            )
            resolved = next(
                event for event in events if event.event_type == "session.input_resolved"
            )
            shadow = next(
                event
                for event in events
                if event.event_type == "session.single_loop_binding_shadow"
            )

        self.assertEqual(decision.action, SessionInputAction.NEW_TASK)
        self.assertEqual(resolved.payload["disposition"], "CREATE_TASK")
        self.assertEqual(shadow.payload["schema_version"], 1)
        self.assertEqual(shadow.payload["binding_kind"], TaskBindingKind.NEW.value)
        self.assertIsNone(shadow.payload["task_id"])
        self.assertIsNone(shadow.payload["source_task_id"])
        self.assertTrue(shadow.payload["current_input_event_ref"].startswith("input:"))
        self.assertFalse(shadow.payload["requires_runtime_inspection"])
        self.assertTrue(shadow.payload["legacy_matches"])
        self.assertIsNone(shadow.payload["difference_reason"])
        self.assertNotIn(secret, json.dumps(shadow.payload, ensure_ascii=False))

    async def test_shadow_disagreement_does_not_change_legacy_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = await self.app.kernel.create_session("shadow mismatch")
            task = await self.app.kernel.create_task(
                "running work", root, session_id=session.session_id
            )
            for state in (
                TaskState.INTAKE,
                TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS,
                TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await self.app.kernel.transition_task(
                    task.task_id, state, state.value
                )

            decision = await self.app.kernel.resolve_session_input(
                session.session_id, "new unrelated request", root
            )
            events = await self.app.kernel.dependencies.store.read_session_events(
                session.session_id
            )
            shadow = next(
                event
                for event in reversed(events)
                if event.event_type == "session.single_loop_binding_shadow"
            )

        self.assertEqual(decision.action, SessionInputAction.NEW_TASK)
        self.assertEqual(
            shadow.payload["binding_kind"],
            TaskBindingKind.ACTIVE_CONTINUATION.value,
        )
        self.assertFalse(shadow.payload["legacy_matches"])
        self.assertEqual(shadow.payload["difference_reason"], "binding_kind_mismatch")
        self.assertEqual(
            shadow.payload["legacy_decision"]["disposition"], "CREATE_TASK"
        )
