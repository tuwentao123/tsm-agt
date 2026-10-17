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
