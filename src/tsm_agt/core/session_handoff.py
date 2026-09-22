"""Deterministic, authority-free Session Task handoff construction."""

from __future__ import annotations

from .runtime_input import SessionTaskCatalogEntry

_MAX_GOAL_CHARACTERS = 2000


def _clip(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 1)].rstrip() + "…"


def _list_lines(
    heading: str, values: tuple[str, ...], *, maximum: int, item_limit: int,
) -> str | None:
    selected = tuple(
        _clip(value, item_limit) for value in values[:maximum] if value.strip()
    )
    if not selected:
        return None
    return heading + "\n" + "\n".join(f"- {value}" for value in selected)


def build_session_follow_up_goal(
    current_input: str, source: SessionTaskCatalogEntry,
) -> str:
    """Build a bounded goal without replaying privileged source Task state.

    The current request has the largest budget. Source summaries are already
    deterministic Session projections, so this needs no additional model call.
    Old approvals, checkpoints, tool payloads, process handles and grants are
    deliberately unavailable to this function.
    """
    request = _clip(current_input, 1050)
    sections = [
        "[session-follow-up]",
        f"Current request:\n{request}",
        (
            "Authority-free source Task:\n"
            f"- task_id: {source.task_id}\n"
            f"- state: {source.task_state}\n"
            f"- phase: {source.phase1_state or 'unknown'}\n"
            f"- verification: {source.verification_status or 'unknown'}\n"
            f"- original goal: {_clip(source.goal, 320)}"
        ),
    ]
    for section in (
        _list_lines(
            "Remaining work:", source.remaining_work,
            maximum=4, item_limit=140,
        ),
        _list_lines(
            "Completed work:", source.completed_work,
            maximum=3, item_limit=110,
        ),
        _list_lines(
            "Historical outcomes:", source.outcome_summaries,
            maximum=3, item_limit=100,
        ),
    ):
        if section:
            sections.append(section)
    sections.append(
        "Safety boundary:\n"
        "Create an independent FOLLOW_UP Task under the current Runtime. "
        "Revalidate current workspace and external state. Do not inherit or "
        "replay source approvals, grants, checkpoints, tool batches, process "
        "handles, or unknown outcomes."
    )
    result = "\n\n".join(sections)
    if len(result) > _MAX_GOAL_CHARACTERS:
        result = result[: _MAX_GOAL_CHARACTERS - 1].rstrip() + "…"
    return result
