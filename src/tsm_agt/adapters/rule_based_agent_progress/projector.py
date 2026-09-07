"""Deterministic project-neutral progress projection."""

from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, AgentProgressProjection,
    AgentProgressProjectorPort, AgentProgressSignals, HealthState, HealthStatus,
)


class RuleBasedAgentProgressProjector:
    descriptor = AdapterDescriptor(
        "builtin.rule-based-agent-progress", "1.0.0",
        "AgentProgressProjectorPort", "1.0",
        frozenset({"project-neutral", "redacted", "ui-neutral"}),
    )
    _ACTIVITIES = {
        "INSPECT_PROJECT_STRUCTURE": "inspect_structure",
        "SEARCH_CONCEPT": "search_concept",
        "SEARCH_DEFINITION": "search_definition",
        "SEARCH_REFERENCES": "search_references",
        "READ_ARTIFACT": "read_artifact",
        "INSPECT_CONFIGURATION": "inspect_configuration",
        "MUTATE_WORKSPACE": "mutate_workspace",
        "EXECUTE_VERIFICATION": "verify",
        "EXECUTE_COMMAND": "run_command",
        "CONTROL_PROCESS": "control_process",
        "REQUEST_CLARIFICATION": "ask_user",
        "MANAGE_AGENT_STATE": "manage_state",
        "USE_TOOL": "use_tool",
    }
    _SCOPES = frozenset({
        "workspace", "directory", "file", "symbol", "external", "internal",
        "unknown",
    })
    _CHANGES = frozenset({"first", "same", "narrowed", "expanded", "lateral"})

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "agent progress projector ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def project(self, signals: AgentProgressSignals) -> AgentProgressProjection:
        if not self._started:
            raise RuntimeError("agent progress projector is not started")
        return AgentProgressProjection(
            phase=signals.phase,
            activity=self._ACTIVITIES.get(signals.semantic_family, "use_tool"),
            question_ref=signals.question_ref[:12],
            scope=(
                signals.scope_kind if signals.scope_kind in self._SCOPES else "unknown"
            ),
            scope_change=(
                signals.scope_relation
                if signals.scope_relation in self._CHANGES else "unknown"
            ),
            evidence_delta=signals.evidence_delta,
            consecutive_zero_delta=max(0, signals.consecutive_zero_delta),
            budget_score=signals.budget_score,
            value_band=signals.value_band,
            next_action=signals.next_action,
            reason=signals.decision_reason,
        )
