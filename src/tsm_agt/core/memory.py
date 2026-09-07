"""Validated, source-bearing memory records safe for later recall."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from tsm_agt.ports import StoredMemory


class MemoryScope(StrEnum):
    TASK = "TASK"
    SESSION = "SESSION"
    PROJECT = "PROJECT"
    USER = "USER"


class MemorySourceKind(StrEnum):
    USER_CONFIRMED = "user_confirmed"
    WORKSPACE_FILE = "workspace_file"
    TOOL_OBSERVATION = "tool_observation"


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(
        r"\b(?:api[_-]?key|password|passwd|secret|access[_-]?token)"
        r"\s*[:=]\s*[^\s,;]{6,}",
        re.I,
    ),
)


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    scope: MemoryScope
    content: str
    source_kind: MemorySourceKind
    source_reference: str
    source_hash: str | None
    created_at: datetime
    last_verified_at: datetime
    workspace: str | None
    workspace_fingerprint: str | None
    subject: str
    task_id: str | None
    writer: str
    revision: int = 1
    session_id: str | None = None

    def __post_init__(self) -> None:
        values = (
            self.memory_id, self.content, self.source_reference, self.subject,
            self.writer,
        )
        if any(not value.strip() for value in values):
            raise ValueError("memory identity, content, source, subject, and writer are required")
        if len(self.content) > 2000:
            raise ValueError("memory content cannot exceed 2000 characters")
        if len(self.source_reference) > 1000:
            raise ValueError("memory source reference cannot exceed 1000 characters")
        if any(pattern.search(self.content) for pattern in _SECRET_PATTERNS):
            raise ValueError("credentials and private key material cannot be stored in memory")
        if self.revision < 1:
            raise ValueError("memory revision must be positive")
        if self.scope is MemoryScope.TASK and not self.task_id:
            raise ValueError("TASK memory requires task_id")
        if self.scope is MemoryScope.SESSION and not self.session_id:
            raise ValueError("SESSION memory requires session_id")
        if self.scope is MemoryScope.PROJECT and (
            not self.workspace or not self.workspace_fingerprint
        ):
            raise ValueError("PROJECT memory requires workspace and fingerprint")
        if (
            self.scope is MemoryScope.USER
            and self.source_kind is not MemorySourceKind.USER_CONFIRMED
        ):
            raise ValueError("USER memory requires an explicitly confirmed user source")

    @property
    def stale_for(self) -> str | None:
        return self.workspace_fingerprint

    def verify(
        self, *, workspace_fingerprint: str | None, source_hash: str | None,
        writer: str,
    ) -> MemoryRecord:
        return replace(
            self,
            workspace_fingerprint=(
                workspace_fingerprint
                if self.scope is MemoryScope.PROJECT
                else self.workspace_fingerprint
            ),
            source_hash=source_hash if source_hash is not None else self.source_hash,
            last_verified_at=datetime.now(timezone.utc), writer=writer,
            revision=self.revision + 1,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id, "scope": self.scope.value,
            "content": self.content, "source_kind": self.source_kind.value,
            "source_reference": self.source_reference,
            "source_hash": self.source_hash,
            "created_at": self.created_at.isoformat(),
            "last_verified_at": self.last_verified_at.isoformat(),
            "workspace": self.workspace,
            "workspace_fingerprint": self.workspace_fingerprint,
            "subject": self.subject, "task_id": self.task_id,
            "writer": self.writer, "revision": self.revision,
            "session_id": self.session_id,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> MemoryRecord:
        raw_scope = str(data["scope"])
        # Before Session Runtime existed, the value SESSION meant what is now
        # TASK scope.  Only that old shape (task_id without session_id) is
        # translated; new Session memories retain their real scope.
        if (
            raw_scope == "SESSION"
            and data.get("task_id")
            and not data.get("session_id")
        ):
            raw_scope = "TASK"
        return cls(
            memory_id=str(data["memory_id"]),
            scope=MemoryScope(raw_scope), content=str(data["content"]),
            source_kind=MemorySourceKind(str(data["source_kind"])),
            source_reference=str(data["source_reference"]),
            source_hash=(str(data["source_hash"]) if data.get("source_hash") else None),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            last_verified_at=datetime.fromisoformat(str(data["last_verified_at"])),
            workspace=(str(data["workspace"]) if data.get("workspace") else None),
            workspace_fingerprint=(
                str(data["workspace_fingerprint"])
                if data.get("workspace_fingerprint") else None
            ),
            subject=str(data["subject"]),
            task_id=(str(data["task_id"]) if data.get("task_id") else None),
            writer=str(data["writer"]), revision=int(data.get("revision", 1)),
            session_id=(
                str(data["session_id"]) if data.get("session_id") else None
            ),
        )

    def to_stored(self) -> StoredMemory:
        return StoredMemory(
            self.memory_id, self.subject, self.scope.value, self.workspace,
            self.task_id, self.to_data(), self.session_id,
        )

    @classmethod
    def from_stored(cls, stored: StoredMemory) -> MemoryRecord:
        record = cls.from_data(stored.data)
        legacy_session_index = (
            stored.scope == "SESSION" and stored.task_id is not None
            and stored.session_id is None and record.scope is MemoryScope.TASK
        )
        if (
            record.memory_id != stored.memory_id
            or record.subject != stored.subject
            or (
                record.scope.value != stored.scope
                and not legacy_session_index
            )
            or record.workspace != stored.workspace
            or record.task_id != stored.task_id
            or (record.session_id != stored.session_id and not legacy_session_index)
        ):
            raise ValueError("stored memory index does not match its record body")
        return record


@dataclass(frozen=True, slots=True)
class MemoryView:
    record: MemoryRecord
    stale: bool
    stale_reason: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            **self.record.to_data(), "stale": self.stale,
            "stale_reason": self.stale_reason,
        }
