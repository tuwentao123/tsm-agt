"""Replaceable semantic decision boundary for ordinary Session input."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .adapter import RuntimeAdapter


class SessionRouteResolutionError(ValueError):
    """Stable, redacted failure from the semantic routing Adapter."""

    def __init__(
        self, stage: str, reason_code: str, *, attempt: int,
        finish_reason: str = "", tool_call_count: int = 0,
        returned_tool_names: tuple[str, ...] = (),
        argument_keys: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"{stage}:{reason_code}")
        self.stage = stage
        self.reason_code = reason_code
        self.attempt = attempt
        self.finish_reason = finish_reason
        self.tool_call_count = tool_call_count
        self.returned_tool_names = returned_tool_names
        self.argument_keys = argument_keys

    def event_data(self) -> dict[str, object]:
        return {
            "failure_stage": self.stage,
            "reason_code": self.reason_code,
            "attempt": self.attempt,
            "finish_reason": self.finish_reason,
            "tool_call_count": self.tool_call_count,
            "returned_tool_names": list(self.returned_tool_names),
            "argument_keys": list(self.argument_keys),
        }


class SessionInputResolverPort(RuntimeAdapter, Protocol):
    """Choose a Session action without executing it or granting authority."""

    async def resolve_session_input(
        self, text: str, context: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...
