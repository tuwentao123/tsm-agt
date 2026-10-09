"""Pure, deterministic boundary for provider-supplied tool results."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tsm_agt.ports.tool import ToolCall, ToolResult, ToolResultAuthority, ToolSpec


_REDACTED = "[REDACTED]"
_TRUNCATED = "…"
_CHARS_PER_TOKEN = 4
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|[_\-.])(authorization|proxy-authorization|cookie|set-cookie|"
    r"token|access[_-]?token|refresh[_-]?token|id[_-]?token|secret|"
    r"client[_-]?secret|password|passwd|api[_-]?key|apikey|private[_-]?key|"
    r"credential|session)(?:$|[_\-.])",
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"(?i)(\bbearer\s+)[a-z0-9._~+/=-]+")
_GITHUB_TOKEN_PATTERN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b")
_OPENAI_TOKEN_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_URL_PATTERN = re.compile(r"https?://[^\s\]\[\"'<>]+", re.IGNORECASE)

# These locate evidence or prescribe a follow-up call. They stay visible even
# when a provider's prose or result collection must be compacted.
_COMMON_IDENTITY_FIELDS = frozenset(
    {
        "blob_sha",
        "canonical_url",
        "content_hash",
        "content_type",
        "count",
        "document_ref",
        "end_line",
        "hash",
        "line",
        "line_count",
        "line_end",
        "line_start",
        "next_page",
        "page",
        "path",
        "per_page",
        "provenance",
        "ref",
        "repo",
        "requested_url",
        "resource_ref",
        "root",
        "sha",
        "sha256",
        "source_kind",
        "start_line",
        "target",
        "target_args",
        "target_arguments",
        "total_lines",
        "truncated",
        "url",
    }
)
_PROFILE_IDENTITIES = {
    "core_read_search": _COMMON_IDENTITY_FIELDS
    | frozenset(
        {
            "pattern",
            "query",
            "search_target",
            "skipped_entries",
            "skipped_files",
            "scanned_entries",
            "scanned_files",
        }
    ),
    "web_open_find": _COMMON_IDENTITY_FIELDS
    | frozenset({"match_count", "status_code", "title"}),
    "github_resolve_source": _COMMON_IDENTITY_FIELDS
    | frozenset({"complete", "number", "source_kind", "tree_truncated"}),
    "generic": _COMMON_IDENTITY_FIELDS,
}


class DefaultToolResultGovernor:
    """Sanitize and compact provider results without changing execution semantics.

    ``ToolSpec.max_result_tokens`` is an estimate of the model-visible content
    budget. It applies only to ``data``, ``message``, and ``hint``; result control
    metadata, fact descriptors, and recovery instructions are deliberately
    excluded so a compacted result remains actionable and attributable.
    """

    def govern(self, spec: ToolSpec, call: ToolCall, result: ToolResult) -> ToolResult:
        if result.call_id != call.call_id:
            raise ValueError("tool result call_id must match the governed call")

        raw_content = {
            "data": result.data,
            "message": result.message,
            "hint": result.hint,
        }
        original_size_bytes = len(_canonical_json(_plain_value(raw_content)).encode("utf-8"))
        sanitized_data, data_redacted = _sanitize_value(result.data)
        sanitized_message, message_redacted = _sanitize_value(result.message)
        sanitized_hint, hint_redacted = _sanitize_value(result.hint)
        sanitized_meta, meta_redacted = _sanitize_value(result.meta)
        if not isinstance(sanitized_meta, Mapping):
            sanitized_meta = {"value": sanitized_meta}
        sanitized_meta = _mapping_without(sanitized_meta, "result_governance")

        profile = _profile_for(spec.name)
        omitted_items: dict[str, int] = {}
        omitted_characters: dict[str, int] = {}
        governed_data, data_truncated, _ = _truncate_value(
            sanitized_data,
            budget=spec.max_result_tokens * _CHARS_PER_TOKEN,
            path="data",
            identity_fields=_PROFILE_IDENTITIES[profile],
            omitted_items=omitted_items,
            omitted_characters=omitted_characters,
        )
        remaining_budget = max(
            0,
            spec.max_result_tokens * _CHARS_PER_TOKEN
            - _content_size(governed_data),
        )
        governed_message, message_truncated, message_size = _truncate_value(
            _optional_text(sanitized_message),
            budget=remaining_budget,
            path="message",
            identity_fields=frozenset(),
            omitted_items=omitted_items,
            omitted_characters=omitted_characters,
        )
        governed_hint, hint_truncated, _ = _truncate_value(
            _optional_text(sanitized_hint),
            budget=max(0, remaining_budget - message_size),
            path="hint",
            identity_fields=frozenset(),
            omitted_items=omitted_items,
            omitted_characters=omitted_characters,
        )
        governor_truncated = data_truncated or message_truncated or hint_truncated
        visible_content = {
            "data": governed_data,
            "message": governed_message,
            "hint": governed_hint,
        }
        metadata = sanitized_meta
        if (
            spec.requires_network
            or spec.result_authority is ToolResultAuthority.EXTERNAL_SERVICE
        ):
            metadata = _mapping_updated(metadata, {"untrusted_data": True})
        metadata = _mapping_updated(
            metadata,
            {
                "result_governance": {
                    "schema_version": 2,
                    "profile": profile,
                    "redacted": data_redacted or message_redacted or hint_redacted or meta_redacted,
                    "provider_truncated": result.truncated,
                    "governor_truncated": governor_truncated,
                    "original_content_bytes": original_size_bytes,
                    "visible_content_bytes": len(
                        _canonical_json(visible_content).encode("utf-8")
                    ),
                    "content_hash": "sha256:"
                    + hashlib.sha256(
                        _canonical_json(visible_content).encode("utf-8")
                    ).hexdigest(),
                    "omitted_items": omitted_items,
                    "omitted_characters": omitted_characters,
                }
            },
        )
        # Do not alter status, error, recovery, or fact semantics. In particular,
        # provider ``truncated`` remains distinct from governor compaction.
        return replace(
            result,
            data=governed_data,
            message=governed_message,
            hint=governed_hint,
            truncated=result.truncated,
            meta=metadata,
        )


def _profile_for(tool_name: str) -> str:
    if tool_name in {
        "core.read_file",
        "core.list_files",
        "core.search_files",
        "core.find_files",
        "core.search_text",
        "core.grep_search",
    }:
        return "core_read_search"
    if tool_name in {"web.open", "web.find_in_page", "web.fetch_markdown"}:
        return "web_open_find"
    if tool_name.startswith("source.github.") or tool_name.startswith("github."):
        return "github_resolve_source"
    return "generic"


def _truncate_value(
    value: Any,
    *,
    budget: int,
    path: str,
    identity_fields: frozenset[str],
    omitted_items: dict[str, int],
    omitted_characters: dict[str, int],
    unbounded: bool = False,
) -> tuple[Any, bool, int]:
    if unbounded:
        return value, False, _content_size(value)
    if value is None or isinstance(value, bool | int | float):
        return value, False, _content_size(value)
    if isinstance(value, str):
        rendered_size = _content_size(value)
        if rendered_size <= budget:
            return value, False, rendered_size
        compact = _head_tail(value, max(1, budget))
        omitted_characters[path] = max(0, len(value) - len(compact.replace(_TRUNCATED, "")))
        return compact, True, _content_size(compact)
    if isinstance(value, Mapping):
        items = list(value.items())
        visible: list[tuple[Any, Any]] = []
        truncated = False
        remaining = budget
        ordinary = [item for item in items if str(item[0]) not in identity_fields]
        for key, item in items:
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if key_text in identity_fields:
                child, child_truncated, child_size = _truncate_value(
                    item,
                    budget=0,
                    path=child_path,
                    identity_fields=identity_fields,
                    omitted_items=omitted_items,
                    omitted_characters=omitted_characters,
                    unbounded=True,
                )
            else:
                remaining_items = len(ordinary)
                allocation = remaining if remaining_items <= 1 else remaining // remaining_items
                child, child_truncated, child_size = _truncate_value(
                    item,
                    budget=allocation,
                    path=child_path,
                    identity_fields=identity_fields,
                    omitted_items=omitted_items,
                    omitted_characters=omitted_characters,
                )
                remaining = max(0, remaining - child_size)
                ordinary.pop(0)
            visible.append((key, child))
            truncated = truncated or child_truncated
        rebuilt = _rebuild_mapping(value, visible)
        return rebuilt, truncated, _content_size(rebuilt)
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        if _content_size(value) <= budget:
            return value, False, _content_size(value)
        selected_indexes = list(range(len(items)))
        if len(items) > 2:
            selected_indexes = [0, len(items) - 1]
            omitted_items[path] = len(items) - 2
        visible_items: list[Any] = []
        truncated = len(selected_indexes) != len(items)
        remaining = budget
        for position, index in enumerate(selected_indexes):
            remaining_items = len(selected_indexes) - position
            allocation = remaining if remaining_items == 1 else remaining // remaining_items
            child, child_truncated, child_size = _truncate_value(
                items[index],
                budget=allocation,
                path=f"{path}[{index}]",
                identity_fields=identity_fields,
                omitted_items=omitted_items,
                omitted_characters=omitted_characters,
            )
            visible_items.append(child)
            remaining = max(0, remaining - child_size)
            truncated = truncated or child_truncated
        rebuilt = _rebuild_sequence(value, visible_items)
        return rebuilt, truncated, _content_size(rebuilt)
    rendered = _sanitize_text(repr(value))[0]
    return _truncate_value(
        rendered,
        budget=budget,
        path=path,
        identity_fields=identity_fields,
        omitted_items=omitted_items,
        omitted_characters=omitted_characters,
    )


def _head_tail(value: str, budget: int) -> str:
    if budget <= len(_TRUNCATED):
        return _TRUNCATED
    visible = max(0, budget - len(_TRUNCATED))
    head_length = visible // 2
    tail_length = visible - head_length
    return value[:head_length] + _TRUNCATED + value[-tail_length:]


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _sanitize_value(value: Any, *, seen: set[int] | None = None) -> tuple[Any, bool]:
    seen = set() if seen is None else seen
    if isinstance(value, str):
        return _sanitize_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value, False
    identity = id(value)
    if identity in seen:
        return "[RECURSIVE]", False
    if isinstance(value, Mapping):
        seen.add(identity)
        try:
            items: list[tuple[Any, Any]] = []
            redacted = False
            for key, item in value.items():
                key_text = str(key)
                if _is_sensitive_key(key_text):
                    items.append((key, _REDACTED))
                    redacted = True
                    continue
                safe_item, item_redacted = _sanitize_value(item, seen=seen)
                items.append((key, safe_item))
                redacted = redacted or item_redacted
            return _rebuild_mapping(value, items), redacted
        finally:
            seen.remove(identity)
    if isinstance(value, list | tuple | set | frozenset):
        seen.add(identity)
        try:
            items: list[Any] = []
            redacted = False
            for item in value:
                safe_item, item_redacted = _sanitize_value(item, seen=seen)
                items.append(safe_item)
                redacted = redacted or item_redacted
            return _rebuild_sequence(value, items), redacted
        finally:
            seen.remove(identity)
    return _sanitize_text(repr(value))


def _mapping_without(value: Mapping[Any, Any], key_to_remove: str) -> Mapping[Any, Any]:
    return _rebuild_mapping(
        value,
        [(key, item) for key, item in value.items() if str(key) != key_to_remove],
    )


def _mapping_updated(
    value: Mapping[Any, Any], updates: Mapping[str, Any]
) -> Mapping[Any, Any]:
    items = [(key, item) for key, item in value.items() if str(key) not in updates]
    items.extend(updates.items())
    return _rebuild_mapping(value, items)


def _rebuild_mapping(value: Mapping[Any, Any], items: list[tuple[Any, Any]]) -> Mapping[Any, Any]:
    try:
        return type(value)(items)
    except (TypeError, ValueError):
        return dict(items)


def _rebuild_sequence(value: Any, items: list[Any]) -> Any:
    if isinstance(value, list):
        return list(items)
    if isinstance(value, tuple):
        return tuple(items)
    if isinstance(value, frozenset):
        return frozenset(items)
    if isinstance(value, set):
        return set(items)
    return list(items)


def _sanitize_text(value: str) -> tuple[str, bool]:
    sanitized = _redact_url_queries(value)
    sanitized, bearer_count = _BEARER_PATTERN.subn(r"\1" + _REDACTED, sanitized)
    sanitized, github_count = _GITHUB_TOKEN_PATTERN.subn(_REDACTED, sanitized)
    sanitized, openai_count = _OPENAI_TOKEN_PATTERN.subn(_REDACTED, sanitized)
    return sanitized, bool(bearer_count or github_count or openai_count or sanitized != value)


def _redact_url_queries(value: str) -> str:
    def redact(match: re.Match[str]) -> str:
        url = match.group(0)
        parsed = urlsplit(url)
        if not parsed.query:
            return url
        query = [
            (key, _REDACTED if _is_sensitive_key(key) else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        return urlunsplit(parsed._replace(query=urlencode(query)))

    return _URL_PATTERN.sub(redact, value)


def _is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_PATTERN.search(key))


def _content_size(value: Any) -> int:
    return len(_canonical_json(_plain_value(value)))


def _plain_value(value: Any, *, seen: set[int] | None = None) -> Any:
    seen = set() if seen is None else seen
    if value is None or isinstance(value, str | bool | int | float):
        return value
    identity = id(value)
    if identity in seen:
        return "[RECURSIVE]"
    if isinstance(value, Mapping):
        seen.add(identity)
        try:
            return {
                str(key): _plain_value(item, seen=seen)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        finally:
            seen.remove(identity)
    if isinstance(value, list | tuple):
        seen.add(identity)
        try:
            return [_plain_value(item, seen=seen) for item in value]
        finally:
            seen.remove(identity)
    if isinstance(value, set | frozenset):
        return sorted((_plain_value(item, seen=seen) for item in value), key=_canonical_json)
    return repr(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
