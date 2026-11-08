"""Content-free comparisons at the final provider payload boundary."""
from __future__ import annotations

import hashlib
import inspect
import json
from collections import OrderedDict

from tsm_agt.ports import ModelTransportProgress


def _fingerprint(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest(), len(data)


async def observe_request_prefix(provider, request, payload):
    if not request.context_window_id or request.on_transport_progress is None:
        return
    history = getattr(provider, "_request_prefix_history", None)
    if history is None:
        history = provider._request_prefix_history = OrderedDict()
    key = request.context_window_id
    messages = tuple(_fingerprint(message) for message in payload.get("messages", ()))
    settings = _fingerprint({name: payload.get(name) for name in (
        "model", "system", "tools", "tool_choice", "parallel_tool_calls",
        "reasoning_effort", "thinking",
    )})[0]
    previous = history.get(key)
    common, common_bytes = 0, 0
    if previous and previous[0] == settings:
        for old, new in zip(previous[1], messages):
            if old != new:
                break
            common += 1
            common_bytes += new[1]
    history[key] = settings, messages
    history.move_to_end(key)
    while len(history) > 16:
        history.popitem(last=False)
    update = ModelTransportProgress(
        "request_prefix_observed", 1, 1,
        diagnostics={
            "comparison_scope": "serialized_provider_fields_not_tokenizer_or_cache_hits",
            "has_previous_request": previous is not None,
            "prefix_settings_unchanged": previous is not None and previous[0] == settings,
            "unchanged_leading_messages": common, "unchanged_message_bytes": common_bytes,
            "message_count": len(messages), "prefix_settings_hash": settings,
        },
    )
    result = request.on_transport_progress(update)
    if inspect.isawaitable(result):
        await result
