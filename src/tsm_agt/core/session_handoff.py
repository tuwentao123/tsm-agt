"""Deterministic, authority-free Session Task handoff construction.

The goal a derived Task carries is the user's own current request and nothing
else. Facts about the source Task travel separately as ``background_task``, an
explicitly non-authoritative block, so no amount of prompt drift can promote
historical work into this Task's goal or acceptance criteria.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .runtime_input import SessionTaskCatalogEntry

_MAX_GOAL_CHARACTERS = 2000
#: Shortest verbatim span that counts as "copied from background". Long enough
#: to ignore incidental word overlap, short enough to catch a lifted sentence.
_BACKGROUND_SPAN_MIN = 24


def clip_text(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 1)].rstrip() + "…"


#: Backwards-compatible private alias.
_clip = clip_text


def _normalize_for_overlap(value: str) -> str:
    return " ".join(value.casefold().split())


def find_background_leak(
    authored_text: str, background_texts: Sequence[str], *,
    min_span: int = _BACKGROUND_SPAN_MIN,
) -> str | None:
    """Return a background span that appears verbatim in ``authored_text``.

    Deterministic and pure: case-folded, whitespace-collapsed bidirectional
    substring scan. It answers one question only -- did the author copy text
    that it was not allowed to copy? -- and never decides what the author meant.
    """
    authored = _normalize_for_overlap(authored_text)
    if not authored:
        return None
    for background in background_texts:
        source = _normalize_for_overlap(background)
        if len(source) < min_span:
            continue
        for start in range(0, len(source) - min_span + 1):
            if source[start:start + min_span] in authored:
                return source[start:start + min_span]
    return None


@dataclass(frozen=True, slots=True)
class SessionFollowUpHandoff:
    """One derived Task's split inputs: an authoritative goal and background."""

    goal: str
    background_task: Mapping[str, Any]
    background_texts: tuple[str, ...] = ()


def build_session_follow_up_handoff(
    current_input: str, source: SessionTaskCatalogEntry,
) -> SessionFollowUpHandoff:
    """Split the current request from the bounded, non-authoritative source facts.

    Old approvals, checkpoints, tool payloads, process handles and grants are
    deliberately unavailable to this function.
    """
    goal = clip_text(current_input, _MAX_GOAL_CHARACTERS)
    remaining_work = tuple(
        clip_text(value, 140) for value in source.remaining_work[:4]
        if value.strip()
    )
    completed_work = tuple(
        clip_text(value, 110) for value in source.completed_work[:3]
        if value.strip()
    )
    source_goal = clip_text(source.goal, 320)
    background_task: dict[str, Any] = {
        "task_id": source.task_id,
        "state": source.task_state,
        "phase": source.phase1_state or "unknown",
        "verification": source.verification_status or "unknown",
        "goal": source_goal,
        "historical_remaining_work": list(remaining_work),
        "completed_work": list(completed_work),
        "authority": "SCOPED_BACKGROUND",
    }
    background_texts = tuple(
        value for value in (source_goal, *remaining_work, *completed_work) if value
    )
    return SessionFollowUpHandoff(goal, background_task, background_texts)


def build_session_follow_up_goal(
    current_input: str, source: SessionTaskCatalogEntry,
) -> str:
    """Backward-compatible facade: the goal is ONLY the user's current request."""
    return build_session_follow_up_handoff(current_input, source).goal


__all__ = [
    "SessionFollowUpHandoff",
    "build_session_follow_up_goal",
    "build_session_follow_up_handoff",
    "clip_text",
    "find_background_leak",
]
