"""Tool-free semantic handoff across a context-window boundary."""
from __future__ import annotations

from typing import Protocol
from dataclasses import dataclass
from .model import Message, ModelUsage


@dataclass(frozen=True, slots=True)
class ContextSummary:
    text: str
    usage: ModelUsage = ModelUsage()
    model_calls: int = 0


class ContextSummaryPort(Protocol):
    async def summarize(self, messages: tuple[Message, ...], *,
                        context_window: int, model_profile_id: str | None,
                        task_id: str, cancellation_scope: object | None = None) -> ContextSummary: ...
