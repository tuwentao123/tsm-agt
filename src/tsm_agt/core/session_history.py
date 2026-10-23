"""Model-only rendering of authored dialogue and attributed runtime facts.

No native historical tool calls are synthesized from the lossy Session view.
The persisted events and the UI projection are not rewritten by this module.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from tsm_agt.ports import Message, MessageRole, TextBlock

from .configuration import canonical_hash


MANAGED_HISTORY_BUDGETS = {"turn-token-v1", "turn-token-v2"}


def normalize_history_body(
    body: Mapping[str, Any], *, omit_read_bodies: bool = False,
) -> dict[str, Any]:
    """Pool exact read payloads, not execution outcomes or permissions.

    Every call keeps its own source, arguments, status and errors. A reference
    identifies recorded text, not current file contents or a readable resource.
    Also accepts old checkpoint projections containing per-Turn aggregates.
    """
    result = deepcopy(dict(body))
    explicit_aggregates = {item["task_id"]: item for item in result.get("task_scope_observations", [])}
    aggregates = {}
    summaries = result.get("recent_task_summaries", [])
    for summary in summaries:
        aggregate = summary.pop("task_scope_observations", None)
        if aggregate is not None:
            task_id = summary.get("task_id")
            aggregates[task_id] = {**aggregate, "task_id": task_id,
                                   "source_turn_id": summary.get("turn_id")}
            summary["task_scope_ref"] = task_id
    # An explicit latest Task view wins over a legacy per-Turn snapshot.
    result["task_scope_observations"] = list((aggregates | explicit_aggregates).values())
    existing = result.get("read_payloads", {})
    pool: dict[str, Any] = {}
    events = [event for summary in summaries for event in summary.get("execution_events", [])]
    events.extend(result.get("unattributed_execution_events", []))
    for event in events:
        observation = event.get("result", {}).get("observation")
        if not isinstance(observation, dict):
            continue
        payload = {key: observation.pop(key) for key in ("content", "entries", "matches", "candidates")
                   if key in observation}
        reference = observation.get("read_payload_ref")
        if payload:
            reference = canonical_hash({
                "tool": event.get("call", {}).get("tool"),
                "arguments": event.get("call", {}).get("arguments"),
                "source": {key: observation[key] for key in (
                    "path", "resolved_path", "requested_path", "sha256", "start_line", "end_line",
                ) if key in observation},
                "payload": payload,
            })
        elif reference in existing:
            payload = existing[reference]
        if not reference:
            continue
        observation["read_payload_ref"] = reference
        if omit_read_bodies or not payload:
            observation["payload_status"] = "omitted_by_history_budget"
            if "content_included" in observation:
                observation["content_included"] = False
        else:
            pool[reference] = payload
            observation["payload_status"] = "included_once_in_read_payloads"
    result["read_payloads"] = pool
    return result


def _ranges(sequences: Sequence[int]) -> list[list[int]]:
    result: list[list[int]] = []
    for value in sorted(set(sequences)):
        if result and result[-1][1] + 1 == value:
            result[-1][1] = value
        else:
            result.append([value, value])
    return result


def compact_history_body(
    body: Mapping[str, Any], keep_turns: int, *, include_previews: bool = True,
    omit_read_bodies: bool = False,
) -> dict[str, Any]:
    """Drop whole old turns, preserving bounded excerpts as claims, not facts.

    Selection is mechanical and chronological. It does not infer intent, decide
    task completion, or label historical assistant prose true or false.
    """
    groups = history_turn_groups(body.get("recent_messages", []))
    dropped = groups[:-keep_turns] if keep_turns else groups
    kept = groups[-keep_turns:] if keep_turns else []
    rows = [dict(row) for group in kept for row in group]
    identities = {(row.get("task_id"), row.get("turn_id")) for row in rows}
    task_ids = {row.get("task_id") for row in rows if row.get("task_id")}
    pinned = set(body.get("pinned_task_ids", []))
    task_ids.update(pinned)
    result = dict(body)
    result["recent_messages"] = rows
    result["recent_task_summaries"] = [
        item for item in body.get("recent_task_summaries", [])
        if (item.get("task_id"), item.get("turn_id")) in identities
    ]
    # A Task index is an aggregate, not a substitute for a removed Turn. Older
    # entries remain durably queryable and are accounted for below.
    index = body.get("task_index", [])
    result["task_index"] = [item for item in index if item.get("task_id") in task_ids]
    result["task_scope_observations"] = [
        item for item in body.get("task_scope_observations", []) if item.get("task_id") in task_ids
    ]
    result["unattributed_execution_events"] = [
        item for item in body.get("unattributed_execution_events", [])
        if item.get("task_id") in task_ids
    ]
    investigation = body.get("historical_investigation")
    if isinstance(investigation, dict):
        result["historical_investigation"] = {
            **investigation, **{key: [
                item for item in investigation.get(key, []) if item.get("source_task_id") in task_ids
            ] for key in ("resources", "questions")},
        }
    dropped_rows = [row for group in dropped for row in group]
    sequences = [row["source_event_sequence"] for row in dropped_rows
                 if isinstance(row.get("source_event_sequence"), int)]
    earlier = dict(body.get("earlier_summary", {}))
    existing_ranges = earlier.get("source_event_ranges", [])
    # Merge intervals without expanding a possibly very large historical range.
    intervals = sorted([*existing_ranges, *_ranges(sequences)])
    merged: list[list[int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    earlier.update({
        "algorithm": "turn-token-v2", "message_count": earlier.get("message_count", 0) + len(dropped_rows),
        "turn_count": earlier.get("turn_count", 0) + len(dropped),
        "source_event_sequences": [], "source_event_ranges": merged,
        "omitted_task_count": earlier.get("omitted_task_count", 0) + len(index) - len(result["task_index"]),
        "tasks": [], "turn_previews": [],
        "retrieval_reference": {"session_id": body.get("session_id"), "source_event_ranges": merged},
        "warning": "Older dialogue is omitted or excerpted, not verified or completed. References address durable Session events, not tool capabilities.",
    })
    if include_previews:
        summaries = {(item.get("task_id"), item.get("turn_id")): item
                     for item in body.get("recent_task_summaries", [])}
        earlier["turn_previews"] = [
            {"task_id": group[0].get("task_id"), "turn_id": group[0].get("turn_id"),
             "messages": [{"message_id": row.get("message_id"), "role": row.get("role"),
                           "source_event_sequence": row.get("source_event_sequence"),
                           "excerpt": str(row.get("text", ""))[:200],
                           "truncated": len(str(row.get("text", ""))) > 200}
                          for row in group],
             "execution_facts": [{
                 "task_id": event.get("task_id"), "turn_id": event.get("turn_id"),
                 "call": {key: event.get("call", {}).get(key) for key in ("call_id", "tool")},
                 "result": {key: value for key, value in event.get("result", {}).items()
                            if key in {"execution_id", "state", "ok", "error_code", "succeeded", "exit_code", "execution_state", "message"}},
             } for event in summaries.get((group[0].get("task_id"), group[0].get("turn_id")), {}).get("execution_events", [])]}
            for group in dropped
        ]
    earlier["content_hash"] = canonical_hash({key: value for key, value in earlier.items() if key != "content_hash"})
    result["earlier_summary"] = earlier
    result["source_event_sequences"] = sorted({
        row["source_event_sequence"] for row in rows if isinstance(row.get("source_event_sequence"), int)
    })
    result["compaction"] = {"algorithm": "turn-token-v2", "complete_turns_preserved": True,
                            "latest_turn_preserved": True, "previews_included": include_previews,
                            "read_bodies_omitted": omit_read_bodies}
    if isinstance(body.get("selection"), dict):
        result["selection"] = {**body["selection"], "algorithm": "turn-token-v2",
                               "dropped_message_count": earlier["message_count"]}
    return normalize_history_body(result, omit_read_bodies=omit_read_bodies)


def history_turn_groups(rows: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Group real turn identities; old chat rows fall back to user boundaries."""
    groups: list[list[Mapping[str, Any]]] = []
    last_identity: tuple[Any, Any] | None = None
    for row in rows:
        identity = (row.get("task_id"), row.get("turn_id"))
        has_turn = bool(identity[1])
        new_group = not groups or (
            has_turn and identity != last_identity
        ) or (not has_turn and (last_identity is not None or row.get("role") == "user"))
        if new_group:
            groups.append([])
        groups[-1].append(row)
        last_identity = identity if has_turn else None
    return groups


def render_session_history(message: Message) -> tuple[Message, ...]:
    if not message.message_id.startswith("session-context-"):
        return (message,)
    try:
        body = json.loads(message.text)
    except (ValueError, TypeError):
        return (message,)
    if (not isinstance(body, dict) or body.get("boundary") != "session_conversation_projection"
            or body.get("history_format") != "native"):
        return (message,)
    rows = body.get("recent_messages", [])
    if not isinstance(rows, list):
        return (message,)
    summaries = body.get("recent_task_summaries", [])
    by_turn = {
        (item.get("task_id"), item.get("turn_id")): item
        for item in summaries if isinstance(item, dict) and item.get("turn_id")
    }
    rendered: list[Message] = []
    entries: list[dict[str, Any]] = []
    used: set[tuple[Any, Any]] = set()
    for group in history_turn_groups([row for row in rows if isinstance(row, dict)]):
        for row in group:
            # Old unpaired tool rows stay descriptive metadata. A native TOOL
            # message here would fabricate a protocol exchange we cannot replay.
            if row.get("role") not in {"user", "assistant"}:
                entries.append({key: row.get(key) for key in (
                    "message_id", "role", "task_id", "turn_id", "source_event_sequence",
                    "tool_call_id", "tool_name",
                )})
                continue
            text = row.get("text")
            if not isinstance(text, str) or not text:
                continue
            identity = {key: row.get(key) for key in (
                "message_id", "task_id", "turn_id", "source_event_sequence",
            )}
            native_id = f"session-history-{canonical_hash(identity)[:24]}"
            rendered.append(Message(native_id, MessageRole(row["role"]), (TextBlock(text),)))
            entries.append({**identity, "native_message_id": native_id, "role": row["role"]})
        identity = (group[0].get("task_id"), group[0].get("turn_id"))
        summary = by_turn.get(identity)
        if summary is not None and identity not in used:
            used.add(identity)
            facts = {"boundary": "historical_runtime_facts", "authority_inherited": False,
                     "summary": summary}
            rendered.append(Message(
                f"session-facts-{canonical_hash(facts)[:24]}", MessageRole.USER,
                (TextBlock(json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),),
            ))
    # Dynamic metadata follows authored history so growing revisions do not
    # invalidate the otherwise stable prefix of earlier dialogue messages.
    metadata = {**body, "recent_messages": [], "history_entries": entries,
                "recent_task_summaries": [
                    item for item in summaries if isinstance(item, dict)
                    and (item.get("task_id"), item.get("turn_id")) not in used
                ]}
    rendered.append(Message(
        message.message_id, MessageRole.USER,
        (TextBlock(json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),),
    ))
    return tuple(rendered)
