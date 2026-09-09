"""Replaceable contract for checking whether an Agent may finish a Task."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .adapter import RuntimeAdapter


class CompletionReadinessAction(StrEnum):
    COMPLETE = "COMPLETE"
    CONTINUE = "CONTINUE"
    REPORT_BLOCKED = "REPORT_BLOCKED"


@dataclass(frozen=True, slots=True)
class CompletionGap:
    """One inspectable reason why the proposed final answer may be early."""

    gap_id: str
    kind: str
    description: str
    status: str
    required: bool = True
    recoverable: bool = False
    evidence_reference: str = ""
    expected_scope: str = ""

    def __post_init__(self) -> None:
        if not all((self.gap_id.strip(), self.kind.strip(), self.description.strip())):
            raise ValueError("completion gap identity, kind, and description are required")

    def to_data(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id, "kind": self.kind,
            "description": self.description, "status": self.status,
            "required": self.required,
            "recoverable": self.recoverable,
            "evidence_reference": self.evidence_reference,
            "expected_scope": self.expected_scope,
        }


@dataclass(frozen=True, slots=True)
class CompletionReadinessState:
    """Checkpointed bounded correction counts for one Agent Turn."""

    continue_attempts: int = 0
    disclosure_attempts: int = 0
    last_action: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.continue_attempts < 0 or self.disclosure_attempts < 0:
            raise ValueError("completion readiness counters must not be negative")
        if self.schema_version != 1:
            raise ValueError("unsupported completion readiness schema version")

    @classmethod
    def from_data(
        cls, data: Mapping[str, Any] | None,
    ) -> CompletionReadinessState:
        if not data:
            return cls()
        return cls(
            int(data.get("continue_attempts", 0)),
            int(data.get("disclosure_attempts", 0)),
            str(data.get("last_action", "")),
            int(data.get("schema_version", 1)),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "continue_attempts": self.continue_attempts,
            "disclosure_attempts": self.disclosure_attempts,
            "last_action": self.last_action,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class CompletionReadinessProbe:
    goal: str
    gaps: tuple[CompletionGap, ...]
    remaining_model_calls: int
    remaining_tool_calls: int
    available_read_tools: tuple[str, ...]
    forced_wrap_up: bool = False
    evidence_item_count: int = 0
    successful_tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class CompletionReadinessDecision:
    action: CompletionReadinessAction
    reason: str
    state: CompletionReadinessState
    gaps: tuple[CompletionGap, ...] = ()


class CompletionReadinessPolicyPort(RuntimeAdapter, Protocol):
    """Judge a proposed final answer from persisted, inspectable facts only."""

    async def evaluate(
        self, probe: CompletionReadinessProbe, state: CompletionReadinessState,
    ) -> CompletionReadinessDecision: ...
