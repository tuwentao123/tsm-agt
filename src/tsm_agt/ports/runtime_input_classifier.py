"""Replaceable semantic boundary for input received during a live Task."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .adapter import RuntimeAdapter


class RuntimeInputClassifierPort(RuntimeAdapter, Protocol):
    """Classify ordinary live input without performing or authorizing it."""

    async def classify_runtime_input(
        self, text: str, context: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...
