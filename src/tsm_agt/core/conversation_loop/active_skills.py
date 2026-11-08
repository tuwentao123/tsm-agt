"""Retain whole, revalidated current-turn instruction exchanges at a boundary."""
from __future__ import annotations

from collections.abc import Mapping
import json

from tsm_agt.ports import MessageRole, ToolCallBlock, ToolResultBlock
from ..execution import ToolCommitState

MAX_SKILL_BYTES = 16000
MAX_SKILL_RESOURCES = 4


def active_skill_groups(messages, executions, *, turn_id, catalog, registry,
                        groups, budget_bytes):
    """A ledger-backed selection, never handler dispatch or fabricated tool rows.

    Only loads from this Turn qualify. A new user request must choose applicable
    instructions again; restored history alone does not activate an old Skill.
    A newer failed load also supersedes an older successful load of that resource.
    """
    if catalog is None or budget_bytes <= 0:
        return (), {"retained_call_ids": [], "omitted_count": 0}
    latest = {}
    last_steering = max((index for index, message in enumerate(messages)
                         if message.role is MessageRole.USER and message.scaffolding == "steering"), default=-1)
    call_positions = {block.call.call_id: index for index, message in enumerate(messages)
                      for block in message.content if isinstance(block, ToolCallBlock)}
    for row in sorted(executions, key=lambda row: row.started_at):
        if row.turn_id == turn_id and row.call.name == "skill.load":
            args = row.call.arguments
            latest[(args.get("name"), args.get("resource", "SKILL.md"))] = row
    applicable = {key: row for key, row in latest.items()
                  if row.call.call_id in call_positions and call_positions[row.call.call_id] > last_steering}
    if applicable:
        try:
            catalog.refresh(registry)
        except (OSError, ValueError):
            return (), {"retained_call_ids": [], "omitted_count": len(latest)}
    eligible = {}
    candidates = sorted(applicable.items(), key=lambda item: item[1].started_at, reverse=True)
    omitted = len(latest) - len(applicable) + max(0, len(candidates) - MAX_SKILL_RESOURCES)
    for (name, resource), row in candidates[:MAX_SKILL_RESOURCES]:
        result = row.result
        governance = result.meta.get("result_governance") if result is not None else None
        if (row.state is not ToolCommitState.COMMITTED or result is None or not result.ok
                or result.truncated or (isinstance(governance, Mapping) and governance.get("redacted"))
                or not isinstance(result.data, Mapping)
                or not isinstance(result.data.get("content"), str)):
            omitted += 1
            continue
        try:
            # This is the same file-only loader as skill.load: current registry,
            # path permissions, resource boundary and bytes are checked again.
            current = catalog.load_registered(registry, name, resource)
        except (OSError, ValueError):
            omitted += 1
            continue
        if any(current.get(key) != result.data.get(key)
               for key in ("name", "path", "directory", "content", "source")):
            omitted += 1
            continue
        eligible[row.call.call_id] = result
    selected, selected_ids = [], []
    used = 0
    limit = min(MAX_SKILL_BYTES, budget_bytes)
    for group in reversed(groups):
        calls = {block.call.call_id for message in group for block in message.content
                 if isinstance(block, ToolCallBlock)}
        proven = [block.result.call_id for message in group for block in message.content
                  if isinstance(block, ToolResultBlock)
                  and block.result.call_id in calls
                  and eligible.get(block.result.call_id) == block.result]
        if not proven:
            continue
        size = len(json.dumps([m.to_data() for m in group], ensure_ascii=False).encode("utf-8"))
        if used + size > limit or len(selected_ids) + len(proven) > MAX_SKILL_RESOURCES:
            omitted += len(proven)
            continue
        # Preserve the entire native group, including sibling calls, reasoning
        # and tool results. Dropping part would violate provider protocols.
        selected.append(group)
        selected_ids.extend(proven)
        used += size
    return tuple(reversed(selected)), {
        "retained_call_ids": selected_ids, "candidate_bytes": used,
        "budget_bytes": limit, "omitted_count": omitted,
        "scope": "revalidated_current_turn_loads",
    }
