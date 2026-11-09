"""Durable window baselines and explicit, append-only context updates.

Transport observations and tool results belong to history, not to this diff.
Only project/configuration/reference changes and capability directory changes
produce updates. Memory and progress snapshots are refreshed at window boundaries.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from tsm_agt.ports import Message, MessageRole, SessionEvent, TextBlock, ToolSpec
from .configuration import canonical_hash
from .prompt import PromptTemplate

ROOT_PREFIX = "window-context-root-"
_LIVE_SECTIONS = (
    "project-instructions-context-", "task-spec-context-",
    "conversation-environment-context-", "document-references-context-",
)


def window_identity(*, task_id: str, profile_id: str | None, workspace: str,
                    manifest_hash: str, tools: tuple[ToolSpec, ...],
                    reasoning_effort: str | None, model_fingerprint: str | None = "") -> str:
    return canonical_hash({
        "task_id": task_id, "profile": profile_id, "workspace": workspace,
        "manifest": manifest_hash, "reasoning_effort": reasoning_effort,
        "model_fingerprint": model_fingerprint,
        "tools": [{"name": tool.name, "description": tool.model_description(),
                   "parameters": dict(tool.parameters),
                   # Budget/configuration changes establish a new baseline;
                   # ordinary catalog revisions still append directory updates.
                   "skill_catalog_budget": (tool.model_context.get("budget_tokens")
                                            if tool.model_context and tool.model_context.get("kind") == "skill_catalog" else None)}
                  for tool in tools],
    })


def restore_messages(data: Mapping[str, Any]) -> tuple[Message, ...]:
    # IDs are local storage metadata, never sent as prompt text. Mark earlier
    # authored inputs so source-user lookup still selects this turn's input.
    return tuple(
        replace(message, message_id="window-history-" + message.message_id)
        if message.role is not MessageRole.SYSTEM
        and not message.message_id.startswith(("window-", "session-", "project-", "task-spec-",
                                                "conversation-environment-", "document-", "working-", "execution-", "workflow-handoff-", "tool-catalog-", "context-summary-"))
        else message
        for message in (Message.from_data(row) for row in data.get("messages", []))
    )


def restore_interrupted_inputs(
    events: tuple[SessionEvent, ...], *, task_id: str, before_sequence: int,
) -> tuple[Message, ...]:
    """Recover authored text between the saved window and the new turn."""
    messages = []
    for event in reversed(events):
        if event.sequence >= before_sequence or event.payload.get("task_id") != task_id:
            continue
        # Legacy windows have no cursor. Their last result is the same boundary.
        if event.event_type == "session.task_result_recorded":
            break
        if event.event_type != "session.turn_input_recorded":
            continue
        raw = event.payload.get("user_message")
        if not isinstance(raw, Mapping):
            continue
        try:
            message = Message.from_data(raw)
        except (KeyError, TypeError, ValueError):
            continue
        text = message.text
        if message.role is MessageRole.USER and text.strip():
            # Match ordinary session history: replay text without old attachments.
            messages.append(Message(
                "window-history-" + message.message_id, MessageRole.USER,
                (TextBlock(text),),
            ))
    return tuple(reversed(messages))


def _section(message: Message) -> str | None:
    return next((prefix for prefix in _LIVE_SECTIONS if message.message_id.startswith(prefix)), None)


def _catalogs(tools: tuple[ToolSpec, ...]) -> dict[str, dict[str, Any]]:
    result = {}
    for tool in tools:
        if tool.model_context and tool.model_context.get("kind") in {"mcp_catalog", "skill_catalog"}:
            # A Skill body edit is not a directory change. Explicit discovery
            # still returns its current content revision when needed.
            result[tool.name] = {key: value for key, value in tool.model_context.items() if key != "revision"}
    return result


def establish_window(template: PromptTemplate, projections: tuple[Message, ...],
                     tools: tuple[ToolSpec, ...], identity: str,
                     epoch: int = 1) -> tuple[tuple[Message, ...], dict[str, Any]]:
    assembly = template.assemble(projections, tools)
    baseline = tuple(message for message in assembly.messages if not message.message_id.startswith("prompt-"))
    root = Message(ROOT_PREFIX + str(epoch), MessageRole.USER, (TextBlock(json.dumps({
        "kind": "context_window_baseline", "version": 1, "epoch": epoch,
        "snapshot_rule": "These are window-start snapshots. Later tool results and explicit updates supersede older state. Query current state when needed.",
    })),))
    state = {"identity": identity, "epoch": epoch,
             "sections": {_section(message): canonical_hash(message.to_data()["content"])
                          for message in projections if _section(message)},
             "catalogs": _catalogs(tools)}
    return (root, *baseline), state


def context_updates(previous: Mapping[str, Any], projections: tuple[Message, ...],
                    tools: tuple[ToolSpec, ...]) -> tuple[tuple[Message, ...], dict[str, Any]]:
    state = dict(previous)
    old_sections = dict(state.get("sections", {}))
    sections = {_section(message): message for message in projections if _section(message)}
    updates = []
    for key in sorted(set(old_sections) | set(sections)):
        message = sections.get(key)
        digest = canonical_hash(message.to_data()["content"]) if message else None
        if old_sections.get(key) == digest:
            continue
        if message:
            prefix = "window-system-" if message.role is MessageRole.SYSTEM else "window-context-"
            updates.append(replace(message, message_id=prefix + key + digest[:16]))
        else:
            role = MessageRole.SYSTEM if key != "document-references-context-" else MessageRole.USER
            updates.append(Message("window-system-removed-" + key if role is MessageRole.SYSTEM else "window-context-removed-" + key,
                                   role, (TextBlock("Context section is no longer active: " + key),)))
    state["sections"] = {key: canonical_hash(message.to_data()["content"]) for key, message in sections.items()}
    current = _catalogs(tools)
    old = state.get("catalogs", {})
    changed = {name: value for name, value in current.items() if old.get(name) != value}
    removed = sorted(set(old) - set(current))
    if changed or removed:
        body = {"kind": "capability_directory_update", "catalogs": changed, "removed": removed}
        updates.append(Message("window-context-directory-" + canonical_hash(body)[:16], MessageRole.USER,
                               (TextBlock(json.dumps(body, ensure_ascii=False, sort_keys=True)),)))
    state["catalogs"] = current
    return tuple(updates), state


def window_history(messages: tuple[Message, ...]) -> tuple[Message, ...]:
    from .context_messages import is_generated_context
    return tuple(message for message in messages
                 if not is_generated_context(message.message_id)
                 and not message.message_id.startswith(("window-context-", "window-system-", "tool-catalog-context-")))
