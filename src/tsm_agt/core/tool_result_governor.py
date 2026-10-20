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
_TEXT_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|"
    r"password|passwd|secret|credential)\b\s*[:=]\s*[\"']?)[^\s,;\"']+"
)


def sanitize_historical_error_text(value: str) -> str:
    """Best-effort redaction for bounded, untrusted historical error text."""
    sanitized, _ = _sanitize_text(value)
    return _TEXT_SECRET_ASSIGNMENT.sub(r"\1" + _REDACTED, sanitized)



class DefaultToolResultGovernor:
    """Redact provider results; never compact them.

    This used to also truncate ``data``/``message``/``hint`` to
    ``ToolSpec.max_result_tokens``. That was a second, invisible limit stacked on
    results the tool had already bounded -- ``core.list_files`` caps at its own
    ``limit``, ``core.read_file`` at ``_MAX_READ_CHARS``, the search tools at
    ``_MAX_SEARCH_MATCHES``/``_MAX_SEARCH_FILES``, and ``core.process_logs`` at
    ``max_output_bytes``. Worse, an over-budget list was collapsed to its **first
    and last** element with ``truncated`` left untouched, so a 31-entry workspace
    listing arrived as two files and read as a complete answer:
    ``task-c4dfa243`` reported ``.DS_Store`` and ``脚本说明.md`` only, and the
    Agent concluded the workspace was not mounted.

    Redaction stays: stripping credentials from a result is a safety boundary, not
    a budget. Size limits belong to the tool that produced the bytes, where they
    are declared in the schema and reported in the result.
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
        governed_data, data_redacted = _sanitize_value(result.data)
        governed_message, message_redacted = _sanitize_value(result.message)
        governed_hint, hint_redacted = _sanitize_value(result.hint)
        sanitized_meta, meta_redacted = _sanitize_value(result.meta)
        if not isinstance(sanitized_meta, Mapping):
            sanitized_meta = {"value": sanitized_meta}
        metadata = _mapping_without(sanitized_meta, "result_governance")
        if (
            spec.requires_network
            or spec.result_authority is ToolResultAuthority.EXTERNAL_SERVICE
        ):
            metadata = _mapping_updated(metadata, {"untrusted_data": True})
        visible_content = {
            "data": governed_data,
            "message": governed_message,
            "hint": governed_hint,
        }
        metadata = _mapping_updated(
            metadata,
            {
                "result_governance": {
                    # Stays 2: ``turn_tool_batch`` treats a 2 as "already
                    # governed, do not govern again".
                    "schema_version": 2,
                    "profile": _profile_for(spec.name),
                    "redacted": (
                        data_redacted or message_redacted
                        or hint_redacted or meta_redacted
                    ),
                    "provider_truncated": result.truncated,
                    "original_content_bytes": original_size_bytes,
                    "visible_content_bytes": len(
                        _canonical_json(visible_content).encode("utf-8")
                    ),
                    "content_hash": "sha256:"
                    + hashlib.sha256(
                        _canonical_json(visible_content).encode("utf-8")
                    ).hexdigest(),
                }
            },
        )
        # Do not alter status, error, recovery, or fact semantics, and do not
        # rewrite the payload: redaction changes values only where a secret was
        # found.
        return replace(
            result,
            data=governed_data,
            message=governed_message,
            hint=governed_hint,
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
