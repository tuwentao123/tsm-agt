from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .tool import ToolEffect


@dataclass(frozen=True, slots=True)
class CompletionGap:
    """One durable gap attached to an explicit Outcome completion claim."""

    gap_id: str
    kind: str
    description: str
    status: str
    required: bool = True
    recoverable: bool = False
    evidence_reference: str = ""
    expected_scope: str = ""
    required_effects: tuple[ToolEffect, ...] = ()
    candidate_tools: tuple[str, ...] = ()
    observed: str = ""

    def __post_init__(self) -> None:
        if not all((self.gap_id.strip(), self.kind.strip(), self.description.strip())):
            raise ValueError("completion gap identity, kind, and description are required")
        if ToolEffect.UNSPECIFIED in self.required_effects:
            raise ValueError("completion gap cannot require an unspecified tool effect")
        if any(not name.strip() for name in self.candidate_tools):
            raise ValueError("completion gap candidate tool names must not be empty")

    @property
    def effective_required_effects(self) -> frozenset[ToolEffect]:
        if self.required_effects:
            return frozenset(self.required_effects)
        if self.recoverable:
            return frozenset({ToolEffect.OBSERVE})
        return frozenset()

    def to_data(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "kind": self.kind,
            "description": self.description,
            "status": self.status,
            "required": self.required,
            "recoverable": self.recoverable,
            "evidence_reference": self.evidence_reference,
            "expected_scope": self.expected_scope,
            "required_effects": [item.value for item in self.required_effects],
            "candidate_tools": list(self.candidate_tools),
            "observed": self.observed,
        }
