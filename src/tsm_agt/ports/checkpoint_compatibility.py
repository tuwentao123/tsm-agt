"""Replaceable policy boundary for durable Agent checkpoint compatibility."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .adapter import RuntimeAdapter


class CheckpointCompatibilityAction(StrEnum):
    """What Runtime may safely do with a persisted checkpoint."""

    EXACT_RESUME = "EXACT_RESUME"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    REBASE_REQUIRED = "REBASE_REQUIRED"
    REQUIRES_VALIDATION = "REQUIRES_VALIDATION"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class CheckpointCompatibilityProbe:
    """Project-neutral facts collected by Kernel; contains no model judgment."""

    differences: tuple[str, ...] = ()
    reconcilable_differences: tuple[str, ...] = ()
    committed_pending_tool_count: int = 0
    pending_tool_call_count: int = 0
    tool_execution_count: int = 0
    unknown_outcome_count: int = 0
    running_non_idempotent_count: int = 0
    mutation_count: int = 0
    background_process_count: int = 0


@dataclass(frozen=True, slots=True)
class CheckpointCompatibilityDecision:
    """Inspectable policy result consumed deterministically by Kernel."""

    action: CheckpointCompatibilityAction
    reason_code: str
    conflict_reasons: tuple[str, ...] = ()
    rebase_reasons: tuple[str, ...] = ()
    discard_pending_tool_calls: bool = False
    reset_transient_state: bool = False


class CheckpointCompatibilityPolicyPort(RuntimeAdapter, Protocol):
    """Classify compatibility only; implementations must not mutate state."""

    async def evaluate(
        self, probe: CheckpointCompatibilityProbe,
    ) -> CheckpointCompatibilityDecision: ...
