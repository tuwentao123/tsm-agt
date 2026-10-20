"""Explicit, in-memory request capture for local debugging only.

Capture can expose project text and other confidential information. The
redaction below is best effort, not a security boundary; never enable this on
production traffic or with data the operator cannot inspect. No HTTP headers,
URLs, or API keys are intentionally collected. Nothing is written to disk.
"""
from __future__ import annotations

import json
import re
import threading
import time
import contextvars
import secrets
from typing import Any


_SECRET = re.compile(
    r"(?i)(bearer\s+|(?:api[_-]?key|password|secret|token)\s*[:=]\s*)"
    r"[^\s,;\"']+"
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if re.search(
                r"(?i)^(authorization|api[_-]?key|password|secret|token|access_token)$", str(key)
            ) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        if value.startswith("data:image/"):
            return "[IMAGE REDACTED]"
        return _SECRET.sub(r"\1[REDACTED]", value)
    return value


class RequestDiagnosticCapture:
    """One explicitly enabled snapshot, expiring in memory; caller owns access."""

    def __init__(self, *, acknowledge_sensitive_data: bool, ttl_seconds: float = 60,
                 max_bytes: int = 2_000_000) -> None:
        if not acknowledge_sensitive_data:
            raise ValueError("request capture requires explicit sensitive-data acknowledgement")
        if not 0 < ttl_seconds <= 300 or not 0 < max_bytes <= 2_000_000:
            raise ValueError("capture must be bounded to 300 seconds and 2 MB")
        self._ttl = ttl_seconds
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._timer: threading.Timer | None = None

    def record(self, request: Any, payload: dict[str, Any]) -> None:
        snapshot = _redact({
            "turn_id": request.turn_id,
            "provider_neutral_messages": [m.to_data() for m in request.messages],
            "provider_neutral_tools": [t.to_data() for t in request.tools],
            "provider_payload": payload,
        })
        encoded = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        if len(encoded) > self._max_bytes:
            self.clear()
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._snapshot = json.loads(encoded)
            self._timer = threading.Timer(self._ttl, self.clear)
            self._timer.daemon = True
            self._timer.start()

    def take(self) -> dict[str, Any] | None:
        """Return and remove the latest snapshot; callers must protect it."""
        with self._lock:
            snapshot = self._snapshot
            self._snapshot = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            return snapshot

    def clear(self) -> None:
        with self._lock:
            self._snapshot = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


class TurnRequestDiagnostics:
    """Opt-in, bounded request snapshots keyed by an unguessable capability.

    The context is inherited by the submitted background task; the turn binding
    also permits approval resume on a different task. Nothing is persisted.
    """

    def __init__(self, *, ttl_seconds: float = 3600, max_bytes: int = 2_000_000,
                 max_calls: int = 16) -> None:
        self._ttl = ttl_seconds
        self._max_bytes = max_bytes
        self._max_calls = max_calls
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        self._turns: dict[str, str] = {}
        self._context = contextvars.ContextVar("request_diagnostic", default=None)

    def start(self, session_id: str) -> str:
        key = secrets.token_urlsafe(32)
        with self._lock:
            self._expire_locked()
            if len(self._items) >= 16:
                raise ValueError("too many active diagnostic captures")
            self._items[key] = {"session_id": session_id, "expires": time.monotonic() + self._ttl,
                                "turn_id": None, "requests": [], "bytes": 0, "dropped": 0}
        timer = threading.Timer(self._ttl, self.delete, args=(key,))
        timer.daemon = True
        timer.start()
        return key

    def activate(self, key: str):
        return self._context.set(key)

    def reset(self, token: contextvars.Token) -> None:
        self._context.reset(token)

    def record(self, request: Any, payload: dict[str, Any]) -> None:
        if str(request.purpose) != "AGENT_TURN":
            return
        with self._lock:
            self._expire_locked()
            key = self._turns.get(request.turn_id) or self._context.get()
            item = self._items.get(key)
            if item is None or (item["turn_id"] and item["turn_id"] != request.turn_id):
                return
            if not item["turn_id"]:
                item["turn_id"] = request.turn_id
                self._turns[request.turn_id] = key
            snapshot = _redact({
                "turn_id": request.turn_id,
                "provider_neutral_messages": [m.to_data() for m in request.messages],
                "provider_neutral_tools": [t.to_data() for t in request.tools],
                "provider_payload": payload,
            })
            encoded = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
            if len(item["requests"]) >= self._max_calls or item["bytes"] + len(encoded) > self._max_bytes:
                item["dropped"] += 1
                return
            item["requests"].append(json.loads(encoded))
            item["bytes"] += len(encoded)

    def read(self, key: str, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._expire_locked()
            item = self._items.get(key)
            if item is None or item["session_id"] != session_id:
                return None
            return {"turn_id": item["turn_id"], "requests": list(item["requests"]),
                    "dropped": item["dropped"], "expires_in_seconds": max(0, int(item["expires"] - time.monotonic()))}

    def delete(self, key: str) -> None:
        with self._lock:
            item = self._items.pop(key, None)
            if item and item["turn_id"]:
                self._turns.pop(item["turn_id"], None)

    def clear(self) -> None:
        key = self._context.get()
        if key:
            self.delete(key)

    def _expire_locked(self) -> None:
        for key, item in list(self._items.items()):
            if item["expires"] <= time.monotonic():
                self._items.pop(key, None)
                if item["turn_id"]:
                    self._turns.pop(item["turn_id"], None)
