"""Request-only folding of identical, observed local-file text windows.

The caller supplies canonical messages, not a cache of previously sent bodies.
Every projection chooses its sources again, so removing an old atomic tool
group cannot leave a reference to bytes outside the current model window.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any

from tsm_agt.ports import (
    Message, MessageRole, ToolCall, ToolCallBlock, ToolResult, ToolResultBlock,
    ToolResultMaterialization,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _size(result: ToolResult) -> int:
    return len(_json(result.to_model_data()).encode("utf-8"))


def _unavailable(result: ToolResult) -> ToolResult:
    return replace(result, data={
        key: value for key, value in result.data.items()
        if key not in {"reused_from_call_id", "status"}
    } | {"status": "content_unavailable"}, message=(
        "The earlier text body is not retained in this request. "
        "Read the file again if its exact current content is needed."
    ), meta={key: value for key, value in result.meta.items()
             if key != "read_reuse_projection"})


@dataclass(frozen=True, slots=True)
class ReadReuseStats:
    eligible_read_count: int = 0
    source_result_bytes: int = 0
    model_result_bytes: int = 0
    references: tuple[tuple[str, str], ...] = ()

    def event_data(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "eligible_read_count": self.eligible_read_count,
            "reference_count": len(self.references),
            "source_result_bytes": self.source_result_bytes,
            "model_result_bytes": self.model_result_bytes,
            "saved_result_bytes": self.source_result_bytes - self.model_result_bytes,
            "references": [
                {"call_id": call_id, "source_call_id": source_id}
                for call_id, source_id in self.references
            ],
        }


@dataclass(frozen=True, slots=True)
class ReadReuseProjection:
    messages: tuple[Message, ...]
    stats: ReadReuseStats


def _identity(call: ToolCall, result: ToolResult, task_id: str) -> tuple[Any, ...] | None:
    """Require runtime provenance, version and exact page, not just a path match."""
    data, fact = result.data, result.fact_descriptor
    if (call.name != "core.read_file" or not result.ok or not isinstance(data, Mapping)
            or fact is None or fact.authority != "workspace_read"
            or fact.materialization is not ToolResultMaterialization.CONTENT
            or [scope for scope in fact.scope if scope.startswith("task:")] != ["task:" + task_id]):
        return None
    governance = result.meta.get("result_governance", {})
    if isinstance(governance, Mapping) and governance.get("redacted"):
        return None
    path, root, sha = (data.get(key) for key in ("resolved_path", "resolved_root", "sha256"))
    if (not all(isinstance(value, str) and value for value in (path, root, sha))
            or len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha)
            or fact.content_hash != "sha256:" + sha):
        return None
    fields = tuple(data.get(key) for key in ("start_line", "start_column", "end_line", "total_lines"))
    if any(type(value) is not int for value in fields):
        return None
    start, column, end, total = fields
    if start < 1 or column < 1 or end < start or total < end:
        return None
    requested = tuple(call.arguments.get(key, default) for key, default in (
        ("start_line", 1), ("start_column", 1), ("max_lines", 200),
    ))
    if any(type(value) is not int or value < 1 for value in requested):
        return None
    if requested[:2] != (start, column):
        return None
    cursor = data.get("next_cursor")
    offset = data.get("next_offset")
    if result.truncated:
        if (not isinstance(cursor, Mapping) or set(cursor) != {"start_line", "start_column"}
                or any(type(value) is not int or value < 1 for value in cursor.values())):
            return None
        if type(offset) is not int or offset != cursor["start_line"]:
            return None
    elif cursor is not None or offset is not None:
        return None
    return (task_id, root, path, sha, requested, fields, result.truncated,
            _json(cursor), offset)


def project_read_results(messages: tuple[Message, ...], task_id: str) -> ReadReuseProjection:
    """Return a model view without changing any canonical result or call identity."""
    calls: dict[str, ToolCall] = {}
    ambiguous: set[str] = set()
    for message in messages:
        if message.role is not MessageRole.ASSISTANT:
            continue
        for block in message.content:
            if isinstance(block, ToolCallBlock):
                if block.call.call_id in calls:
                    ambiguous.add(block.call.call_id)
                calls[block.call.call_id] = block.call
    sources: dict[tuple[Any, ...], str] = {}
    emitted: dict[str, tuple[tuple[Any, ...], str]] = {}
    projected: list[Message] = []
    references: list[tuple[str, str]] = []
    eligible, before_bytes, after_bytes = 0, 0, 0
    for message in messages:
        blocks = []
        for block in message.content:
            if not isinstance(block, ToolResultBlock) or message.role is not MessageRole.TOOL:
                blocks.append(block)
                continue
            result = block.result
            call = calls.get(result.call_id)
            identity = (_identity(call, result, task_id)
                        if call is not None and result.call_id not in ambiguous else None)
            data = result.data
            existing = result.meta.get("read_reuse_projection")
            if (isinstance(data, Mapping) and isinstance(existing, Mapping)
                    and data.get("status") == "unchanged"):
                # Check even incomplete legacy references, before the normal
                # provenance filter. Missing proof must never preserve a claim
                # that an absent source is visible in this request.
                source = data.get("reused_from_call_id")
                if (identity is not None and isinstance(source, str)
                        and emitted.get(source) == (identity, existing.get("body_sha256"))):
                    blocks.append(block)
                else:
                    blocks.append(ToolResultBlock(_unavailable(result)))
                continue
            if identity is None:
                blocks.append(block)
                continue
            body = data.get("content")
            if isinstance(body, str) and body:
                digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
                key = (*identity, digest)
                source = sources.get(key)
                replacement = result
                if source is not None and source != result.call_id:
                    replacement = replace(result, data={
                        **{key: value for key, value in data.items() if key != "content"},
                        "status": "unchanged", "content_returned": False,
                        "reused_from_call_id": source,
                    }, message=(
                        f"Same file version and text window as {source}; "
                        "that body is retained in this request. This result is a reference, not file text."
                    ), meta={**result.meta, "read_reuse_projection": {
                        "schema_version": 1, "body_sha256": digest,
                    }})
                    if _size(replacement) >= _size(result):
                        replacement = result
                eligible += 1
                before_bytes += _size(result)
                after_bytes += _size(replacement)
                if replacement is result:
                    sources.setdefault(key, result.call_id)
                    emitted[result.call_id] = (identity, digest)
                else:
                    references.append((result.call_id, source))
                blocks.append(ToolResultBlock(replacement) if replacement is not result else block)
            else:
                blocks.append(block)
        content = tuple(blocks)
        projected.append(message if content == message.content else replace(message, content=content))
    return ReadReuseProjection(tuple(projected), ReadReuseStats(
        eligible, before_bytes, after_bytes, tuple(references),
    ))
