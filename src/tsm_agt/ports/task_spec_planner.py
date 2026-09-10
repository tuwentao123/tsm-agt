"""Replaceable semantic planner for an untrusted Task SPEC proposal."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .adapter import RuntimeAdapter


class TaskSpecPlannerPort(RuntimeAdapter, Protocol):
    """Propose outcomes without assigning Runtime identity or status."""

    async def propose_task_spec(
        self, goal: str, context: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...
