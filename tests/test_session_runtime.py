from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AgentCheckpointConflict, MemoryScope, MemorySourceKind, SessionSnapshot,
    SessionState, TaskSnapshot, TaskState, standalone_session_id,
)
from tsm_agt.ports import (
    AdapterDescriptor, ModelRequest, RuntimeEvent, RuntimeStorePort,
    RuntimeUnitOfWork, SessionEvent, SessionTaskUnitOfWork, SessionUnitOfWork,
)


class BlockingModel(EchoModelProvider):
    descriptor = AdapterDescriptor(
        "fixture.session-blocking-model", "1.0", "ModelProviderPort", "1.0"
    )

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def complete(self, request: ModelRequest):
        self.entered.set()
        await asyncio.Event().wait()


class SessionRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_follow_up_queue_persists_lists_and_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first = compose_fixture_application(
                store_adapter=SQLiteRuntimeStore(database)
            )
            await first.registry.start_all()
            session = await first.kernel.create_session("queue")
            task = await first.kernel.create_task(
                "first", root, session_id=session.session_id
            )
            one = await first.kernel.queue_session_follow_up(
                session.session_id, task.task_id, "then test", "follow-1"
            )
            replay = await first.kernel.queue_session_follow_up(
                session.session_id, task.task_id, "then test", "follow-1"
            )
            self.assertEqual(one, replay)
            await first.registry.stop_all()

            second = compose_fixture_application(
                store_adapter=SQLiteRuntimeStore(database)
            )
            await second.registry.start_all()
            try:
                pending = await second.kernel.list_queued_session_follow_ups(
                    session.session_id
                )
                self.assertEqual(pending, (one,))
                self.assertTrue(await second.kernel.cancel_session_follow_up(
                    session.session_id, one.input_id
                ))
                self.assertFalse(await second.kernel.cancel_session_follow_up(
                    session.session_id, one.input_id
                ))
                self.assertEqual(
                    await second.kernel.list_queued_session_follow_ups(
                        session.session_id
                    ), ()
                )
            finally:
                await second.registry.stop_all()

    async def test_follow_up_consume_requires_terminal_source_and_records_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("follow-up")
                source = await app.kernel.create_task(
                    "source", root, session_id=session.session_id
                )
                queued = await app.kernel.queue_session_follow_up(
                    session.session_id, source.task_id, "after failure", "follow-terminal"
                )
                self.assertIsNone(await app.kernel.consume_queued_session_follow_up(
                    session.session_id, root, input_id=queued.input_id
                ))
                await app.kernel.transition_task(
                    source.task_id, TaskState.FAILED, "source failed"
                )
                dispatched = await app.kernel.consume_queued_session_follow_up(
                    session.session_id, root, input_id=queued.input_id
                )
                assert dispatched is not None
                self.assertEqual(dispatched.goal, "after failure")
                self.assertEqual(dispatched.session_id, session.session_id)
                events = await app.registry.require(RuntimeStorePort).read_session_events(
                    session.session_id
                )
                self.assertEqual(
                    [event.event_type for event in events[-3:]],
                    [
                        "session.task_attached", "session.task_derived",
                        "session.follow_up_dispatched",
                    ],
                )
                created = await app.registry.require(RuntimeStorePort).read_events(
                    dispatched.task_id
                )
                self.assertEqual(created[0].payload["source_task_id"], source.task_id)
                self.assertEqual(created[0].payload["task_relation"], "FOLLOW_UP")
            finally:
                await app.registry.stop_all()

    async def test_follow_up_consume_uses_first_ready_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("follow-up ordering")
                waiting = await app.kernel.create_task(
                    "waiting", root, session_id=session.session_id
                )
                ready = await app.kernel.create_task(
                    "ready", root, session_id=session.session_id
                )
                first = await app.kernel.queue_session_follow_up(
                    session.session_id, waiting.task_id, "wait for this", "follow-waiting"
                )
                second = await app.kernel.queue_session_follow_up(
                    session.session_id, ready.task_id, "consume this", "follow-ready"
                )
                await app.kernel.transition_task(
                    ready.task_id, TaskState.CANCELLED, "source cancelled"
                )
                dispatched = await app.kernel.consume_queued_session_follow_up(
                    session.session_id, root
                )
                assert dispatched is not None
                self.assertEqual(dispatched.goal, second.text)
                replay = await app.kernel.consume_queued_session_follow_up(
                    session.session_id, root, input_id=second.input_id
                )
                self.assertEqual(replay, dispatched)
                self.assertEqual(
                    await app.kernel.list_queued_session_follow_ups(session.session_id),
                    (first,),
                )
                events = await app.registry.require(RuntimeStorePort).read_session_events(
                    session.session_id
                )
                self.assertEqual(
                    sum(
                        event.event_type == "session.follow_up_dispatched"
                        and event.payload.get("input_id") == second.input_id
                        for event in events
                    ),
                    1,
                )
            finally:
                await app.registry.stop_all()

    async def test_follow_up_consume_concurrent_replays_one_atomically_dispatched_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("concurrent follow-up")
                source = await app.kernel.create_task(
                    "source", root, session_id=session.session_id
                )
                queued = await app.kernel.queue_session_follow_up(
                    session.session_id, source.task_id, "after source", "follow-concurrent"
                )
                await app.kernel.transition_task(
                    source.task_id, TaskState.CANCELLED, "source cancelled"
                )
                original_get_project_trust = app.kernel.get_project_trust
                entered_create = 0
                both_creates_ready = asyncio.Event()

                async def synchronize_create(workspace: Path):
                    nonlocal entered_create
                    entered_create += 1
                    if entered_create == 2:
                        both_creates_ready.set()
                    await both_creates_ready.wait()
                    return await original_get_project_trust(workspace)

                app.kernel.get_project_trust = synchronize_create
                first, second = await asyncio.gather(
                    app.kernel.consume_queued_session_follow_up(
                        session.session_id, root, input_id=queued.input_id
                    ),
                    app.kernel.consume_queued_session_follow_up(
                        session.session_id, root, input_id=queued.input_id
                    ),
                )
                assert first is not None and second is not None
                self.assertEqual(first, second)
                self.assertEqual(first.goal, queued.text)
                self.assertEqual(
                    (await app.kernel.get_session(session.session_id)).task_ids.count(
                        first.task_id
                    ),
                    1,
                )
                events = await app.registry.require(RuntimeStorePort).read_session_events(
                    session.session_id
                )
                self.assertEqual(
                    sum(
                        event.event_type == "session.follow_up_dispatched"
                        and event.payload.get("input_id") == queued.input_id
                        for event in events
                    ),
                    1,
                )
            finally:
                await app.registry.stop_all()

    async def _executing_task(
        self, application, root: Path, task_id: str, session_id: str | None = None,
    ) -> TaskSnapshot:
        task = await application.kernel.create_task(
            task_id, root, task_id=task_id, session_id=session_id
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(task_id, state, state.value)
        return task

    def test_session_snapshot_round_trip_and_invariants(self) -> None:
        snapshot = SessionSnapshot.create("session-1", "uid:1", "Conversation")
        snapshot = snapshot.attach_task("task-1")
        self.assertEqual(SessionSnapshot.from_data(snapshot.to_data()), snapshot)
        self.assertEqual(snapshot.state, SessionState.ACTIVE)
        self.assertEqual(snapshot.active_task_id, "task-1")
        with self.assertRaisesRegex(ValueError, "already attached"):
            snapshot.attach_task("task-1")

    async def test_one_session_owns_multiple_tasks_and_task_events_link_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application()
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("multi task", "session-1")
                first = await app.kernel.create_task(
                    "first", root, "task-1", session.session_id
                )
                second = await app.kernel.create_task(
                    "second", root, "task-2", session.session_id
                )
                tasks = await app.kernel.list_session_tasks(session.session_id)
                self.assertEqual([task.task_id for task in tasks], ["task-1", "task-2"])
                self.assertEqual({task.session_id for task in tasks}, {session.session_id})
                loaded = await app.kernel.get_session(session.session_id)
                self.assertEqual(loaded.active_task_id, "task-2")
                events = await app.registry.require(RuntimeStorePort).read_events(
                    first.task_id
                )
                self.assertEqual(events[0].session_id, session.session_id)
                with self.assertRaisesRegex(ValueError, "version conflict"):
                    await app.kernel.select_session_task(
                        session.session_id, second.task_id, expected_version=1
                    )
            finally:
                await app.registry.stop_all()

    async def test_session_flow_exposes_phase1_state_alongside_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("phase1 flow")
                task = await app.kernel.create_task(
                    "phase one state", root, session_id=session.session_id
                )
                flow = await app.kernel.get_session_flow(session.session_id)
                self.assertEqual(flow["tasks"], [{
                    "task_id": task.task_id,
                    "state": TaskState.CREATED.value,
                    "phase1_state": "PREPARING",
                    "active": True,
                }])
            finally:
                await app.registry.stop_all()

    async def test_session_event_cursor_and_active_task_optimistic_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application()
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("cursor", "session-cursor")
                await app.kernel.create_task(
                    "one", root, "task-cursor-1", session.session_id
                )
                current = await app.kernel.get_session(session.session_id)
                store = app.registry.require(RuntimeStorePort)
                stored = await store.load_session(session.session_id)
                assert stored is not None
                selected = await app.kernel.select_session_task(
                    session.session_id, "task-cursor-1",
                    expected_version=stored.version,
                )
                self.assertEqual(selected.active_task_id, "task-cursor-1")
                with self.assertRaisesRegex(ValueError, "version conflict"):
                    await app.kernel.select_session_task(
                        session.session_id, "task-cursor-1",
                        expected_version=stored.version,
                    )
                later = await store.read_session_events(session.session_id, 2)
                self.assertEqual(
                    [event.event_type for event in later],
                    ["session.active_task_changed"],
                )
                with self.assertRaisesRegex(ValueError, "negative"):
                    await store.read_session_events(session.session_id, -1)
            finally:
                await app.registry.stop_all()

    async def test_task_creation_command_is_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            app = compose_fixture_application(
                store_adapter=SQLiteRuntimeStore(database), tool_adapters=()
            )
            await app.registry.start_all()
            try:
                first = await app.kernel.create_task(
                    "same", root, "task-idempotent", command_id="create-1"
                )
                replay = await app.kernel.create_task(
                    "same", root, "task-idempotent", command_id="create-1"
                )
                self.assertEqual(replay, first)
                session = await app.kernel.get_session(first.session_id)
                self.assertEqual(session.task_ids, (first.task_id,))
                store = app.registry.require(RuntimeStorePort)
                self.assertEqual(len(await store.read_session_events(first.session_id)), 2)
                with self.assertRaisesRegex(ValueError, "different Task input"):
                    await app.kernel.create_task(
                        "changed", root, "task-idempotent", command_id="create-1"
                    )
            finally:
                await app.registry.stop_all()

    async def test_session_and_task_sqlite_commit_rolls_back_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRuntimeStore(Path(directory) / "runtime.db")
            from tsm_agt.ports import AdapterContext
            await store.start(AdapterContext({}, lambda _type, _payload: None))
            try:
                session = SessionSnapshot.create("session-a", "uid:1", "atomic")
                with self.assertRaisesRegex(ValueError, "invalid Task Event"):
                    await store.commit_session_and_task(SessionTaskUnitOfWork(
                        SessionUnitOfWork(
                            "session-a", 0, session.to_data(),
                            (SessionEvent("sevt-a", "session-a", 1, "session.created"),),
                        ),
                        RuntimeUnitOfWork(
                            "task-a", 0, {"task_id": "task-a"},
                            (RuntimeEvent("evt-a", "task-a", 2, "task.created"),),
                        ),
                        "atomic-a",
                    ))
                self.assertIsNone(await store.load_session("session-a"))
                self.assertIsNone(await store.load_task("task-a"))
            finally:
                from datetime import datetime, timezone
                await store.stop(datetime.now(timezone.utc))

    async def test_task_and_session_memory_have_distinct_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("shared", "session-shared")
                first = await self._executing_task(app, root, "task-first", session.session_id)
                second = await self._executing_task(app, root, "task-second", session.session_id)
                other = await self._executing_task(app, root, "task-other")
                await app.kernel.remember_for_task(
                    first.task_id, MemoryScope.SESSION, "shared preference",
                    MemorySourceKind.USER_CONFIRMED, "user message", None,
                    "memory-session", "test",
                )
                await app.kernel.remember_for_task(
                    first.task_id, MemoryScope.TASK, "private scratch fact",
                    MemorySourceKind.USER_CONFIRMED, "user message", None,
                    "memory-task", "test",
                )
                first_facts = {
                    view.record.content for view in await app.kernel.list_task_memories(first.task_id)
                }
                second_facts = {
                    view.record.content for view in await app.kernel.list_task_memories(second.task_id)
                }
                other_facts = {
                    view.record.content for view in await app.kernel.list_task_memories(other.task_id)
                }
                self.assertEqual(first_facts, {"shared preference", "private scratch fact"})
                self.assertEqual(second_facts, {"shared preference"})
                self.assertEqual(other_facts, set())
            finally:
                await app.registry.stop_all()

    async def test_close_gate_and_archive_preserve_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application()
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("finish", root, "task-close")
                with self.assertRaisesRegex(RuntimeError, "unresolved"):
                    await app.kernel.close_session(task.session_id, "done")
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.VERIFYING, TaskState.FINALIZING, TaskState.SUCCEEDED,
                ):
                    task = await app.kernel.transition_task(task.task_id, state, state.value)
                closed = await app.kernel.close_session(task.session_id, "done")
                archived = await app.kernel.archive_session(closed.session_id)
                self.assertEqual(archived.state, SessionState.ARCHIVED)
                self.assertIsNotNone(await app.kernel.get_task(task.task_id))
                store = app.registry.require(RuntimeStorePort)
                self.assertEqual(
                    [event.event_type for event in await store.read_session_events(task.session_id)][-2:],
                    ["session.closed", "session.archived"],
                )
            finally:
                await app.registry.stop_all()

    async def test_sqlite_restart_and_legacy_task_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            snapshot = TaskSnapshot.create("legacy-task", "legacy", str(root))
            data = snapshot.to_data()
            data.pop("session_id")
            connection = sqlite3.connect(database)
            connection.executescript(
                "CREATE TABLE runtime_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "INSERT INTO runtime_meta VALUES('schema_version','1');"
                "CREATE TABLE runtime_tasks(task_id TEXT PRIMARY KEY, version INTEGER NOT NULL, "
                "last_event_sequence INTEGER NOT NULL, data_json TEXT NOT NULL, updated_at TEXT NOT NULL);"
                "CREATE TABLE runtime_events(event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
                "sequence INTEGER NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, "
                "occurred_at TEXT NOT NULL, schema_version INTEGER NOT NULL, UNIQUE(task_id, sequence));"
            )
            connection.execute(
                "INSERT INTO runtime_tasks VALUES(?,1,1,?,?)",
                (snapshot.task_id, json.dumps(data), snapshot.updated_at.isoformat()),
            )
            connection.execute(
                "INSERT INTO runtime_events VALUES(?,?,?,?,?,?,?)",
                ("legacy-event", snapshot.task_id, 1, "task.created", "{}",
                 snapshot.created_at.isoformat(), 1),
            )
            connection.commit()
            connection.close()
            store = SQLiteRuntimeStore(database)
            from tsm_agt.ports import AdapterContext
            await store.start(AdapterContext({}, lambda _type, _payload: None))
            try:
                stored_task = await store.load_task(snapshot.task_id)
                assert stored_task is not None
                expected_session = standalone_session_id(snapshot.task_id)
                self.assertEqual(stored_task.data["session_id"], expected_session)
                stored_session = await store.load_session(expected_session)
                self.assertIsNotNone(stored_session)
                assert stored_session is not None
                self.assertEqual(stored_session.data["task_ids"], [snapshot.task_id])
                self.assertEqual(
                    (await store.read_events(snapshot.task_id))[0].session_id, expected_session
                )
            finally:
                from datetime import datetime, timezone
                await store.stop(datetime.now(timezone.utc))

    async def test_session_context_change_is_safe_rebase_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = BlockingModel()
            app = compose_fixture_application(model_adapter=model, tool_adapters=())
            await app.registry.start_all()
            try:
                task = await self._executing_task(app, root, "task-checkpoint-session")
                running = asyncio.create_task(
                    app.kernel.run_agent_turn(task.task_id, "start")
                )
                await model.entered.wait()
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
                await app.kernel.remember_for_task(
                    task.task_id, MemoryScope.SESSION, "new session context",
                    MemorySourceKind.USER_CONFIRMED, "user message", None,
                    "checkpoint-memory", "test",
                )
                candidates = await app.kernel.list_session_resume_candidates(
                    task.session_id, root
                )
                selected = next(
                    item for item in candidates if item.task_id == task.task_id
                )
                self.assertEqual(selected.safety.value, "REBASE_REQUIRED")
                self.assertEqual(
                    selected.conflict_reasons, ("session_context_hash",)
                )
                self.assertEqual(
                    (await app.kernel.get_task(task.task_id)).state,
                    TaskState.EXECUTING,
                )
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
