"""Project-local configuration for model-visible history bounds.

Two independent bounds live here, both measured against the same thing -- what a
Turn renders from Session history:

* *when to compact* (``compaction_ratio`` / ``latency_soft_tokens``), consumed by
  ``core/context.py``'s ``ContextWindowManager``, which is token-budget driven;
* *how much Session history a Turn renders* (``session_recent_messages`` /
  ``session_task_summaries``), consumed by ``core/session_context.py``'s
  ``SessionContextProjector``, which is a **fixed count** and is therefore
  window-independent -- it plateaus after ``session_recent_messages``/2 turns no
  matter how large the provider window is.

Neither is a tool budget.
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
)


@dataclass(frozen=True, slots=True)
class ContextConfiguration:
    """How much model-visible history a Turn renders, and when it is compacted.

    ``session_recent_messages`` counts *messages*, so a user/assistant round trip
    costs two; the historic default of 8 therefore keeps four round trips. The
    defaults are the values the projector hard-coded before they were reachable
    from configuration, so leaving the settings unset changes nothing.

    ``recent_message_floor`` is the *second*, independent cap, applied by
    ``ContextWindowManager`` only once estimated input passes the compaction
    trigger. Raising only ``session_recent_messages`` therefore does nothing on a
    small provider window: the floor trims the payload back down. Both are
    exposed because both bind.
    """

    compaction_ratio: float = 0.80
    latency_soft_tokens: int = 0
    session_recent_messages: int = 8
    session_task_summaries: int = 4
    recent_message_floor: int = 8
    sources: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not 0 < self.compaction_ratio < 1:
            raise ValueError(
                "TSM_AGT_CONTEXT_COMPACTION_RATIO must be between 0 and 1"
            )
        if self.latency_soft_tokens < 0:
            raise ValueError(
                "TSM_AGT_CONTEXT_LATENCY_SOFT_TOKENS must be zero or positive"
            )
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
    """Build the token-budget compactor from configuration.

    **本项目新增，非 Hermes.** Hermes has per-tool caps, a spill-to-file layer and
    ``enforce_turn_budget``, but no single "keep at least N recent messages"
    knob; this only exposes a value this project already hard-coded.
    """

    return ContextWindowManager(
        trigger_ratio=configuration.compaction_ratio,
        latency_soft_input_tokens=configuration.latency_soft_tokens,
        recent_message_floor=configuration.recent_message_floor,
    )


def session_context_projector(
    configuration: ContextConfiguration,
) -> SessionContextProjector:
    """Build the Session-history renderer from configuration.

    Only the two *count* bounds are configurable here. The remaining projector
    limits (executions, resources, questions, per-Task breadth) are untouched:
    they bound other sections and were never the subject of the multi-turn
    fidelity question.

    **本项目新增，非 Hermes.** Hermes replays its whole ``messages`` table each
    turn and has no equivalent count bound, so there is no upstream shape to
    copy; this only exposes a bound this project already enforced internally.
    """

    return SessionContextProjector(
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
    try:
        ratio = float(ratio_raw)
        soft_tokens = int(soft_raw)
        session_recent_messages = int(messages_raw)
        session_task_summaries = int(summaries_raw)
        recent_message_floor = int(floor_raw)
    except ValueError as error:
        raise ValueError("context compaction settings must be numeric") from error
    return ContextConfiguration(
        compaction_ratio=ratio,
        latency_soft_tokens=soft_tokens,
        session_recent_messages=session_recent_messages,
        session_task_summaries=session_task_summaries,
        recent_message_floor=recent_message_floor,
        sources={
            "context.compaction_ratio": ratio_source,
            "context.latency_soft_tokens": soft_source,
            "context.recent_message_floor": floor_source,
            "session.recent_messages": messages_source,
            "session.task_summaries": summaries_source,
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
