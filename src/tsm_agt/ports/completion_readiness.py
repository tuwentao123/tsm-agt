"""Replaceable contract for checking whether an Agent may finish a Task."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .adapter import RuntimeAdapter
from .tool import ToolEffect


class CompletionReadinessMode(StrEnum):
    """Controls whether completion diagnostics can direct Agent execution."""

    LEGACY_GATE = "LEGACY_GATE"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    AGENT_DECIDES = "AGENT_DECIDES"


class CompletionReadinessAction(StrEnum):
    COMPLETE = "COMPLETE"
    CONTINUE = "CONTINUE"
    REPORT_INCOMPLETE_RECOVERABLE = "REPORT_INCOMPLETE_RECOVERABLE"
    REPORT_BLOCKED = "REPORT_BLOCKED"


@dataclass(frozen=True, slots=True)
class CompletionGap:
    """One inspectable reason why the proposed final answer may be early.

    required_effects describes the capability needed to close the gap. It does
    not grant permission or select an invocation; normal tool validation, risk
    policy, approval and sandboxing still apply. recoverable remains as a
    compatibility hint where it means OBSERVE may recover the gap.
    """

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
        """Return explicit effects or the legacy read-recovery equivalent."""
        if self.required_effects:
            return frozenset(self.required_effects)
        if self.recoverable:
            return frozenset({ToolEffect.OBSERVE})
        return frozenset()

    def to_data(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id, "kind": self.kind,
            "description": self.description, "status": self.status,
            "required": self.required,
            "recoverable": self.recoverable,
            "evidence_reference": self.evidence_reference,
            "expected_scope": self.expected_scope,
            "required_effects": [item.value for item in self.required_effects],
            "candidate_tools": list(self.candidate_tools),
            "observed": self.observed,
        }


@dataclass(frozen=True, slots=True)
class CompletionReadinessState:
    """Checkpointed bounded correction counts for one Agent Turn.

    ``stalled_continuations`` counts consecutive user continuations that ended
    with exactly the same required gap set and no completed Outcome. It is the
    one counter that deliberately survives a capacity replenishment: the other
    three bound corrections inside a single attempt, while this one bounds how
    many times the same unsatisfied requirement may be retried at all.
    """

    continue_attempts: int = 0
    disclosure_attempts: int = 0
    automatic_resume_attempts: int = 0
    last_action: str = ""
    schema_version: int = 1
    stalled_continuations: int = 0

    def __post_init__(self) -> None:
        if min(
            self.continue_attempts, self.disclosure_attempts,
            self.automatic_resume_attempts, self.stalled_continuations,
        ) < 0:
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
            int(data.get("automatic_resume_attempts", 0)),
            str(data.get("last_action", "")),
            int(data.get("schema_version", 1)),
            int(data.get("stalled_continuations", 0)),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "continue_attempts": self.continue_attempts,
            "disclosure_attempts": self.disclosure_attempts,
            "automatic_resume_attempts": self.automatic_resume_attempts,
            "last_action": self.last_action,
            "schema_version": self.schema_version,
            "stalled_continuations": self.stalled_continuations,
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
    available_effects: frozenset[ToolEffect] = frozenset()
    available_tools: tuple[str, ...] = ()
    #: Effect-level failures that no later action resolved. Holds
    #: ``core.execution_facts.ExecutionFact`` values; typed loosely here because
    #: ports must not import core. Kept so a policy can inspect the failure
    #: facts independently of the gaps they produced.
    unresolved_failures: tuple[Any, ...] = ()


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
