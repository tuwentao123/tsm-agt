"""Rule-based comparison of declared investigation scope and tool facts."""

from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus, ToolCall,
    ToolResult, ToolScopeConsistencyAction, ToolScopeConsistencyDecision,
    ToolScopeConsistencyPolicyPort, ToolScopeConsistencyProbe, ToolScopeRelation,
)


class RuleBasedToolScopeConsistencyPolicy:
    """Reject only proven scope mismatches, independent of project type.

    Missing declarations and tools without filesystem location facts stay
    observable but allowed for compatibility. A proven mismatch requests one
    model replan and carries no filesystem authority.
    """

    descriptor = AdapterDescriptor(
        "builtin.rule-based-tool-scope-consistency", "1.0.0",
        "ToolScopeConsistencyPolicyPort", "1.0",
        frozenset({"project-neutral", "platform-normalized", "no-authority"}),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "tool scope consistency policy ready"
            if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def evaluate(
        self, call: ToolCall, result: ToolResult, probe: ToolScopeConsistencyProbe,
    ) -> ToolScopeConsistencyDecision:
        if not self._started:
            raise RuntimeError("tool scope consistency policy is not started")
        if probe.relation is ToolScopeRelation.MISMATCH:
            return ToolScopeConsistencyDecision(
                ToolScopeConsistencyAction.REPLAN,
                "actual_tool_scope_does_not_match_declared_scope", probe,
            )
        reasons = {
            ToolScopeRelation.MATCH: "declared_scope_matches_tool_facts",
            ToolScopeRelation.UNDECLARED: "scope_not_declared",
            ToolScopeRelation.NOT_APPLICABLE: "tool_has_no_scope_facts",
            ToolScopeRelation.UNRESOLVED: "scope_could_not_be_compared",
        }
        return ToolScopeConsistencyDecision(
            ToolScopeConsistencyAction.ALLOW, reasons[probe.relation], probe
        )
