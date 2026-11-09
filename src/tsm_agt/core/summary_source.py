"""Lossy, request-only preparation for a single semantic summary.

Canonical messages remain untouched. Omitted tool bodies carry source call IDs
and hashes; exact results remain in the durable event log, not in this projection.
"""
from __future__ import annotations

import hashlib
import json
import re

from tsm_agt.ports import TextBlock, ToolCallBlock, ToolResultBlock
from tsm_agt.ports.model import ReasoningBlock
from dataclasses import replace


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _bounded(value, chars, source):
    if isinstance(value, str) and len(value) > chars:
        important = [line[:300] for line in value.splitlines()
                     if re.search(r"error|fail|exception|traceback|assert|失败|错误", line, re.I)][:12]
        return {"omitted_body": True, "source": source, "characters": len(value),
                "sha256": _digest(value), "head": value[:chars * 3 // 4],
                "tail": value[-chars // 4:], "error_lines": important}
    if isinstance(value, dict):
        return {key: _bounded(item, chars, source) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > 40:
            def failed(item):
                return isinstance(item, dict) and (item.get("ok") is False or item.get("error_code")
                    or item.get("exit_code", 0) not in (0, None) or item.get("returncode", 0) not in (0, None)
                    or str(item.get("status", "")).lower() in {"failed", "error"})
            keep = [i for i, item in enumerate(value) if i < 10 or i >= len(value) - 10 or failed(item)]
            return {"omitted_items": len(value) - len(keep), "source": source, "sha256": _digest(value),
                    "items": [_bounded(value[i], chars, source) for i in keep]}
        return [_bounded(item, chars, source) for item in value]
    return value


def prepare_summary_source(messages, *, tool_text_chars=4000):
    calls = {block.call.call_id: block.call for message in messages for block in message.content
             if isinstance(block, ToolCallBlock)}
    # Identical observations may differ in call IDs and bookkeeping. Match the
    # same tool, arguments, result status and body; changed file contents differ.
    newest, duplicates = {}, {}
    for message in reversed(messages):
        for block in reversed(message.content):
            if isinstance(block, ToolResultBlock):
                result, call = block.result, calls.get(block.result.call_id)
                if call is None:
                    continue
                key = _digest([call.name, dict(call.arguments), result.ok, result.data,
                               result.error_code, result.message])
                if key in newest:
                    duplicates[result.call_id] = newest[key]
                else:
                    newest[key] = result.call_id
    rows, reasoning, catalogs = [], 0, 0
    for message in messages:
        if message.message_id.startswith(("tool-catalog-context-", "window-context-directory-")):
            catalogs += 1
            continue
        blocks = []
        for block in message.content:
            if isinstance(block, ReasoningBlock):
                reasoning += 1
            elif isinstance(block, TextBlock):
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCallBlock):
                call = block.call
                blocks.append({"type": "tool_call", "call_id": call.call_id, "name": call.name,
                               "arguments": _bounded(dict(call.arguments), tool_text_chars, call.call_id)})
            elif isinstance(block, ToolResultBlock):
                result = block.result
                data = ({"same_observation_as_call_id": duplicates[result.call_id]}
                        if result.call_id in duplicates else _bounded(result.data, tool_text_chars, result.call_id))
                blocks.append({"type": "tool_result", "call_id": result.call_id, "ok": result.ok,
                               "error_code": result.error_code, "message": _bounded(result.message, tool_text_chars, result.call_id),
                               "truncated": result.truncated, "data": data})
            else:
                blocks.append({"type": "omitted_nontext", "source_message_id": message.message_id})
        if blocks:
            rows.append({"source_message_id": message.message_id, "role": message.role.value, "content": blocks})
    return rows, {"reasoning_blocks_excluded": reasoning, "catalog_messages_excluded": catalogs,
                  "duplicate_observations": len(duplicates), "source_message_count": len(messages),
                  "projected_message_count": len(rows), "tool_text_chars": tool_text_chars}


def prune_old_tool_messages(messages, protected_ids):
    """Preserve native protocol and recent exchanges; fold only old tool bodies."""
    rows, _ = prepare_summary_source(messages)
    bodies = {block["call_id"]: block["data"] for row in rows for block in row["content"]
              if block["type"] == "tool_result"}
    projected = []
    for message in messages:
        if message.message_id in protected_ids:
            projected.append(message)
            continue
        blocks = tuple(
            ToolResultBlock(replace(block.result, data=bodies.get(block.result.call_id, block.result.data),
                                    meta={**block.result.meta, "summary_pruned_projection": True}))
            if isinstance(block, ToolResultBlock) and block.result.call_id in bodies
            and bodies[block.result.call_id] != block.result.data else block
            for block in message.content
        )
        projected.append(replace(message, content=blocks) if blocks != message.content else message)
    return tuple(projected)
