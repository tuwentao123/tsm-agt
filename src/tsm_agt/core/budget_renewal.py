"""Pure, bounded model-call budget renewal decisions.

Renewal is evaluated only at the existing CompletionReadiness boundary. It is
not a second progress model and does not infer whether the Agent is "making
progress" from prose. Runtime already knows whether required work remains and
whether that work is recoverable; this policy only decides how much bounded
capacity may be leased for it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelCallBudgetRenewal:
    """One immutable renewal decision returned by the pure policy."""

    renewal_count: int
    increment: int
    previous_limit: int
    new_limit: int
    remaining_before: int


@dataclass(frozen=True, slots=True)
class DiminishingModelCallBudgetPolicy:
    """Grant configured, diminishing capacity at a recoverable completion gap."""

    increments: tuple[int, ...] = (10, 5, 3)
    max_renewals: int = 3
    absolute_limit: int = 58
    threshold: int = 4

    def __post_init__(self) -> None:
        if not self.increments or any(value <= 0 for value in self.increments):
            raise ValueError("model-call renewal increments must be positive")
        if any(
            later > earlier
            for earlier, later in zip(self.increments, self.increments[1:])
        ):
            raise ValueError("model-call renewal increments must not increase")
        if not 0 <= self.max_renewals <= len(self.increments):
            raise ValueError("max model-call renewals exceeds configured increments")
        if self.absolute_limit < 1 or self.threshold < 0:
            raise ValueError("model-call renewal limits are invalid")

    def evaluate(
        self, *, current_limit: int, consumed: int, renewal_count: int,
        required_recoverable_work: bool,
    ) -> ModelCallBudgetRenewal | None:
        """Return a renewal only when required work and bounded capacity agree."""
        if not required_recoverable_work:
            return None
        if renewal_count < 0 or renewal_count >= self.max_renewals:
            return None
        remaining = max(0, current_limit - consumed)
        if remaining > self.threshold or current_limit >= self.absolute_limit:
            return None
        increment = min(
            self.increments[renewal_count], self.absolute_limit - current_limit
        )
        if increment <= 0:
            return None
        return ModelCallBudgetRenewal(
            renewal_count=renewal_count + 1,
            increment=increment,
            previous_limit=current_limit,
            new_limit=current_limit + increment,
            remaining_before=remaining,
        )
