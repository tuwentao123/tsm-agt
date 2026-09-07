"""Durable user clarification owned by the microkernel."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tsm_agt.ports import ToolCall


@dataclass(frozen=True, slots=True)
class ClarificationChoice:
    value: str
    label: str

    def __post_init__(self) -> None:
        if not self.value.strip() or not self.label.strip():
            raise ValueError("clarification choice value and label must not be empty")

    def to_data(self) -> dict[str, str]:
        return {"value": self.value, "label": self.label}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ClarificationChoice:
        return cls(str(data["value"]), str(data["label"]))


@dataclass(frozen=True, slots=True)
class ClarificationRequest:
    request_id: str
    task_id: str
    turn_id: str
    call: ToolCall
    question: str
    choices: tuple[ClarificationChoice, ...]
    reason: str
    required: bool
    resume_token_hash: str
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.question.strip():
            raise ValueError("clarification request_id and question must not be empty")
        if not self.reason.strip():
            raise ValueError("clarification reason must not be empty")
        if len(self.choices) > 3:
            raise ValueError("clarification supports at most 3 choices")
        if self.expires_at <= self.created_at:
            raise ValueError("clarification expiry must follow creation")

    @staticmethod
    def hash_resume_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def accepts_token(
        self, token: str, now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(timezone.utc)
        return current < self.expires_at and hmac.compare_digest(
            self.resume_token_hash, self.hash_resume_token(token),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "call": self.call.to_data(),
            "question": self.question,
            "choices": [choice.to_data() for choice in self.choices],
            "reason": self.reason,
            "required": self.required,
            "resume_token_hash": self.resume_token_hash,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ClarificationRequest:
        raw_call = data.get("call")
        raw_choices = data.get("choices", ())
        if not isinstance(raw_call, Mapping) or not isinstance(raw_choices, list):
            raise ValueError("stored clarification call/choices are malformed")
        return cls(
            request_id=str(data["request_id"]),
            task_id=str(data["task_id"]),
            turn_id=str(data["turn_id"]),
            call=ToolCall.from_data(raw_call),
            question=str(data["question"]),
            choices=tuple(
                ClarificationChoice.from_data(item)
                for item in raw_choices if isinstance(item, Mapping)
            ),
            reason=str(data["reason"]),
            required=bool(data.get("required", True)),
            resume_token_hash=str(data["resume_token_hash"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            expires_at=datetime.fromisoformat(str(data["expires_at"])),
        )


class ClarificationNotPending(LookupError):
    pass


class ClarificationTokenMismatch(PermissionError):
    pass


class ClarificationRequired(RuntimeError):
    def __init__(
        self, request: ClarificationRequest, resume_token: str,
    ) -> None:
        super().__init__(f"clarification {request.request_id} requires user input")
        self.request = request
        self.resume_token = resume_token
