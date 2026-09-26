"""Default safe relation judgement: treat every input as a supplement.

Strategies (model / rule based) replace this Adapter later. Until then every
live input is a supplement, so a running Task is never stopped by a guess that
is not yet proven.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    SessionInputRelation,
    SessionInputRelationJudgement,
)


class DefaultSessionInputRelationJudge:
    """Safe default: append to the running Task, never interrupt it."""

    descriptor = AdapterDescriptor(
        "builtin.default-session-input-relation", "1.0.0",
        "SessionInputRelationPort", "1.0",
        frozenset({"safe-default", "non-interrupting", "replaceable"}),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "default relation judge ready" if self._started else "not started",
        )

    async def judge_input_relation(
        self, text: str, context: Mapping[str, Any],
    ) -> SessionInputRelationJudgement:
        return SessionInputRelationJudgement(
            SessionInputRelation.SUPPLEMENT, 1.0, "default_supplement",
        )
