"""Provider-neutral contracts for consuming bounded search hits before re-searching."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .adapter import RuntimeAdapter
from .semantic_action import SemanticAction
from .tool import ToolCall, ToolResult


class ReadHitsAction(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_READ = "REQUIRE_READ"


@dataclass(frozen=True, slots=True)
class ReadHitsState:
    question_hash: str = ""
    candidate_path_hashes: tuple[str, ...] = ()
    source_tool: str = ""
    source_family: str = ""

    @property
    def active(self) -> bool:
        return bool(self.question_hash and self.candidate_path_hashes)

    @classmethod
    def from_data(cls, data: Mapping[str, Any] | None) -> ReadHitsState:
        if not data:
            return cls()
        raw_hashes = data.get("candidate_path_hashes", ())
        if not isinstance(raw_hashes, (list, tuple)) or not all(
            isinstance(item, str) and item for item in raw_hashes
        ):
            raise ValueError(
                "read-hits candidate_path_hashes must be an array of strings"
            )
        return cls(
            question_hash=str(data.get("question_hash", "")),
            candidate_path_hashes=tuple(sorted(set(raw_hashes))),
            source_tool=str(data.get("source_tool", "")),
            source_family=str(data.get("source_family", "")),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "question_hash": self.question_hash,
            "candidate_path_hashes": list(self.candidate_path_hashes),
            "candidate_count": len(self.candidate_path_hashes),
            "source_tool": self.source_tool,
            "source_family": self.source_family,
        }


@dataclass(frozen=True, slots=True)
class ReadHitsDecision:
    action: ReadHitsAction
    reason: str
    candidate_count: int = 0
    question_hash: str = ""


@dataclass(frozen=True, slots=True)
class ReadHitsUpdate:
    state: ReadHitsState
    transition: str
    candidate_count: int = 0


class ReadHitsPolicyPort(RuntimeAdapter, Protocol):
    async def before_call(
        self, call: ToolCall, semantic_action: SemanticAction | None,
        state: ReadHitsState,
    ) -> ReadHitsDecision: ...

    async def after_result(
        self, call: ToolCall, result: ToolResult,
        semantic_action: SemanticAction | None, state: ReadHitsState,
    ) -> ReadHitsUpdate: ...
