"""Default, replaceable evaluator for persisted evidence contracts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from tsm_agt.core.single_loop.evidence_gate import evaluate_evidence_contract
from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    EvidenceFact,
    EvidenceGatePort,
    GateDecision,
    HealthState,
    HealthStatus,
    TaskContract,
)


class RuleBasedEvidenceGate:
    """Evaluate mandatory contracts with the built-in evidence matching rules."""

    descriptor = AdapterDescriptor(
        "builtin.rule-based-evidence-gate",
        "1.0.0",
        "EvidenceGatePort",
        "1.0",
        frozenset({"mandatory-user-contracts", "typed-evidence"}),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        del context
        self._started = True

    async def stop(self, deadline: datetime) -> None:
        del deadline
        self._started = False

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "evidence gate ready" if self._started else "not started",
        )

    def evaluate(
        self,
        contract: TaskContract | None,
        facts: Sequence[EvidenceFact],
        capabilities: frozenset[str],
    ) -> GateDecision:
        return evaluate_evidence_contract(contract, facts, capabilities)
