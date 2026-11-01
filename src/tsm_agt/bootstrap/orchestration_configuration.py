"""Configuration for choosing a Task's immutable orchestration mode."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from tsm_agt.core.conversation_orchestration import OrchestrationMode


@dataclass(frozen=True, slots=True)
class OrchestrationConfiguration:
    """Conversation-only orchestration configuration."""

    mode: OrchestrationMode = OrchestrationMode.CONVERSATION_LOOP
    sources: Mapping[str, str] | None = None

    def snapshot_data(self) -> dict[str, str]:
        return {"mode": self.mode.value.casefold()}


def load_orchestration_configuration(
    document: Mapping[str, object],
) -> OrchestrationConfiguration:
    """Return the sole supported mode.

    The argument keeps all configuration loaders uniform; orchestration has no
    user-selectable alternative yet.
    """
    del document
    return OrchestrationConfiguration(sources={"orchestration.mode": "fixed"})
