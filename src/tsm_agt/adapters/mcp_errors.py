"""Bounded MCP diagnostics; do not expose remote bodies, URLs or credentials."""
from __future__ import annotations

import asyncio


def mcp_failure_detail(error: BaseException | None) -> str:
    if error is None:
        return "connection initialization failed"
    seen: set[int] = set()
    kinds: list[str] = []
    http_statuses: list[int] = []

    def visit(current: BaseException) -> None:
        if id(current) in seen or len(seen) >= 32:
            return
        seen.add(id(current))
        if not isinstance(current, (asyncio.CancelledError, BaseExceptionGroup)):
            kind = type(current).__name__
            if kind not in {"WouldBlock", "EndOfStream"} and kind not in kinds:
                kinds.append(kind)
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and status not in http_statuses:
            http_statuses.append(status)
        if isinstance(current, BaseExceptionGroup):
            for child in current.exceptions:
                visit(child)
        if current.__cause__ is not None:
            visit(current.__cause__)
        # Implicit context can be an unrelated AnyIO queue-control exception.

    visit(error)
    if http_statuses:
        return ", ".join(f"HTTP {status}" for status in http_statuses)
    return ", ".join(kinds[:4]) or "connection initialization failed"
