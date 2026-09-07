"""Allow-listed project-neutral investigation facts for Flow/Replay."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, EVIDENCE_CATEGORIES, HealthState,
    HealthStatus, InvestigationFlowFact,
)


class RuleBasedInvestigationFlowProjector:
    descriptor = AdapterDescriptor(
        "builtin.rule-based-investigation-flow", "1.0.0",
        "InvestigationFlowProjectorPort", "1.0",
        frozenset({"read-only", "redacted", "replay-safe"}),
    )
    _FAMILIES = frozenset({
        "INSPECT_PROJECT_STRUCTURE", "SEARCH_CONCEPT", "SEARCH_DEFINITION",
        "SEARCH_REFERENCES", "READ_ARTIFACT", "INSPECT_CONFIGURATION",
        "MUTATE_WORKSPACE", "EXECUTE_VERIFICATION", "EXECUTE_COMMAND",
        "CONTROL_PROCESS", "REQUEST_CLARIFICATION",
        "MANAGE_AGENT_STATE", "USE_TOOL",
    })
    _SCOPES = frozenset({
        "workspace", "directory", "file", "symbol", "external",
        "internal", "unknown",
    })
    _SCOPE_CHANGES = frozenset({
        "first", "same", "narrowed", "expanded", "lateral",
        "unknown",
    })
    _VALUE_BANDS = frozenset({"useful", "low", "ignored"})
    _DECISIONS = frozenset({
        "CONTINUE", "READ_HITS", "NARROW_SCOPE", "CHANGE_METHOD",
        "ASK_USER", "SUMMARIZE_WITH_EVIDENCE", "STOP_NO_PROGRESS",
    })
    _PIVOT_REASONS = frozenset({
        "explicit_clarification", "bounded_candidates_available",
        "unjustified_scope_expansion", "plan_finished",
        "plan_no_progress", "method_change_not_observed", "method_changed",
        "consecutive_low_value", "cumulative_tool_time",
        "tool_budget_reserve", "model_budget_reserve", "budget_wrap_up",
        "healthy_progress", "legacy_plan_stop", "legacy_budget_wrap_up",
        "policy_disabled", "policy_failed",
    })

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "investigation Flow projector ready"
            if self._started else "not started",
        )

    def project_event(
        self, event_type: str, payload: Mapping[str, object]
    ) -> InvestigationFlowFact | None:
        if not self._started:
            raise RuntimeError("investigation Flow projector is not started")
        call_id = payload.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            return None
        if event_type == "semantic.action_classified":
            family = str(payload.get("family") or "")
            scope = str(payload.get("scope_kind") or "")
            confidence = payload.get("confidence")
            return InvestigationFlowFact(call_id, "control", "investigation.semantic-action", {
                "family": family if family in self._FAMILIES else "USE_TOOL",
                "scope": scope if scope in self._SCOPES else "unknown",
                "confidence_percent": (
                    max(0, min(100, round(float(confidence) * 100)))
                    if isinstance(confidence, (int, float)) else None
                ),
            })
        if event_type == "evidence.delta_evaluated":
            raw_counts = payload.get("counts")
            raw_counts = raw_counts if isinstance(raw_counts, Mapping) else {}
            values = {
                category: max(0, int(raw_counts.get(category, 0)))
                for category in EVIDENCE_CATEGORIES
                if isinstance(raw_counts.get(category, 0), int)
            }
            values.update({
                "total_new": max(0, int(payload.get("total_new", 0)))
                if isinstance(payload.get("total_new", 0), int) else 0,
                "consecutive_zero_delta": max(
                    0, int(payload.get("consecutive_zero_delta", 0))
                ) if isinstance(payload.get("consecutive_zero_delta", 0), int) else 0,
            })
            return InvestigationFlowFact(
                call_id, "result", "investigation.evidence-delta", values
            )
        if event_type == "agent_progress.projected":
            phase = str(payload.get("phase") or "")
            if phase not in {"planned", "tool_result"}:
                return None
            scope = str(payload.get("scope") or "unknown")
            change = str(payload.get("scope_change") or "unknown")
            return InvestigationFlowFact(call_id, "control", "investigation.scope", {
                "phase": phase,
                "scope": scope if scope in self._SCOPES else "unknown",
                "scope_change": (
                    change if change in self._SCOPE_CHANGES else "unknown"
                ),
            })
        if event_type == "exploration_budget.action_scored":
            band = str(payload.get("value_band") or "")
            return InvestigationFlowFact(call_id, "control", "investigation.budget-score", {
                "score": payload.get("score") if isinstance(payload.get("score"), int) else None,
                "value_band": band if band in self._VALUE_BANDS else "",
                "new_evidence": payload.get("new_evidence") if isinstance(payload.get("new_evidence"), int) else None,
                "semantic_repeat": bool(payload.get("semantic_repeat", False)),
                "low_value_streak": payload.get("low_value_streak") if isinstance(payload.get("low_value_streak"), int) else None,
                "elapsed_milliseconds": payload.get("elapsed_milliseconds") if isinstance(payload.get("elapsed_milliseconds"), int) else None,
            })
        if event_type == "stop_or_pivot.decision_made":
            action = str(payload.get("action") or "")
            reason = str(payload.get("reason") or "")
            return InvestigationFlowFact(call_id, "control", "investigation.route-decision", {
                "action": action if action in self._DECISIONS else "",
                "reason_code": reason if reason in self._PIVOT_REASONS else "unclassified",
                "terminal": bool(payload.get("terminal", False)),
                "candidate_count": payload.get("candidate_count") if isinstance(payload.get("candidate_count"), int) else None,
                "low_value_streak": payload.get("low_value_streak") if isinstance(payload.get("low_value_streak"), int) else None,
            })
        return None
