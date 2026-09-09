"""Replaceable semantic decision boundary for ordinary Session input."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .adapter import RuntimeAdapter


class SessionInputResolverPort(RuntimeAdapter, Protocol):
    """Choose a Session action without executing it or granting authority."""

    async def resolve_session_input(
        self, text: str, context: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...
