"""Optional semantic classifier boundary for ambiguous Runtime input."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .adapter import RuntimeAdapter


class RuntimeInputClassifierPort(RuntimeAdapter, Protocol):
    """Classify one ambiguous input without performing any Runtime action."""

    async def classify_runtime_input(
        self, text: str, context: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...
