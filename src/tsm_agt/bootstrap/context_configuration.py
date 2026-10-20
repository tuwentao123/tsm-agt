"""Model history configuration. Conversation Loop uses one token/Turn budget.

Legacy count knobs remain for scoped work-item projections, not as a second
Conversation cutoff. The json/native switch enables isolated representation
comparisons without changing permission policy or the static prompt.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tsm_agt.core.context import ContextWindowManager
from tsm_agt.core.session_context import SessionContextProjector


CONTEXT_ENV_NAMES = (
    "TSM_AGT_CONTEXT_COMPACTION_RATIO",
    "TSM_AGT_CONTEXT_LATENCY_SOFT_TOKENS",
    "TSM_AGT_SESSION_RECENT_MESSAGES",
    "TSM_AGT_SESSION_TASK_SUMMARIES",
    "TSM_AGT_CONTEXT_RECENT_MESSAGE_FLOOR",
    "TSM_AGT_SESSION_HISTORY_FORMAT",
    "TSM_AGT_CONTEXT_HISTORY_SOFT_TOKENS",
)


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
            raise ValueError("TSM_AGT_SESSION_HISTORY_FORMAT must be native or json")
        if not 0 < self.compaction_ratio < 1:
            raise ValueError(
                "TSM_AGT_CONTEXT_COMPACTION_RATIO must be between 0 and 1"
            )
        if self.latency_soft_tokens < 0:
            raise ValueError(
                "TSM_AGT_CONTEXT_LATENCY_SOFT_TOKENS must be zero or positive"
            )
        if self.history_soft_tokens < 0:
            raise ValueError("TSM_AGT_CONTEXT_HISTORY_SOFT_TOKENS must be zero or positive")
        if self.session_recent_messages < 1:
            raise ValueError(
                "TSM_AGT_SESSION_RECENT_MESSAGES must be positive"
            )
        if self.session_task_summaries < 1:
            raise ValueError(
                "TSM_AGT_SESSION_TASK_SUMMARIES must be positive"
            )
        if self.recent_message_floor < 1:
            raise ValueError(
                "TSM_AGT_CONTEXT_RECENT_MESSAGE_FLOOR must be positive"
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


def load_context_configuration(
    path: Path, environment: Mapping[str, str],
) -> ContextConfiguration:
    values = _read_values(path.expanduser().resolve())
    ratio_raw, ratio_source = _selected(
        "TSM_AGT_CONTEXT_COMPACTION_RATIO", values, environment, "0.80"
    )
    soft_raw, soft_source = _selected(
        "TSM_AGT_CONTEXT_LATENCY_SOFT_TOKENS", values, environment, "0"
    )
    messages_raw, messages_source = _selected(
        "TSM_AGT_SESSION_RECENT_MESSAGES", values, environment, "8"
    )
    summaries_raw, summaries_source = _selected(
        "TSM_AGT_SESSION_TASK_SUMMARIES", values, environment, "4"
    )
    floor_raw, floor_source = _selected(
        "TSM_AGT_CONTEXT_RECENT_MESSAGE_FLOOR", values, environment, "8"
    )
    history_format, history_source = _selected(
        "TSM_AGT_SESSION_HISTORY_FORMAT", values, environment, "native"
    )
    history_tokens_raw, history_tokens_source = _selected(
        "TSM_AGT_CONTEXT_HISTORY_SOFT_TOKENS", values, environment, "16384"
    )
    try:
        ratio = float(ratio_raw)
        soft_tokens = int(soft_raw)
        session_recent_messages = int(messages_raw)
        session_task_summaries = int(summaries_raw)
        recent_message_floor = int(floor_raw)
        history_soft_tokens = int(history_tokens_raw)
    except ValueError as error:
        raise ValueError("context compaction settings must be numeric") from error
    return ContextConfiguration(
        compaction_ratio=ratio,
        latency_soft_tokens=soft_tokens,
        session_recent_messages=session_recent_messages,
        session_task_summaries=session_task_summaries,
        recent_message_floor=recent_message_floor,
        conversation_history_format=history_format,
        history_soft_tokens=history_soft_tokens,
        sources={
            "context.compaction_ratio": ratio_source,
            "context.latency_soft_tokens": soft_source,
            "context.recent_message_floor": floor_source,
            "session.recent_messages": messages_source,
            "session.task_summaries": summaries_source,
            "session.history_format": history_source,
            "context.history_soft_tokens": history_tokens_source,
        },
    )


def _selected(name, file_values, environment, default):
    exported = environment.get(name, "").strip()
    from_file = file_values.get(name, "").strip()
    if exported:
        return exported, "environment"
    if from_file:
        return from_file, "env_file"
    return default, "default"


def _read_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        name, value = (part.strip() for part in line.split("=", 1))
        if name not in CONTEXT_ENV_NAMES:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values
