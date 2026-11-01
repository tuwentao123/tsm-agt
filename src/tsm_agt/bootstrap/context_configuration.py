"""Model history configuration. Conversation Loop uses one token/Turn budget.

Legacy count knobs remain for scoped work-item projections, not as a second
Conversation cutoff. The json/native switch enables isolated representation
comparisons without changing permission policy or the static prompt.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from tsm_agt.core.context import ContextWindowManager
from tsm_agt.core.session_context import SessionContextProjector


@dataclass(frozen=True, slots=True)
class ContextConfiguration:
    """Token budgeting plus legacy scoped-projection compatibility settings."""

    compaction_ratio: float = 0.80
    latency_soft_tokens: int = 0
    session_recent_messages: int = 8
    session_task_summaries: int = 4
    recent_message_floor: int = 8
    sources: Mapping[str, str] | None = None
    conversation_history_format: str = "native"
    history_soft_tokens: int = 16384

    def __post_init__(self) -> None:
        if self.conversation_history_format not in {"native", "json"}:
            raise ValueError("session.history_format must be native or json")
        if not 0 < self.compaction_ratio < 1:
            raise ValueError(
                "context.compaction_ratio must be between 0 and 1"
            )
        if self.latency_soft_tokens < 0:
            raise ValueError(
                "context.latency_soft_tokens must be zero or positive"
            )
        if self.history_soft_tokens < 0:
            raise ValueError("context.history_soft_tokens must be zero or positive")
        if self.session_recent_messages < 1:
            raise ValueError(
                "session.recent_messages must be positive"
            )
        if self.session_task_summaries < 1:
            raise ValueError(
                "session.task_summaries must be positive"
            )
        if self.recent_message_floor < 1:
            raise ValueError(
                "context.recent_message_floor must be positive"
            )


def context_window_manager(
    configuration: ContextConfiguration,
) -> ContextWindowManager:
    """Build the shared budget; the floor applies to live tool groups only.

    Native Conversation history is trimmed by whole Turn and tokens, not by
    this live-message floor. Scoped legacy JSON retains its compatibility path.
    """

    return ContextWindowManager(
        trigger_ratio=configuration.compaction_ratio,
        latency_soft_input_tokens=configuration.latency_soft_tokens,
        recent_message_floor=configuration.recent_message_floor,
        history_soft_tokens=configuration.history_soft_tokens,
    )


def session_context_projector(
    configuration: ContextConfiguration,
) -> SessionContextProjector:
    """Build a model renderer while retaining legacy scoped-count settings."""

    return SessionContextProjector(
        conversation_history_format=configuration.conversation_history_format,
        recent_visible_message_limit=configuration.session_recent_messages,
        detailed_task_summary_limit=configuration.session_task_summaries,
    )


def load_context_configuration(document: Mapping[str, object]) -> ContextConfiguration:
    """Load context settings from the YAML document, without environment layers."""

    values = _section(document, "context")
    session = _section(document, "session")
    defaults = ContextConfiguration()
    ratio = _number(values.get("compaction_ratio"), "context.compaction_ratio", defaults.compaction_ratio)
    soft_tokens = _nonnegative_int(values.get("latency_soft_tokens"), "context.latency_soft_tokens", defaults.latency_soft_tokens)
    session_recent_messages = _positive_int(session.get("recent_messages"), "session.recent_messages", defaults.session_recent_messages)
    session_task_summaries = _positive_int(session.get("task_summaries"), "session.task_summaries", defaults.session_task_summaries)
    recent_message_floor = _positive_int(values.get("recent_message_floor"), "context.recent_message_floor", defaults.recent_message_floor)
    history_format = session.get("history_format", defaults.conversation_history_format)
    if not isinstance(history_format, str):
        raise ValueError("session.history_format must be a string")
    history_soft_tokens = _nonnegative_int(values.get("history_soft_tokens"), "context.history_soft_tokens", defaults.history_soft_tokens)
    return ContextConfiguration(
        compaction_ratio=ratio,
        latency_soft_tokens=soft_tokens,
        session_recent_messages=session_recent_messages,
        session_task_summaries=session_task_summaries,
        recent_message_floor=recent_message_floor,
        conversation_history_format=history_format,
        history_soft_tokens=history_soft_tokens,
        sources={
            "context.compaction_ratio": "yaml" if "compaction_ratio" in values else "default",
            "context.latency_soft_tokens": "yaml" if "latency_soft_tokens" in values else "default",
            "context.recent_message_floor": "yaml" if "recent_message_floor" in values else "default",
            "session.recent_messages": "yaml" if "recent_messages" in session else "default",
            "session.task_summaries": "yaml" if "task_summaries" in session else "default",
            "session.history_format": "yaml" if "history_format" in session else "default",
            "context.history_soft_tokens": "yaml" if "history_soft_tokens" in values else "default",
        },
    )


def _section(document: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = document.get(name, {})
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a mapping")
    return value


def _number(value: object, name: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def _positive_int(value: object, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value
