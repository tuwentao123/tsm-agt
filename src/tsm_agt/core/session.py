"""Session aggregate: one durable conversation containing isolated Tasks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from .configuration import canonical_hash


class SessionState(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"


class SessionInteractionKind(StrEnum):
    CHOICE = "CHOICE"


@dataclass(frozen=True, slots=True)
class SessionChoiceOption:
    """One stable option exactly as it was shown to the user."""

    option_id: str
    ordinal: int
    label: str
    target_type: str
    target_id: str
    metadata: Mapping[str, Any]

    def to_data(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id, "ordinal": self.ordinal,
            "label": self.label, "target_type": self.target_type,
            "target_id": self.target_id, "metadata": dict(self.metadata),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> SessionChoiceOption:
        return cls(
            str(data["option_id"]), int(data["ordinal"]),
            str(data["label"]), str(data["target_type"]),
            str(data["target_id"]), dict(data.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class SessionInteractionRequest:
    """Durable UI protocol state; it carries no execution authority."""

    interaction_id: str
    kind: SessionInteractionKind
    prompt: str
    options: tuple[SessionChoiceOption, ...]
    created_at: datetime
    source: str

    def to_data(self) -> dict[str, Any]:
        return {
            "interaction_id": self.interaction_id, "kind": self.kind.value,
            "prompt": self.prompt,
            "options": [item.to_data() for item in self.options],
            "created_at": self.created_at.isoformat(), "source": self.source,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> SessionInteractionRequest:
        return cls(
            str(data["interaction_id"]),
            SessionInteractionKind(str(data["kind"])), str(data["prompt"]),
            tuple(SessionChoiceOption.from_data(item) for item in data["options"]),
            datetime.fromisoformat(str(data["created_at"])),
            str(data.get("source", "runtime")),
        )


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    session_id: str
    subject: str
    title: str
    state: SessionState
    task_ids: tuple[str, ...]
    active_task_id: str | None
    configuration_revision: int
    context_revision: int
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    close_reason: str | None = None
    pending_interaction: SessionInteractionRequest | None = None

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.subject.strip() or not self.title.strip():
            raise ValueError("session id, subject, and title are required")
        if len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("session task_ids must be unique")
        if self.active_task_id is not None and self.active_task_id not in self.task_ids:
            raise ValueError("active Task must belong to the Session")
        if self.configuration_revision < 1 or self.context_revision < 1:
            raise ValueError("session revisions must be positive")
        if self.state is SessionState.ACTIVE and self.closed_at is not None:
            raise ValueError("active Session cannot have closed_at")

    @classmethod
    def create(
        cls, session_id: str, subject: str, title: str,
        now: datetime | None = None,
    ) -> SessionSnapshot:
        timestamp = now or datetime.now(timezone.utc)
        return cls(
            session_id, subject, title.strip(), SessionState.ACTIVE, (), None,
            1, 1, timestamp, timestamp,
        )

    @property
    def context_hash(self) -> str:
        return canonical_hash({
            "session_id": self.session_id, "subject": self.subject,
            "configuration_revision": self.configuration_revision,
            "context_revision": self.context_revision,
        })

    def attach_task(
        self, task_id: str, *, make_active: bool = True,
        now: datetime | None = None,
    ) -> SessionSnapshot:
        if self.state is not SessionState.ACTIVE:
            raise ValueError("Tasks can only be attached to an ACTIVE Session")
        if task_id in self.task_ids:
            raise ValueError(f"Task is already attached to Session: {task_id}")
        return replace(
            self, task_ids=self.task_ids + (task_id,),
            active_task_id=task_id if make_active else self.active_task_id,
            updated_at=now or datetime.now(timezone.utc),
        )

    def select_task(
        self, task_id: str, now: datetime | None = None
    ) -> SessionSnapshot:
        if self.state is not SessionState.ACTIVE:
            raise ValueError("active Task can only change in an ACTIVE Session")
        if task_id not in self.task_ids:
            raise ValueError("Task does not belong to this Session")
        return replace(
            self, active_task_id=task_id,
            updated_at=now or datetime.now(timezone.utc),
        )

    def bump_context(self, now: datetime | None = None) -> SessionSnapshot:
        if self.state is not SessionState.ACTIVE:
            raise ValueError("closed Session context cannot change")
        return replace(
            self, context_revision=self.context_revision + 1,
            updated_at=now or datetime.now(timezone.utc),
        )

    def request_interaction(
        self, interaction: SessionInteractionRequest,
        now: datetime | None = None,
    ) -> SessionSnapshot:
        if self.state is not SessionState.ACTIVE:
            raise ValueError("only an ACTIVE Session may request interaction")
        return replace(
            self, pending_interaction=interaction,
            updated_at=now or datetime.now(timezone.utc),
        )

    def clear_interaction(self, now: datetime | None = None) -> SessionSnapshot:
        return replace(
            self, pending_interaction=None,
            updated_at=now or datetime.now(timezone.utc),
        )

    def close(self, reason: str, now: datetime | None = None) -> SessionSnapshot:
        if self.state is not SessionState.ACTIVE:
            raise ValueError("only ACTIVE Session can be closed")
        normalized = reason.strip()
        if not normalized:
            raise ValueError("session close reason is required")
        timestamp = now or datetime.now(timezone.utc)
        return replace(
            self, state=SessionState.CLOSED, active_task_id=None,
            pending_interaction=None,
            updated_at=timestamp, closed_at=timestamp, close_reason=normalized,
        )

    def archive(self, now: datetime | None = None) -> SessionSnapshot:
        if self.state is not SessionState.CLOSED:
            raise ValueError("only CLOSED Session can be archived")
        return replace(
            self, state=SessionState.ARCHIVED,
            updated_at=now or datetime.now(timezone.utc),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "session_id": self.session_id, "subject": self.subject,
            "title": self.title, "state": self.state.value,
            "task_ids": list(self.task_ids),
            "active_task_id": self.active_task_id,
            "configuration_revision": self.configuration_revision,
            "context_revision": self.context_revision,
            "context_hash": self.context_hash,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "close_reason": self.close_reason,
            "pending_interaction": (
                self.pending_interaction.to_data()
                if self.pending_interaction is not None else None
            ),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> SessionSnapshot:
        if int(data.get("schema_version", 1)) != 1:
            raise ValueError("unsupported Session snapshot schema version")
        raw_tasks = data.get("task_ids", [])
        if not isinstance(raw_tasks, list):
            raise ValueError("session task_ids must be a list")
        snapshot = cls(
            session_id=str(data["session_id"]), subject=str(data["subject"]),
            title=str(data["title"]), state=SessionState(str(data["state"])),
            task_ids=tuple(str(item) for item in raw_tasks),
            active_task_id=(
                str(data["active_task_id"])
                if data.get("active_task_id") is not None else None
            ),
            configuration_revision=int(data.get("configuration_revision", 1)),
            context_revision=int(data.get("context_revision", 1)),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
            closed_at=(
                datetime.fromisoformat(str(data["closed_at"]))
                if data.get("closed_at") is not None else None
            ),
            close_reason=(
                str(data["close_reason"])
                if data.get("close_reason") is not None else None
            ),
            pending_interaction=(
                SessionInteractionRequest.from_data(data["pending_interaction"])
                if data.get("pending_interaction") is not None else None
            ),
        )
        if data.get("context_hash") and str(data["context_hash"]) != snapshot.context_hash:
            raise ValueError("session context hash does not match")
        return snapshot


def standalone_session_id(task_id: str) -> str:
    return "session-standalone-" + canonical_hash(task_id)[:24]
