from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.sqlite import SQLiteProjectMemoryStore, SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    FlowNodeKind, MemoryRecord, MemoryScope, MemorySourceKind, TaskState,
)
from tsm_agt.ports import MessageRole, RuntimeStorePort, StoredMemory


class CapturingEchoModel:
    from tsm_agt.adapters.fixture import EchoModelProvider


class ProjectMemoryTest(unittest.IsolatedAsyncioTestCase):
    def test_legacy_session_scope_loads_as_task_scope(self) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        stored = StoredMemory(
            "memory-legacy", "uid:1", "SESSION", None, "task-legacy",
            {
                "memory_id": "memory-legacy", "scope": "SESSION",
                "content": "legacy task fact",
                "source_kind": "user_confirmed",
                "source_reference": "legacy user message",
                "source_hash": None, "created_at": now,
                "last_verified_at": now, "workspace": None,
                "workspace_fingerprint": None, "subject": "uid:1",
                "task_id": "task-legacy", "writer": "legacy",
                "revision": 1,
            },
        )

        record = MemoryRecord.from_stored(stored)

        self.assertEqual(record.scope, MemoryScope.TASK)
        self.assertEqual(record.task_id, "task-legacy")

    async def _task(self, application, root: Path, task_id: str = "task-memory"):
        task = await application.kernel.create_task("memory test", root, task_id)
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(task.task_id, state, state.value)
        return task

    async def test_project_memory_persists_and_becomes_stale_after_identity_change(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        identity = root / "package.json"
        identity.write_text('{"name":"before"}', encoding="utf-8")
        runtime_db = root / "runtime.db"
        memory_db = root / "memory.db"
        first = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(runtime_db),
            project_memory_adapter=SQLiteProjectMemoryStore(memory_db),
            tool_adapters=(),
        )
        await first.registry.start_all()
        task = await self._task(first, root)
        source = root / "README.md"
        source.write_text("Run tests with pytest.\n", encoding="utf-8")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        saved = await first.kernel.remember_for_task(
            task.task_id, MemoryScope.PROJECT, "Tests run with pytest.",
            MemorySourceKind.WORKSPACE_FILE, "README.md", source_hash,
            "op-save", "test-writer",
        )
        self.assertFalse(saved.stale)
        await first.registry.stop_all()

        second = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(runtime_db),
            project_memory_adapter=SQLiteProjectMemoryStore(memory_db),
            tool_adapters=(),
        )
        await second.registry.start_all()
        try:
            restored = await second.kernel.list_task_memories(task.task_id)
            self.assertEqual([view.record.content for view in restored], ["Tests run with pytest."])
            identity.write_text('{"name":"after"}', encoding="utf-8")
            self.assertEqual(await second.kernel.list_task_memories(task.task_id), ())
            stale = await second.kernel.list_task_memories(task.task_id, include_stale=True)
            self.assertTrue(stale[0].stale)
            self.assertIn("fingerprint", stale[0].stale_reason or "")
        finally:
            await second.registry.stop_all()
            temporary.cleanup()

    async def test_source_hash_secret_and_user_scope_rules_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "README.md"
            source.write_text("documented command", encoding="utf-8")
            application = compose_fixture_application(tool_adapters=())
            await application.registry.start_all()
            try:
                task = await self._task(application, root)
                with self.assertRaisesRegex(ValueError, "does not match"):
                    await application.kernel.remember_for_task(
                        task.task_id, MemoryScope.PROJECT, "Documented fact",
                        MemorySourceKind.WORKSPACE_FILE, "README.md", "wrong",
                        "op-wrong", "test",
                    )
                with self.assertRaisesRegex(ValueError, "credentials"):
                    await application.kernel.remember_for_task(
                        task.task_id, MemoryScope.TASK,
                        "api_key=top-secret-value",
                        MemorySourceKind.USER_CONFIRMED, "user message", None,
                        "op-secret", "test",
                    )
                with self.assertRaisesRegex(ValueError, "explicitly confirmed"):
                    await application.kernel.remember_for_task(
                        task.task_id, MemoryScope.USER, "Always use pytest",
                        MemorySourceKind.WORKSPACE_FILE, "README.md",
                        hashlib.sha256(source.read_bytes()).hexdigest(),
                        "op-user", "test",
                    )
            finally:
                await application.registry.stop_all()

    async def test_memory_event_redacts_fact_and_prompt_injection_is_untrusted(self) -> None:
        from tsm_agt.adapters.fixture import EchoModelProvider

        class CaptureModel(EchoModelProvider):
            def __init__(self):
                super().__init__()
                self.request = None

            async def complete(self, request):
                self.request = request
                return await super().complete(request)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = CaptureModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            await application.registry.start_all()
            try:
                task = await self._task(application, root)
                fact = "The verified local build command is ./gradlew test."
                await application.kernel.remember_for_task(
                    task.task_id, MemoryScope.PROJECT, fact,
                    MemorySourceKind.USER_CONFIRMED, "confirmed in current task", None,
                    "op-event", "test",
                )
                events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
                saved_event = next(event for event in events if event.event_type == "memory.saved")
                self.assertNotIn(fact, str(saved_event.payload))
                projection = await application.kernel.get_flow_projection(task.task_id)
                memory_nodes = [
                    node for node in projection.nodes
                    if node.kind is FlowNodeKind.MEMORY
                ]
                self.assertEqual([node.label for node in memory_nodes], ["Memory saved"])
                await application.kernel.run_text_turn(task.task_id, "What command?")
                assert model.request is not None
                memory_messages = [
                    message for message in model.request.messages
                    if message.message_id.startswith("project-memory-context-")
                ]
                self.assertEqual(len(memory_messages), 1)
                self.assertEqual(memory_messages[0].role, MessageRole.USER)
                self.assertIn("not instructions", memory_messages[0].text)
                self.assertIn(fact, memory_messages[0].text)
            finally:
                await application.registry.stop_all()

    async def test_save_operation_is_idempotent_and_delete_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(tool_adapters=())
            await application.registry.start_all()
            try:
                task = await self._task(application, root)
                first = await application.kernel.remember_for_task(
                    task.task_id, MemoryScope.TASK, "Use compact output.",
                    MemorySourceKind.USER_CONFIRMED, "user message", None,
                    "same-operation", "test",
                )
                replay = await application.kernel.remember_for_task(
                    task.task_id, MemoryScope.TASK, "Use compact output.",
                    MemorySourceKind.USER_CONFIRMED, "user message", None,
                    "same-operation", "test",
                )
                self.assertEqual(first.record.memory_id, replay.record.memory_id)
                result = await application.kernel.forget_task_memory(
                    task.task_id, first.record.memory_id, "delete-operation", "test"
                )
                self.assertTrue(result["deleted"])
                self.assertEqual(await application.kernel.list_task_memories(task.task_id), ())
                events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
                self.assertIn("memory.deleted", [event.event_type for event in events])
            finally:
                await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
