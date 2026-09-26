from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.ports import AdapterContext, RuntimeEvent, RuntimeUnitOfWork


def context() -> AdapterContext:
    return AdapterContext(config={}, emit_event=lambda _type, _payload: None)


class SQLiteRuntimeStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "runtime.db"
        self.store = SQLiteRuntimeStore(self.database)
        await self.store.start(context())

    async def asyncTearDown(self) -> None:
        await self.store.stop(datetime.now(timezone.utc))
        self.temp_dir.cleanup()

    async def test_state_and_events_survive_reopen(self) -> None:
        event = RuntimeEvent(
            "evt-1", "task-1", 1, "task.created", {"goal": "你好"}
        )
        await self.store.commit(
            RuntimeUnitOfWork(
                "task-1", 0, {"state": "CREATED", "goal": "你好"}, (event,)
            )
        )
        await self.store.stop(datetime.now(timezone.utc))

        reopened = SQLiteRuntimeStore(self.database)
        await reopened.start(context())
        try:
            stored = await reopened.load_task("task-1")
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored.data["goal"], "你好")
            self.assertEqual(stored.version, 1)
            self.assertEqual(await reopened.read_events("task-1"), (event,))
        finally:
            await reopened.stop(datetime.now(timezone.utc))

    async def test_cursor_returns_only_later_events_in_order(self) -> None:
        events = (
            RuntimeEvent("evt-1", "task-1", 1, "task.created"),
            RuntimeEvent("evt-2", "task-1", 2, "turn.started"),
            RuntimeEvent("evt-3", "task-1", 3, "turn.completed"),
        )
        await self.store.commit(
            RuntimeUnitOfWork("task-1", 0, {"state": "EXECUTING"}, events)
        )

        later = await self.store.read_events("task-1", after_sequence=1)
        self.assertEqual([event.sequence for event in later], [2, 3])

    async def test_version_conflict_does_not_modify_state_or_events(self) -> None:
        first = RuntimeUnitOfWork(
            "task-1",
            0,
            {"state": "CREATED"},
            (RuntimeEvent("evt-1", "task-1", 1, "task.created"),),
        )
        await self.store.commit(first)

        with self.assertRaisesRegex(ValueError, "version conflict"):
            await self.store.commit(
                RuntimeUnitOfWork(
                    "task-1",
                    0,
                    {"state": "FAILED"},
                    (RuntimeEvent("evt-2", "task-1", 2, "task.failed"),),
                )
            )
        stored = await self.store.load_task("task-1")
        assert stored is not None
        self.assertEqual(stored.data["state"], "CREATED")
        self.assertEqual(len(await self.store.read_events("task-1")), 1)

    async def test_non_contiguous_events_rollback_entire_transaction(self) -> None:
        with self.assertRaisesRegex(ValueError, "event sequence"):
            await self.store.commit(
                RuntimeUnitOfWork(
                    "task-1",
                    0,
                    {"state": "CREATED"},
                    (RuntimeEvent("evt-2", "task-1", 2, "task.created"),),
                )
            )
        self.assertIsNone(await self.store.load_task("task-1"))
        self.assertEqual(await self.store.read_events("task-1"), ())

    async def test_duplicate_event_id_rolls_back_task_update(self) -> None:
        await self.store.commit(
            RuntimeUnitOfWork(
                "task-1",
                0,
                {"state": "CREATED"},
                (RuntimeEvent("evt-shared", "task-1", 1, "task.created"),),
            )
        )
        with self.assertRaisesRegex(ValueError, "integrity violation"):
            await self.store.commit(
                RuntimeUnitOfWork(
                    "task-2",
                    0,
                    {"state": "CREATED"},
                    (RuntimeEvent("evt-shared", "task-2", 1, "task.created"),),
                )
            )
        self.assertIsNone(await self.store.load_task("task-2"))

    async def test_non_json_state_fails_before_transaction(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON serializable"):
            await self.store.commit(
                RuntimeUnitOfWork(
                    "task-1", 0, {"bad": object()}, ()
                )
            )
        self.assertIsNone(await self.store.load_task("task-1"))


    async def test_creation_and_final_commands_share_session_task_targets(self) -> None:
        from tsm_agt.ports import (
            SessionEvent,
            SessionTaskUnitOfWork,
            SessionUnitOfWork,
        )

        creation = SessionTaskUnitOfWork(
            SessionUnitOfWork(
                "session-1", 0, {"subject": "uid:1", "state": "ACTIVE"},
                (SessionEvent("sevt-1", "session-1", 1, "session.created"),),
            ),
            RuntimeUnitOfWork(
                "task-1", 0, {"state": "CREATED"},
                (RuntimeEvent("evt-1", "task-1", 1, "task.created"),),
            ),
            "create-command",
        )
        final = SessionTaskUnitOfWork(
            SessionUnitOfWork(
                "session-1", 1, {"subject": "uid:1", "state": "ACTIVE"},
                (SessionEvent("sevt-2", "session-1", 2, "session.answer_recorded"),),
            ),
            RuntimeUnitOfWork(
                "task-1", 1, {"state": "COMPLETED"},
                (RuntimeEvent("evt-2", "task-1", 2, "turn.completed"),),
            ),
            "final-command",
        )

        await self.store.commit_session_and_task(creation)
        session_result, task_result = await self.store.commit_session_and_task(final)

        self.assertEqual((session_result.committed_version, task_result.committed_version), (2, 2))
        self.assertEqual(
            await self.store.load_session_task_command("create-command"),
            ("session-1", "task-1"),
        )
        self.assertEqual(
            await self.store.load_session_task_command("final-command"),
            ("session-1", "task-1"),
        )
        self.assertEqual(
            [event.sequence for event in await self.store.read_session_events("session-1")],
            [1, 2],
        )
        self.assertEqual(
            [event.sequence for event in await self.store.read_events("task-1")],
            [1, 2],
        )

    async def test_final_command_replay_is_idempotent_and_reuse_with_other_targets_fails(self) -> None:
        from tsm_agt.ports import (
            SessionEvent,
            SessionTaskUnitOfWork,
            SessionUnitOfWork,
        )

        first = SessionTaskUnitOfWork(
            SessionUnitOfWork(
                "session-1", 0, {"subject": "uid:1", "state": "ACTIVE"},
                (SessionEvent("sevt-1", "session-1", 1, "session.created"),),
            ),
            RuntimeUnitOfWork(
                "task-1", 0, {"state": "CREATED"},
                (RuntimeEvent("evt-1", "task-1", 1, "task.created"),),
            ),
            "create-command",
        )
        final = SessionTaskUnitOfWork(
            SessionUnitOfWork(
                "session-1", 1, {"subject": "uid:1", "state": "ACTIVE"},
                (SessionEvent("sevt-2", "session-1", 2, "session.answer_recorded"),),
            ),
            RuntimeUnitOfWork(
                "task-1", 1, {"state": "COMPLETED"},
                (RuntimeEvent("evt-2", "task-1", 2, "turn.completed"),),
            ),
            "final-command",
        )
        await self.store.commit_session_and_task(first)
        await self.store.commit_session_and_task(final)

        replay = await self.store.commit_session_and_task(final)
        self.assertEqual(
            (replay[0].committed_version, replay[1].committed_version), (2, 2)
        )
        self.assertEqual(len(await self.store.read_session_events("session-1")), 2)
        self.assertEqual(len(await self.store.read_events("task-1")), 2)

        with self.assertRaisesRegex(ValueError, "reused with different targets"):
            await self.store.commit_session_and_task(SessionTaskUnitOfWork(
                SessionUnitOfWork(
                    "session-1", 2, {"subject": "uid:1", "state": "ACTIVE"}, (),
                ),
                RuntimeUnitOfWork("task-2", 0, {"state": "CREATED"}, ()),
                "final-command",
            ))

    async def test_session_task_command_failure_rolls_back_both_records_and_receipt(self) -> None:
        from tsm_agt.ports import (
            SessionEvent,
            SessionTaskUnitOfWork,
            SessionUnitOfWork,
        )

        with self.assertRaisesRegex(ValueError, "invalid Task Event"):
            await self.store.commit_session_and_task(SessionTaskUnitOfWork(
                SessionUnitOfWork(
                    "session-1", 0, {"subject": "uid:1", "state": "ACTIVE"},
                    (SessionEvent("sevt-1", "session-1", 1, "session.created"),),
                ),
                RuntimeUnitOfWork(
                    "task-1", 0, {"state": "CREATED"},
                    (RuntimeEvent("evt-1", "task-1", 2, "task.created"),),
                ),
                "failed-command",
            ))

        self.assertIsNone(await self.store.load_session("session-1"))
        self.assertIsNone(await self.store.load_task("task-1"))
        self.assertIsNone(await self.store.load_session_task_command("failed-command"))

    async def test_v1_session_task_command_schema_migrates_without_losing_receipts(self) -> None:
        import sqlite3
        from tsm_agt.ports import (
            SessionEvent,
            SessionTaskUnitOfWork,
            SessionUnitOfWork,
        )

        await self.store.stop(datetime.now(timezone.utc))
        self.database.unlink()
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(
                """
                CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO runtime_meta(key, value) VALUES ('schema_version', '1');
                CREATE TABLE runtime_tasks (
                    task_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                    last_event_sequence INTEGER NOT NULL, data_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE runtime_sessions (
                    session_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                    last_event_sequence INTEGER NOT NULL, subject TEXT NOT NULL,
                    state TEXT NOT NULL, data_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE session_task_commands (
                    command_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(session_id, task_id),
                    FOREIGN KEY(session_id) REFERENCES runtime_sessions(session_id),
                    FOREIGN KEY(task_id) REFERENCES runtime_tasks(task_id)
                );
                """
            )
            created_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "INSERT INTO runtime_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("session-1", 1, 0, "uid:1", "ACTIVE", '{"state":"ACTIVE"}', created_at),
            )
            connection.execute(
                "INSERT INTO runtime_tasks VALUES (?, ?, ?, ?, ?)",
                ("task-1", 1, 0, '{"state":"CREATED"}', created_at),
            )
            connection.execute(
                "INSERT INTO session_task_commands VALUES (?, ?, ?, ?)",
                ("historic-command", "session-1", "task-1", created_at),
            )
            connection.commit()
        finally:
            connection.close()

        self.store = SQLiteRuntimeStore(self.database)
        await self.store.start(context())
        self.assertEqual(
            await self.store.load_session_task_command("historic-command"),
            ("session-1", "task-1"),
        )
        version = self.store._require_connection().execute(
            "SELECT value FROM runtime_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        self.assertEqual(version, "2")

        await self.store.commit_session_and_task(SessionTaskUnitOfWork(
            SessionUnitOfWork(
                "session-1", 1, {"subject": "uid:1", "state": "ACTIVE"},
                (SessionEvent("sevt-1", "session-1", 1, "session.answer_recorded"),),
            ),
            RuntimeUnitOfWork(
                "task-1", 1, {"state": "COMPLETED"},
                (RuntimeEvent("evt-1", "task-1", 1, "turn.completed"),),
            ),
            "final-command",
        ))
        self.assertEqual(
            await self.store.load_session_task_command("final-command"),
            ("session-1", "task-1"),
        )


if __name__ == "__main__":
    unittest.main()
