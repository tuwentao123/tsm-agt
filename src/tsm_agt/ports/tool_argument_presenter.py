"""Replaceable presentation boundary for tool arguments shown to users."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .adapter import RuntimeAdapter


@dataclass(frozen=True, slots=True)
class ToolArgumentPresentation:
    """A bounded UI view; execution still uses the untouched arguments."""

    text: str
    mode: str = "full"
    visible_arguments: Mapping[str, Any] = field(default_factory=dict)


class ToolArgumentPresenterPort(RuntimeAdapter, Protocol):
    """Render arguments without changing the payload bound to approval."""

    def present(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> ToolArgumentPresentation: ...
