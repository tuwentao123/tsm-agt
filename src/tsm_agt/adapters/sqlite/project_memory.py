"""SQLite ProjectMemoryPort Adapter with idempotent mutations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
    MemoryOperationResult, StoredMemory,
)


class SQLiteProjectMemoryStore:
    descriptor = AdapterDescriptor(
        "builtin.sqlite-project-memory", "0.1.0", "ProjectMemoryPort",
        "1.0", frozenset({"persistent", "idempotent-operations"}),
    )

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().resolve()
        self._connection: sqlite3.Connection | None = None

    async def start(self, context: AdapterContext) -> None:
        if self._connection is not None:
            return
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._database_path, isolation_level=None, timeout=5.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS project_memories (
                memory_id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                scope TEXT NOT NULL,
                workspace TEXT,
                task_id TEXT,
                session_id TEXT,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_memory_operations (
                operation_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        columns = {
            str(row["name"]) for row in connection.execute(
                "PRAGMA table_info(project_memories)"
            ).fetchall()
        }
        if "session_id" not in columns:
            connection.execute(
                "ALTER TABLE project_memories ADD COLUMN session_id TEXT"
            )
        connection.execute("DROP INDEX IF EXISTS idx_project_memories_lookup")
        connection.execute(
            "CREATE INDEX idx_project_memories_lookup ON project_memories"
            "(subject, scope, workspace, task_id, session_id)"
        )
        self._connection = connection

    async def health(self) -> HealthStatus:
        if self._connection is None:
            return HealthStatus(HealthState.UNHEALTHY, "SQLite memory not started")
        try:
            self._connection.execute("SELECT 1").fetchone()
        except sqlite3.Error as error:
            return HealthStatus(HealthState.UNHEALTHY, f"SQLite check failed: {error}")
        return HealthStatus(HealthState.HEALTHY, "SQLite project memory ready")

    async def stop(self, deadline: datetime) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    async def load_memory(self, memory_id: str) -> StoredMemory | None:
        row = self._require().execute(
            "SELECT * FROM project_memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        return self._decode_row(row) if row is not None else None

    async def list_memories(
        self, *, subject: str, workspace: str, task_id: str, session_id: str
    ) -> tuple[StoredMemory, ...]:
        rows = self._require().execute(
            """
            SELECT * FROM project_memories
            WHERE subject = ? AND (
                scope = 'USER'
                OR (scope = 'PROJECT' AND workspace = ?)
                OR (scope = 'TASK' AND task_id = ?)
                OR (scope = 'SESSION' AND session_id IS NULL AND task_id = ?)
                OR (scope = 'SESSION' AND session_id = ?)
            )
            ORDER BY memory_id ASC
            """,
            (subject, workspace, task_id, task_id, session_id),
        ).fetchall()
        return tuple(self._decode_row(row) for row in rows)

    async def save_memory(
        self, memory: StoredMemory, operation_id: str
    ) -> MemoryOperationResult:
        encoded = self._encode(memory.data)
        input_hash = self._hash({
            "operation": "save", "memory_id": memory.memory_id,
        })
        connection = self._require()
        connection.execute("BEGIN IMMEDIATE")
        try:
            replay = self._read_operation(connection, operation_id, "save", memory.memory_id, input_hash)
            if replay is not None:
                connection.execute("COMMIT")
                return MemoryOperationResult(replay, replayed=True)
            now = datetime.now().astimezone().isoformat()
            connection.execute(
                """
                INSERT INTO project_memories(
                    memory_id, subject, scope, workspace, task_id, session_id,
                    data_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    subject=excluded.subject, scope=excluded.scope,
                    workspace=excluded.workspace, task_id=excluded.task_id,
                    session_id=excluded.session_id,
                    data_json=excluded.data_json, updated_at=excluded.updated_at
                """,
                (memory.memory_id, memory.subject, memory.scope, memory.workspace,
                 memory.task_id, memory.session_id, encoded, now),
            )
            self._write_operation(connection, operation_id, "save", memory.memory_id, input_hash, memory)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return MemoryOperationResult(memory)

    async def delete_memory(
        self, memory_id: str, subject: str, operation_id: str
    ) -> MemoryOperationResult:
        input_hash = self._hash({
            "operation": "delete", "memory_id": memory_id,
        })
        connection = self._require()
        connection.execute("BEGIN IMMEDIATE")
        try:
            replay = self._read_operation(
                connection, operation_id, "delete", memory_id, input_hash,
                allow_null=True,
            )
            if replay is not False:
                connection.execute("COMMIT")
                return MemoryOperationResult(
                    replay if isinstance(replay, StoredMemory) else None, replayed=True
                )
            row = connection.execute(
                "SELECT * FROM project_memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            memory = self._decode_row(row) if row is not None else None
            if memory is not None and memory.subject != subject:
                raise PermissionError("memory belongs to another local subject")
            if memory is not None:
                connection.execute(
                    "DELETE FROM project_memories WHERE memory_id = ?", (memory_id,)
                )
            self._write_operation(connection, operation_id, "delete", memory_id, input_hash, memory)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return MemoryOperationResult(memory)

    def _read_operation(
        self, connection: sqlite3.Connection, operation_id: str, operation: str,
        memory_id: str, input_hash: str, *, allow_null: bool = False,
    ) -> StoredMemory | bool | None:
        row = connection.execute(
            "SELECT * FROM project_memory_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return False if allow_null else None
        if (
            row["operation"] != operation or row["memory_id"] != memory_id
            or row["input_hash"] != input_hash
        ):
            raise ValueError("memory operation_id was reused with different input")
        if row["result_json"] is None:
            return None
        data = self._decode(str(row["result_json"]))
        return StoredMemory(
            str(data["memory_id"]), str(data["subject"]), str(data["scope"]),
            str(data["workspace"]) if data.get("workspace") else None,
            str(data["task_id"]) if data.get("task_id") else None,
            data["data"],
            str(data["session_id"]) if data.get("session_id") else None,
        )

    def _write_operation(
        self, connection: sqlite3.Connection, operation_id: str, operation: str,
        memory_id: str, input_hash: str, result: StoredMemory | None,
    ) -> None:
        encoded = None if result is None else self._encode({
            "memory_id": result.memory_id, "subject": result.subject,
            "scope": result.scope, "workspace": result.workspace,
            "task_id": result.task_id, "session_id": result.session_id,
            "data": result.data,
        })
        connection.execute(
            """
            INSERT INTO project_memory_operations(
                operation_id, operation, memory_id, input_hash, result_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (operation_id, operation, memory_id, input_hash, encoded,
             datetime.now().astimezone().isoformat()),
        )

    @classmethod
    def _decode_row(cls, row: sqlite3.Row) -> StoredMemory:
        return StoredMemory(
            str(row["memory_id"]), str(row["subject"]), str(row["scope"]),
            str(row["workspace"]) if row["workspace"] is not None else None,
            str(row["task_id"]) if row["task_id"] is not None else None,
            cls._decode(str(row["data_json"])),
            str(row["session_id"]) if row["session_id"] is not None else None,
        )

    def _require(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SQLite project memory is not started")
        return self._connection

    @staticmethod
    def _encode(value: Mapping[str, Any]) -> str:
        return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode(raw: str) -> Mapping[str, Any]:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RuntimeError("stored memory must be a JSON object")
        return value

    @classmethod
    def _hash(cls, value: Mapping[str, Any]) -> str:
        return hashlib.sha256(cls._encode(value).encode("utf-8")).hexdigest()
