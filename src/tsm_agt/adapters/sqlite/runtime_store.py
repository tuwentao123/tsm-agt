"""Transactional SQLite implementation of RuntimeStorePort."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    CommitResult,
    HealthState,
    HealthStatus,
    RuntimeEvent,
    RuntimeUnitOfWork,
    StoredTask,
    StoredProjectTrust,
    StoredProjectOnboarding,
    SessionEvent, StoredSession, SessionUnitOfWork, SessionTaskUnitOfWork,
    RuntimeCommandRecord,
)

_SCHEMA_VERSION = 2


class SQLiteRuntimeStore:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.sqlite-runtime-store",
        adapter_version="0.1.0",
        port_name="RuntimeStorePort",
        port_version="1.0",
        capabilities=frozenset(
            {"atomic-commit", "event-cursor", "persistent", "optimistic-lock"}
        ),
    )

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().resolve()
        self._connection: sqlite3.Connection | None = None

    @property
    def database_path(self) -> Path:
        return self._database_path

    async def start(self, context: AdapterContext) -> None:
        if self._connection is not None:
            return
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._database_path,
            isolation_level=None,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            self._initialize_schema(connection)
        except Exception:
            connection.close()
            raise
        self._connection = connection

    async def health(self) -> HealthStatus:
        connection = self._connection
        if connection is None:
            return HealthStatus(HealthState.UNHEALTHY, "SQLite store not started")
        try:
            connection.execute("SELECT 1").fetchone()
        except sqlite3.Error as error:
            return HealthStatus(HealthState.UNHEALTHY, f"SQLite check failed: {error}")
        return HealthStatus(
            HealthState.HEALTHY, f"SQLite store ready: {self._database_path}"
        )

    async def stop(self, deadline: datetime) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    async def commit(self, unit: RuntimeUnitOfWork) -> CommitResult:
        connection = self._require_connection()
        state_json = self._encode_mapping(unit.next_state, "task state")
        encoded_events = tuple(
            (event, self._encode_mapping(event.payload, "event payload"))
            for event in unit.events
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT version, last_event_sequence FROM runtime_tasks WHERE task_id = ?",
                (unit.task_id,),
            ).fetchone()
            current_version = int(row["version"]) if row is not None else 0
            last_sequence = (
                int(row["last_event_sequence"]) if row is not None else 0
            )
            if current_version != unit.expected_version:
                raise ValueError(
                    f"version conflict for {unit.task_id}: expected "
                    f"{unit.expected_version}, got {current_version}"
                )

            expected_sequence = last_sequence + 1
            for event, _payload_json in encoded_events:
                if event.task_id != unit.task_id:
                    raise ValueError("event task_id must match unit task_id")
                if event.sequence != expected_sequence:
                    raise ValueError(
                        f"event sequence must be {expected_sequence}, got {event.sequence}"
                    )
                expected_sequence += 1

            committed_version = current_version + 1
            committed_last_sequence = expected_sequence - 1
            connection.execute(
                """
                INSERT INTO runtime_tasks(
                    task_id, version, last_event_sequence, data_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    version = excluded.version,
                    last_event_sequence = excluded.last_event_sequence,
                    data_json = excluded.data_json,
                    updated_at = excluded.updated_at
                """,
                (
                    unit.task_id,
                    committed_version,
                    committed_last_sequence,
                    state_json,
                    datetime.now().astimezone().isoformat(),
                ),
            )
            for event, payload_json in encoded_events:
                connection.execute(
                    """
                    INSERT INTO runtime_events(
                        event_id, task_id, sequence, event_type, payload_json,
                        occurred_at, schema_version, session_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.task_id,
                        event.sequence,
                        event.event_type,
                        payload_json,
                        event.occurred_at.isoformat(),
                        event.schema_version,
                        event.session_id or str(unit.next_state.get("session_id") or "") or None,
                    ),
                )
            connection.execute("COMMIT")
        except sqlite3.IntegrityError as error:
            connection.execute("ROLLBACK")
            raise ValueError(f"SQLite runtime integrity violation: {error}") from error
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return CommitResult(unit.task_id, committed_version)

    async def read_events(
        self, task_id: str, after_sequence: int = 0
    ) -> tuple[RuntimeEvent, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        connection = self._require_connection()
        event_columns = {
            str(row["name"]) for row in connection.execute(
                "PRAGMA table_info(runtime_events)"
            ).fetchall()
        }
        session_column = (
            "session_id" if "session_id" in event_columns
            else "NULL AS session_id"
        )
        rows = connection.execute(
            """
            SELECT event_id, task_id, sequence, event_type, payload_json,
                   occurred_at, schema_version, """ + session_column + """
            FROM runtime_events
            WHERE task_id = ? AND sequence > ?
            ORDER BY sequence ASC
            """,
            (task_id, after_sequence),
        ).fetchall()
        return tuple(
            RuntimeEvent(
                event_id=str(row["event_id"]),
                task_id=str(row["task_id"]),
                sequence=int(row["sequence"]),
                event_type=str(row["event_type"]),
                payload=self._decode_mapping(str(row["payload_json"]), "event payload"),
                occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
                schema_version=int(row["schema_version"]),
                session_id=(str(row["session_id"]) if row["session_id"] else None),
            )
            for row in rows
        )

    async def load_task(self, task_id: str) -> StoredTask | None:
        row = self._require_connection().execute(
            """
            SELECT task_id, version, last_event_sequence, data_json
            FROM runtime_tasks WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredTask(
            task_id=str(row["task_id"]),
            version=int(row["version"]),
            last_event_sequence=int(row["last_event_sequence"]),
            data=self._decode_mapping(str(row["data_json"]), "task state"),
        )

    async def commit_session(self, unit: SessionUnitOfWork) -> CommitResult:
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = self._commit_session_in_transaction(connection, unit)
            connection.execute("COMMIT")
            return result
        except sqlite3.IntegrityError as error:
            connection.execute("ROLLBACK")
            raise ValueError(f"SQLite session integrity violation: {error}") from error
        except Exception:
            connection.execute("ROLLBACK")
            raise

    async def commit_session_and_task(
        self, unit: SessionTaskUnitOfWork
    ) -> tuple[CommitResult, CommitResult]:
        if not unit.command_id.strip():
            raise ValueError("session command_id must not be empty")
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            prior = connection.execute(
                "SELECT session_id, task_id FROM session_task_commands WHERE command_id = ?",
                (unit.command_id,),
            ).fetchone()
            if prior is not None:
                if (
                    str(prior["session_id"]) != unit.session.session_id
                    or str(prior["task_id"]) != unit.task.task_id
                ):
                    raise ValueError("session command_id was reused with different targets")
                session_row = connection.execute(
                    "SELECT version FROM runtime_sessions WHERE session_id = ?",
                    (unit.session.session_id,),
                ).fetchone()
                task_row = connection.execute(
                    "SELECT version FROM runtime_tasks WHERE task_id = ?",
                    (unit.task.task_id,),
                ).fetchone()
                if session_row is None or task_row is None:
                    raise RuntimeError("idempotent session command points to missing data")
                connection.execute("COMMIT")
                return (
                    CommitResult(unit.session.session_id, int(session_row["version"])),
                    CommitResult(unit.task.task_id, int(task_row["version"])),
                )
            session_result = self._commit_session_in_transaction(connection, unit.session)
            task_result = self._commit_task_in_transaction(connection, unit.task)
            connection.execute(
                "INSERT INTO session_task_commands(command_id, session_id, task_id, created_at) VALUES (?, ?, ?, ?)",
                (unit.command_id, unit.session.session_id, unit.task.task_id,
                 datetime.now().astimezone().isoformat()),
            )
            connection.execute("COMMIT")
            return session_result, task_result
        except sqlite3.IntegrityError as error:
            connection.execute("ROLLBACK")
            raise ValueError(f"SQLite session/task integrity violation: {error}") from error
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def _commit_session_in_transaction(
        self, connection: sqlite3.Connection, unit: SessionUnitOfWork,
    ) -> CommitResult:
        state_json = self._encode_mapping(unit.next_state, "session state")
        encoded_events = tuple(
            (event, self._encode_mapping(event.payload, "session event payload"))
            for event in unit.events
        )
        row = connection.execute(
            "SELECT version, last_event_sequence FROM runtime_sessions WHERE session_id = ?",
            (unit.session_id,),
        ).fetchone()
        current_version = int(row["version"]) if row is not None else 0
        last_sequence = int(row["last_event_sequence"]) if row is not None else 0
        if current_version != unit.expected_version:
            raise ValueError(
                f"session version conflict for {unit.session_id}: expected "
                f"{unit.expected_version}, got {current_version}"
            )
        expected = last_sequence + 1
        for event, _encoded in encoded_events:
            if event.session_id != unit.session_id or event.sequence != expected:
                raise ValueError("invalid Session Event identity or sequence")
            expected += 1
        committed_version = current_version + 1
        committed_sequence = expected - 1
        connection.execute(
            """
            INSERT INTO runtime_sessions(
                session_id, version, last_event_sequence, subject, state, data_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                version=excluded.version, last_event_sequence=excluded.last_event_sequence,
                subject=excluded.subject, state=excluded.state, data_json=excluded.data_json,
                updated_at=excluded.updated_at
            """,
            (unit.session_id, committed_version, committed_sequence,
             str(unit.next_state["subject"]), str(unit.next_state["state"]),
             state_json, datetime.now().astimezone().isoformat()),
        )
        for event, payload_json in encoded_events:
            connection.execute(
                """
                INSERT INTO session_events(
                    event_id, session_id, sequence, event_type, payload_json, occurred_at, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (event.event_id, event.session_id, event.sequence, event.event_type,
                 payload_json, event.occurred_at.isoformat(), event.schema_version),
            )
        return CommitResult(unit.session_id, committed_version)

    def _commit_task_in_transaction(
        self, connection: sqlite3.Connection, unit: RuntimeUnitOfWork,
    ) -> CommitResult:
        state_json = self._encode_mapping(unit.next_state, "task state")
        encoded_events = tuple(
            (event, self._encode_mapping(event.payload, "event payload"))
            for event in unit.events
        )
        row = connection.execute(
            "SELECT version, last_event_sequence FROM runtime_tasks WHERE task_id = ?",
            (unit.task_id,),
        ).fetchone()
        current_version = int(row["version"]) if row is not None else 0
        last_sequence = int(row["last_event_sequence"]) if row is not None else 0
        if current_version != unit.expected_version:
            raise ValueError("version conflict")
        expected = last_sequence + 1
        for event, _encoded in encoded_events:
            if event.task_id != unit.task_id or event.sequence != expected:
                raise ValueError("invalid Task Event identity or sequence")
            expected += 1
        committed_version = current_version + 1
        committed_sequence = expected - 1
        connection.execute(
            """
            INSERT INTO runtime_tasks(task_id, version, last_event_sequence, data_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET version=excluded.version,
                last_event_sequence=excluded.last_event_sequence, data_json=excluded.data_json,
                updated_at=excluded.updated_at
            """,
            (unit.task_id, committed_version, committed_sequence, state_json,
             datetime.now().astimezone().isoformat()),
        )
        for event, payload_json in encoded_events:
            connection.execute(
                "INSERT INTO runtime_events(event_id, task_id, sequence, event_type, payload_json, occurred_at, schema_version, session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event.event_id, event.task_id, event.sequence, event.event_type,
                 payload_json, event.occurred_at.isoformat(), event.schema_version,
                 event.session_id or str(unit.next_state.get("session_id") or "") or None),
            )
        return CommitResult(unit.task_id, committed_version)

    async def load_session(self, session_id: str) -> StoredSession | None:
        row = self._require_connection().execute(
            "SELECT session_id, version, last_event_sequence, data_json FROM runtime_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredSession(
            str(row["session_id"]), int(row["version"]),
            int(row["last_event_sequence"]),
            self._decode_mapping(str(row["data_json"]), "session state"),
        )

    async def load_session_task_command(
        self, command_id: str
    ) -> tuple[str, str] | None:
        row = self._require_connection().execute(
            "SELECT session_id, task_id FROM session_task_commands WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row["session_id"]), str(row["task_id"])

    async def claim_runtime_command(
        self, command: RuntimeCommandRecord
    ) -> RuntimeCommandRecord:
        connection = self._require_connection()
        encoded_result = self._encode_mapping(command.result, "Runtime command result")
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM runtime_commands WHERE command_id = ?",
                (command.command_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO runtime_commands(
                        command_id, command_type, request_hash, status,
                        result_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command.command_id, command.command_type,
                        command.request_hash, command.status, encoded_result,
                        command.created_at.isoformat(), command.updated_at.isoformat(),
                    ),
                )
                selected = command
            else:
                selected = self._decode_runtime_command(row)
                if (
                    selected.command_type != command.command_type
                    or selected.request_hash != command.request_hash
                ):
                    raise ValueError(
                        "command_id was reused with a different Runtime command"
                    )
            connection.execute("COMMIT")
            return selected
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    async def complete_runtime_command(
        self, command_id: str, request_hash: str, result: Mapping[str, Any]
    ) -> RuntimeCommandRecord:
        connection = self._require_connection()
        encoded = self._encode_mapping(result, "Runtime command result")
        updated_at = datetime.now().astimezone()
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM runtime_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if row is None or str(row["request_hash"]) != request_hash:
                raise ValueError("Runtime command is missing or does not match")
            connection.execute(
                "UPDATE runtime_commands SET status='completed', result_json=?, updated_at=? WHERE command_id=?",
                (encoded, updated_at.isoformat(), command_id),
            )
            completed_row = connection.execute(
                "SELECT * FROM runtime_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            connection.execute("COMMIT")
            assert completed_row is not None
            return self._decode_runtime_command(completed_row)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    async def list_sessions(
        self, subject: str, *, include_archived: bool = False
    ) -> tuple[StoredSession, ...]:
        rows = self._require_connection().execute(
            "SELECT session_id, version, last_event_sequence, data_json FROM runtime_sessions WHERE subject = ? AND (? OR state != 'ARCHIVED') ORDER BY updated_at DESC",
            (subject, 1 if include_archived else 0),
        ).fetchall()
        return tuple(StoredSession(
            str(row["session_id"]), int(row["version"]),
            int(row["last_event_sequence"]),
            self._decode_mapping(str(row["data_json"]), "session state"),
        ) for row in rows)

    async def read_session_events(
        self, session_id: str, after_sequence: int = 0
    ) -> tuple[SessionEvent, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        rows = self._require_connection().execute(
            "SELECT * FROM session_events WHERE session_id = ? AND sequence > ? ORDER BY sequence",
            (session_id, after_sequence),
        ).fetchall()
        return tuple(SessionEvent(
            str(row["event_id"]), str(row["session_id"]), int(row["sequence"]),
            str(row["event_type"]),
            self._decode_mapping(str(row["payload_json"]), "session event payload"),
            datetime.fromisoformat(str(row["occurred_at"])), int(row["schema_version"]),
        ) for row in rows)

    async def find_task_by_pending_approval(
        self, request_id: str
    ) -> StoredTask | None:
        if not request_id.strip():
            raise ValueError("approval request_id must not be empty")
        rows = self._require_connection().execute(
            """
            SELECT task_id, version, last_event_sequence, data_json
            FROM runtime_tasks
            ORDER BY updated_at DESC
            """
        ).fetchall()
        match: StoredTask | None = None
        for row in rows:
            data = self._decode_mapping(str(row["data_json"]), "task state")
            pending = data.get("pending_approval")
            if not isinstance(pending, Mapping) or pending.get("request_id") != request_id:
                continue
            if match is not None:
                raise RuntimeError(f"duplicate pending approval request_id: {request_id}")
            match = StoredTask(
                task_id=str(row["task_id"]),
                version=int(row["version"]),
                last_event_sequence=int(row["last_event_sequence"]),
                data=data,
            )
        return match

    async def find_task_by_pending_clarification(
        self, request_id: str
    ) -> StoredTask | None:
        if not request_id.strip():
            raise ValueError("clarification request_id must not be empty")
        rows = self._require_connection().execute(
            """
            SELECT task_id, version, last_event_sequence, data_json
            FROM runtime_tasks
            ORDER BY updated_at DESC
            """
        ).fetchall()
        match: StoredTask | None = None
        for row in rows:
            data = self._decode_mapping(str(row["data_json"]), "task state")
            pending = data.get("pending_clarification")
            if not isinstance(pending, Mapping) or pending.get("request_id") != request_id:
                continue
            if match is not None:
                raise RuntimeError(
                    f"duplicate pending clarification request_id: {request_id}"
                )
            match = StoredTask(
                task_id=str(row["task_id"]), version=int(row["version"]),
                last_event_sequence=int(row["last_event_sequence"]), data=data,
            )
        return match

    async def load_project_trust(
        self, workspace: str, subject: str
    ) -> StoredProjectTrust | None:
        row = self._require_connection().execute(
            "SELECT data_json FROM project_trust WHERE workspace = ? AND subject = ?",
            (workspace, subject),
        ).fetchone()
        if row is None:
            return None
        return StoredProjectTrust(
            workspace, subject,
            self._decode_mapping(str(row["data_json"]), "project trust"),
        )

    async def save_project_trust(self, trust: StoredProjectTrust) -> None:
        encoded = self._encode_mapping(trust.data, "project trust")
        self._require_connection().execute(
            """
            INSERT INTO project_trust(workspace, subject, data_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(workspace, subject) DO UPDATE SET
                data_json = excluded.data_json, updated_at = excluded.updated_at
            """,
            (trust.workspace, trust.subject, encoded, datetime.now().astimezone().isoformat()),
        )

    async def load_project_onboarding(
        self, workspace: str, subject: str
    ) -> StoredProjectOnboarding | None:
        row = self._require_connection().execute(
            "SELECT data_json FROM project_onboarding WHERE workspace = ? AND subject = ?",
            (workspace, subject),
        ).fetchone()
        if row is None:
            return None
        return StoredProjectOnboarding(
            workspace, subject,
            self._decode_mapping(str(row["data_json"]), "project onboarding"),
        )

    async def save_project_onboarding(
        self, onboarding: StoredProjectOnboarding
    ) -> None:
        encoded = self._encode_mapping(onboarding.data, "project onboarding")
        self._require_connection().execute(
            """
            INSERT INTO project_onboarding(workspace, subject, data_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(workspace, subject) DO UPDATE SET
                data_json = excluded.data_json, updated_at = excluded.updated_at
            """,
            (onboarding.workspace, onboarding.subject, encoded,
             datetime.now().astimezone().isoformat()),
        )

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        # executescript() manages its own transaction boundary when the
        # connection uses autocommit mode, so schema DDL is applied first.
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_tasks (
                task_id TEXT PRIMARY KEY,
                version INTEGER NOT NULL CHECK(version > 0),
                last_event_sequence INTEGER NOT NULL CHECK(last_event_sequence >= 0),
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_sessions (
                session_id TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version > 0),
                last_event_sequence INTEGER NOT NULL CHECK(last_event_sequence >= 0),
                subject TEXT NOT NULL, state TEXT NOT NULL, data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_events (
                event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK(sequence > 0), event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL, occurred_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                UNIQUE(session_id, sequence),
                FOREIGN KEY(session_id) REFERENCES runtime_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS session_task_commands (
                command_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES runtime_sessions(session_id),
                FOREIGN KEY(task_id) REFERENCES runtime_tasks(task_id)
            );
            CREATE TABLE IF NOT EXISTS runtime_commands (
                command_id TEXT PRIMARY KEY, command_type TEXT NOT NULL,
                request_hash TEXT NOT NULL, status TEXT NOT NULL,
                result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_events (
                event_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK(sequence > 0),
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                session_id TEXT,
                UNIQUE(task_id, sequence),
                FOREIGN KEY(task_id) REFERENCES runtime_tasks(task_id)
            );
            CREATE INDEX IF NOT EXISTS idx_runtime_events_task_sequence
                ON runtime_events(task_id, sequence);
            CREATE TABLE IF NOT EXISTS project_trust (
                workspace TEXT NOT NULL,
                subject TEXT NOT NULL,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(workspace, subject)
            );
            CREATE TABLE IF NOT EXISTS project_onboarding (
                workspace TEXT NOT NULL,
                subject TEXT NOT NULL,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(workspace, subject)
            );
            """
        )
        event_columns = {
            str(row["name"]) for row in connection.execute(
                "PRAGMA table_info(runtime_events)"
            ).fetchall()
        }
        if "session_id" not in event_columns:
            connection.execute(
                "ALTER TABLE runtime_events ADD COLUMN session_id TEXT"
            )
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT value FROM runtime_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO runtime_meta(key, value) VALUES ('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
            else:
                version = int(row["value"])
                if version == 1:
                    SQLiteRuntimeStore._migrate_session_task_commands_v1_to_v2(
                        connection
                    )
                    connection.execute(
                        "UPDATE runtime_meta SET value = ? WHERE key = 'schema_version'",
                        (str(_SCHEMA_VERSION),),
                    )
                elif version != _SCHEMA_VERSION:
                    raise RuntimeError(
                        f"unsupported SQLite schema version: {row['value']}"
                    )
            SQLiteRuntimeStore._migrate_standalone_sessions(connection)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _decode_runtime_command(row: sqlite3.Row) -> RuntimeCommandRecord:
        return RuntimeCommandRecord(
            str(row["command_id"]), str(row["command_type"]),
            str(row["request_hash"]), str(row["status"]),
            SQLiteRuntimeStore._decode_mapping(
                str(row["result_json"]), "Runtime command result"
            ),
            datetime.fromisoformat(str(row["created_at"])),
            datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _migrate_session_task_commands_v1_to_v2(
        connection: sqlite3.Connection,
    ) -> None:
        """Remove the obsolete one-command-per-session/task constraint."""
        connection.execute(
            """
            CREATE TABLE session_task_commands_v2 (
                command_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES runtime_sessions(session_id),
                FOREIGN KEY(task_id) REFERENCES runtime_tasks(task_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO session_task_commands_v2(
                command_id, session_id, task_id, created_at
            )
            SELECT command_id, session_id, task_id, created_at
            FROM session_task_commands
            """
        )
        connection.execute("DROP TABLE session_task_commands")
        connection.execute(
            "ALTER TABLE session_task_commands_v2 RENAME TO session_task_commands"
        )

    @staticmethod
    def _migrate_standalone_sessions(connection: sqlite3.Connection) -> None:
        """Materialize one deterministic Session for each pre-Session Task."""
        rows = connection.execute(
            "SELECT task_id, data_json, updated_at FROM runtime_tasks ORDER BY task_id"
        ).fetchall()
        for row in rows:
            data = SQLiteRuntimeStore._decode_mapping(
                str(row["data_json"]), "task state"
            )
            task_id = str(row["task_id"])
            # Low-level Store contract tests and foreign users may persist
            # opaque records. Only migrate genuine versioned Task snapshots.
            if str(data.get("task_id") or "") != task_id:
                continue
            encoded_task_id = json.dumps(
                task_id, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            standalone_id = (
                "session-standalone-"
                + hashlib.sha256(encoded_task_id).hexdigest()[:24]
            )
            session_id = str(data.get("session_id") or standalone_id)
            if session_id != standalone_id:
                continue
            existing = connection.execute(
                "SELECT 1 FROM runtime_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None:
                if not data.get("session_id"):
                    data["session_id"] = session_id
                    connection.execute(
                        "UPDATE runtime_tasks SET data_json = ? WHERE task_id = ?",
                        (SQLiteRuntimeStore._encode_mapping(data, "task state"), task_id),
                    )
                continue
            subject = str(data.get("trust_subject") or "legacy-local-subject")
            title = str(data.get("goal") or "Legacy task").strip()[:120]
            created_at = str(data.get("created_at") or row["updated_at"])
            updated_at = str(data.get("updated_at") or row["updated_at"])
            context_source = {
                "session_id": session_id, "subject": subject,
                "configuration_revision": 1, "context_revision": 1,
            }
            context_hash = hashlib.sha256(json.dumps(
                context_source, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            session_data = {
                "session_id": session_id, "subject": subject, "title": title,
                "state": "ACTIVE", "task_ids": [task_id],
                "active_task_id": task_id, "configuration_revision": 1,
                "context_revision": 1, "context_hash": context_hash,
                "created_at": created_at, "updated_at": updated_at,
                "closed_at": None, "close_reason": None,
            }
            connection.execute(
                "INSERT INTO runtime_sessions(session_id, version, last_event_sequence, "
                "subject, state, data_json, updated_at) VALUES (?, 1, 2, ?, 'ACTIVE', ?, ?)",
                (session_id, subject, SQLiteRuntimeStore._encode_mapping(
                    session_data, "session state"
                ), updated_at),
            )
            for sequence, event_type in ((1, "session.created"), (2, "session.task_attached")):
                payload = (
                    {"standalone": True, "migrated": True}
                    if sequence == 1 else
                    {"task_id": task_id, "active_task_id": task_id, "migrated": True}
                )
                event_id = "sevt-migrated-" + hashlib.sha256(
                    f"{session_id}:{sequence}".encode("utf-8")
                ).hexdigest()[:24]
                connection.execute(
                    "INSERT INTO session_events(event_id, session_id, sequence, event_type, "
                    "payload_json, occurred_at, schema_version) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (event_id, session_id, sequence, event_type,
                     SQLiteRuntimeStore._encode_mapping(payload, "session event payload"),
                     updated_at),
                )
            data["session_id"] = session_id
            connection.execute(
                "UPDATE runtime_tasks SET data_json = ? WHERE task_id = ?",
                (SQLiteRuntimeStore._encode_mapping(data, "task state"), task_id),
            )
            connection.execute(
                "UPDATE runtime_events SET session_id = ? "
                "WHERE task_id = ? AND session_id IS NULL",
                (session_id, task_id),
            )

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SQLite runtime store is not started")
        return self._connection

    @staticmethod
    def _encode_mapping(value: Mapping[str, Any], label: str) -> str:
        try:
            return json.dumps(
                dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label} must be JSON serializable") from error

    @staticmethod
    def _decode_mapping(raw: str, label: str) -> Mapping[str, Any]:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"stored {label} is invalid JSON") from error
        if not isinstance(value, dict):
            raise RuntimeError(f"stored {label} must be a JSON object")
        return value


class SQLiteRuntimeReader(SQLiteRuntimeStore):
    """Read an existing runtime database without creating or mutating it."""

    descriptor = AdapterDescriptor(
        adapter_id="builtin.sqlite-runtime-reader",
        adapter_version="0.1.0",
        port_name="RuntimeStorePort",
        port_version="1.0",
        capabilities=frozenset({"event-cursor", "persistent", "read-only"}),
    )

    async def start(self, context: AdapterContext) -> None:
        if self._connection is not None:
            return
        if not self._database_path.is_file():
            raise FileNotFoundError(
                f"runtime database not found: {self._database_path}"
            )
        connection = sqlite3.connect(
            f"{self._database_path.as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            row = connection.execute(
                "SELECT value FROM runtime_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None or int(row["value"]) != _SCHEMA_VERSION:
                version = None if row is None else row["value"]
                raise RuntimeError(
                    f"unsupported SQLite schema version: {version}"
                )
        except Exception:
            connection.close()
            raise
        self._connection = connection

    async def commit(self, unit: RuntimeUnitOfWork) -> CommitResult:
        raise RuntimeError("SQLite runtime reader does not allow writes")

    async def commit_session(self, unit: SessionUnitOfWork) -> CommitResult:
        raise RuntimeError("SQLite runtime reader does not allow writes")

    async def commit_session_and_task(
        self, unit: SessionTaskUnitOfWork
    ) -> tuple[CommitResult, CommitResult]:
        raise RuntimeError("SQLite runtime reader does not allow writes")

    async def claim_runtime_command(
        self, command: RuntimeCommandRecord
    ) -> RuntimeCommandRecord:
        raise RuntimeError("SQLite runtime reader does not allow writes")

    async def complete_runtime_command(
        self, command_id: str, request_hash: str, result: Mapping[str, Any]
    ) -> RuntimeCommandRecord:
        raise RuntimeError("SQLite runtime reader does not allow writes")

    async def save_project_trust(self, trust: StoredProjectTrust) -> None:
        raise RuntimeError("SQLite runtime reader does not allow writes")
