"""Safe projection of investigation Events into Flow facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from .adapter import RuntimeAdapter


@dataclass(frozen=True, slots=True)
class InvestigationFlowFact:
    tool_call_id: str
    category: str
    code: str
    values: Mapping[str, str | int | bool | None] = field(default_factory=dict)


class InvestigationFlowProjectorPort(RuntimeAdapter, Protocol):
    def project_event(
        self, event_type: str, payload: Mapping[str, object]
    ) -> InvestigationFlowFact | None: ...
