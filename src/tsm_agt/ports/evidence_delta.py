"""Provider-neutral evidence-delta contracts for model-requested tool calls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .adapter import RuntimeAdapter
from .tool import ToolCall, ToolResult


EVIDENCE_CATEGORIES = (
    "new_paths",
    "new_symbols",
    "new_relations",
    "new_facts",
    "new_exclusions",
    "resolved_questions",
    "new_verification",
)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    category: str
    fingerprint: str
    summary: str

    def __post_init__(self) -> None:
        if self.category not in EVIDENCE_CATEGORIES:
            raise ValueError(f"unsupported evidence category: {self.category}")
        if not self.fingerprint.strip():
            raise ValueError("evidence fingerprint must not be empty")
        if not self.summary.strip() or len(self.summary) > 500:
            raise ValueError("evidence summary must contain 1..500 characters")

    def to_data(self) -> dict[str, str]:
        return {
            "category": self.category,
            "fingerprint": self.fingerprint,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class EvidenceInventory:
    fingerprints: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    consecutive_zero_delta: int = 0

    def __post_init__(self) -> None:
        if self.consecutive_zero_delta < 0:
            raise ValueError("consecutive_zero_delta must not be negative")
        unknown = set(self.fingerprints) - set(EVIDENCE_CATEGORIES)
        if unknown:
            raise ValueError(
                "unsupported evidence inventory categories: "
                + ", ".join(sorted(unknown))
            )

    @classmethod
    def from_data(cls, data: Mapping[str, Any] | None) -> EvidenceInventory:
        if not data:
            return cls()
        raw = data.get("fingerprints", {})
        if not isinstance(raw, Mapping):
            raise ValueError("evidence inventory fingerprints must be an object")
        fingerprints: dict[str, tuple[str, ...]] = {}
        for category, values in raw.items():
            if not isinstance(values, (list, tuple)) or not all(
                isinstance(value, str) and value for value in values
            ):
                raise ValueError(
                    f"evidence inventory {category} must be an array of strings"
                )
            fingerprints[str(category)] = tuple(sorted(set(values)))
        return cls(
            fingerprints=fingerprints,
            consecutive_zero_delta=int(data.get("consecutive_zero_delta", 0)),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "fingerprints": {
                category: list(values)
                for category, values in sorted(self.fingerprints.items())
            },
            "consecutive_zero_delta": self.consecutive_zero_delta,
        }


@dataclass(frozen=True, slots=True)
class EvidenceDelta:
    question_id: str
    items: tuple[EvidenceItem, ...]
    result_status: str
    consecutive_zero_delta: int

    @property
    def total_new(self) -> int:
        return len(self.items)

    @property
    def has_progress(self) -> bool:
        return bool(self.items)

    @property
    def counts(self) -> dict[str, int]:
        return {
            category: sum(item.category == category for item in self.items)
            for category in EVIDENCE_CATEGORIES
        }

    def to_data(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "result_status": self.result_status,
            "has_progress": self.has_progress,
            "total_new": self.total_new,
            "counts": self.counts,
            "items": [item.to_data() for item in self.items],
            "consecutive_zero_delta": self.consecutive_zero_delta,
        }


@dataclass(frozen=True, slots=True)
class EvidenceEvaluation:
    delta: EvidenceDelta
    inventory: EvidenceInventory


class EvidenceDeltaEvaluatorPort(RuntimeAdapter, Protocol):
    async def evaluate(
        self, call: ToolCall, result: ToolResult, inventory: EvidenceInventory
    ) -> EvidenceEvaluation: ...
