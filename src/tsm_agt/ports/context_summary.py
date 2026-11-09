"""Tool-free semantic handoff across a context-window boundary."""
from __future__ import annotations

from typing import Protocol, Any
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import math
from .model import Message, ModelUsage


@dataclass(frozen=True, slots=True)
class ContextSummary:
    text: str
    usage: ModelUsage = ModelUsage()
    model_calls: int = 0
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContextSummarySettings:
    timeout_seconds: float = 180
    request_timeout_seconds: float = 90
    max_output_tokens: int = 2048
    failure_cooldown_seconds: float = 60
    model_profile_id: str | None = None

    def __post_init__(self):
        for name in ("timeout_seconds", "request_timeout_seconds", "failure_cooldown_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"context.summary_{name} must be a positive finite number")
        if isinstance(self.max_output_tokens, bool) or not isinstance(self.max_output_tokens, int) or self.max_output_tokens < 1:
            raise ValueError("context.summary_max_output_tokens must be a positive integer")
        if self.model_profile_id is not None and (not isinstance(self.model_profile_id, str) or not self.model_profile_id.strip()):
            raise ValueError("context.summary_model_profile_id must be a nonempty string")


class ContextSummaryPort(Protocol):
    async def summarize(self, messages: tuple[Message, ...], *,
                        context_window: int, model_profile_id: str | None,
                        task_id: str, cancellation_scope: object | None = None,
                        on_progress: Callable[[Mapping[str, Any]], Any] | None = None) -> ContextSummary: ...
