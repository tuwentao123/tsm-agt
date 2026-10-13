"""Agent-progress reporting and progressive-scope policy probes for `Kernel`.

A mixin module in the facade-plus-siblings shape (``run_agent.py`` +
``agent/session_persistence.py``): the facade ``core/kernel.py`` imports
this at module level and adds ``ProgressMixin`` to ``Kernel``'s bases, so
**every method resolves through ``Kernel``'s MRO unchanged** --
``self._dependencies``, ``self._append_events`` and ``Kernel``'s own class
attributes all still resolve. Moved bodies are verbatim: nothing is renamed
and no signature changes.

This module must never bind the facade at module level; late-import it
inside a method instead (the project's module-boundary rule).

Moved from ``core/kernel.py``:

* ``_emit_agent_progress_projection``
* ``_evaluate_progressive_scope_before``
* ``_notify_agent_progress``
* ``_progressive_scope_probe``
* ``_update_progressive_scope_after``
"""

from __future__ import annotations


from collections.abc import Mapping
from pathlib import Path
from tsm_agt.ports import (
    AgentProgressSignals, ProgressiveScopeAction,
    ProgressiveScopeDecision, ProgressiveScopeState, ScopeProbe
)
from typing import Any
from .agent_loop import AgentProgress, AgentProgressKind
from .configuration import canonical_hash


class ProgressMixin:
    @staticmethod
    def _notify_agent_progress(
        callback: Callable[[AgentProgress], None] | None, progress: AgentProgress
    ) -> None:
        if callback is None:
            return
        try:
            callback(progress)
        except Exception:
            # Rendering is observational and must never change Task correctness.
            return

    def _progressive_scope_probe(
        self, workspace: Path, call: ToolCall,
        semantic_action: SemanticAction | None,
    ) -> ScopeProbe:
        if semantic_action is None or semantic_action.family.value not in {
            "SEARCH_CONCEPT", "SEARCH_DEFINITION", "SEARCH_REFERENCES",
            "READ_ARTIFACT", "INSPECT_CONFIGURATION",
        }:
            return ScopeProbe()
        raw_path = call.arguments.get("path", ".")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raw_path = "."
        try:
            resolved = self._dependencies.workspace_path.resolve_access_path(
                workspace, raw_path
            )
        except (OSError, PermissionError, ValueError):
            return ScopeProbe()
        relative = Path(resolved.relative_path)
        parts = () if str(relative) in {"", "."} else relative.parts
        ancestor_hashes: list[str] = []
        for depth in range(0, len(parts)):
            ancestor_relative = "." if depth == 0 else Path(*parts[:depth]).as_posix()
            try:
                ancestor = self._dependencies.workspace_path.resolve_access_path(
                    workspace, ancestor_relative
                )
            except (OSError, PermissionError, ValueError):
                return ScopeProbe()
            ancestor_hashes.append(canonical_hash(ancestor.canonical_key))
        return ScopeProbe(
            canonical_hash(resolved.canonical_key), tuple(ancestor_hashes), len(parts)
        )

    async def _evaluate_progressive_scope_before(
        self, task_id: str, turn_id: str, call: ToolCall,
        semantic_action: SemanticAction | None, probe: ScopeProbe,
        state_data: Mapping[str, Any],
    ) -> ProgressiveScopeDecision:
        policy = self._dependencies.progressive_scope_policy
        if policy is None:
            return ProgressiveScopeDecision(
                ProgressiveScopeAction.ALLOW, "policy_disabled"
            )
        try:
            decision = await policy.before_call(
                call, semantic_action, probe,
                ProgressiveScopeState.from_data(state_data),
            )
        except Exception as error:
            await self._append_events(task_id, ((
                "progressive_scope.policy_failed", {
                    "turn_id": turn_id, "phase": "before_call",
                    "tool_call_id": call.call_id, "tool_name": call.name,
                    "error_type": type(error).__name__,
                },
            ),))
            return ProgressiveScopeDecision(
                ProgressiveScopeAction.ALLOW, "policy_failed"
            )
        await self._append_events(task_id, ((
            "progressive_scope.action_evaluated", {
                "turn_id": turn_id, "tool_call_id": call.call_id,
                "tool_name": call.name, "action": decision.action.value,
                "reason": decision.reason, "relation": decision.relation,
                "previous_depth": decision.previous_depth,
                "current_depth": decision.current_depth,
                "expansion_reason_hash": decision.expansion_reason_hash,
            },
        ),))
        if decision.action is ProgressiveScopeAction.REQUIRE_EXPANSION_REASON:
            await self._append_events(task_id, ((
                "progressive_scope.expansion_reason_required", {
                    "turn_id": turn_id, "tool_call_id": call.call_id,
                    "tool_name": call.name, "relation": decision.relation,
                    "previous_depth": decision.previous_depth,
                    "current_depth": decision.current_depth,
                },
            ),))
        return decision

    async def _update_progressive_scope_after(
        self, task_id: str, turn_id: str, call: ToolCall, result: ToolResult,
        semantic_action: SemanticAction | None, probe: ScopeProbe,
        state_data: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        policy = self._dependencies.progressive_scope_policy
        if policy is None:
            return dict(state_data)
        try:
            update = await policy.after_result(
                call, result, semantic_action, probe,
                ProgressiveScopeState.from_data(state_data),
            )
        except Exception as error:
            await self._append_events(task_id, ((
                "progressive_scope.policy_failed", {
                    "turn_id": turn_id, "phase": "after_result",
                    "tool_call_id": call.call_id, "tool_name": call.name,
                    "error_type": type(error).__name__,
                },
            ),))
            return dict(state_data)
        if update.transition != "unchanged":
            await self._append_events(task_id, ((
                "progressive_scope.state_changed", {
                    "turn_id": turn_id, "tool_call_id": call.call_id,
                    "tool_name": call.name,
                    "transition": update.transition, "depth": update.depth,
                    "zero_results": update.zero_results,
                    "semantic_signature": (
                        semantic_action.semantic_signature
                        if semantic_action is not None else ""
                    ),
                    "scope_hash": probe.scope_hash,
                },
            ),))
        return update.state.to_data()

    async def _emit_agent_progress_projection(
        self, task_id: str, turn_id: str, call: ToolCall,
        semantic_action: SemanticAction | None, scope_relation: str,
        scope_depth: int, evidence_delta: EvidenceDelta | None,
        budget_update: ExplorationBudgetUpdate | None, next_action: str,
        phase: str, callback: Callable[[AgentProgress], None] | None,
        *, reason: str = "", goal: str = "",
        budget_decision: ExplorationBudgetDecision | None = None,
        tool_calls_used: int = 0,
    ) -> None:
        projector = self._dependencies.agent_progress_projector
        if projector is None:
            return
        question_ref = canonical_hash({
            "tool": call.name, "arguments": dict(call.arguments),
        })[:12]
        try:
            projection = await projector.project(AgentProgressSignals(
                phase=phase,
                semantic_family=(
                    semantic_action.family.value if semantic_action is not None else ""
                ),
                question_ref=question_ref,
                scope_kind=(
                    semantic_action.scope_kind.value
                    if semantic_action is not None else "unknown"
                ),
                scope_relation=scope_relation,
                scope_depth=scope_depth,
                evidence_delta=(
                    evidence_delta.total_new if evidence_delta is not None else None
                ),
                consecutive_zero_delta=(
                    evidence_delta.consecutive_zero_delta
                    if evidence_delta is not None else 0
                ),
                budget_score=(budget_update.score if budget_update is not None else None),
                value_band=(
                    budget_update.value_band if budget_update is not None else ""
                ),
                next_action=next_action,
                decision_reason=reason,
            ))
        except Exception as error:
            await self._append_events(task_id, ((
                "agent_progress.projection_failed", {
                    "turn_id": turn_id, "tool_call_id": call.call_id,
                    "tool_name": call.name, "error_type": type(error).__name__,
                },
            ),))
            return
        payload = {
            "turn_id": turn_id, "tool_call_id": call.call_id,
            "tool_name": call.name, "phase": projection.phase,
            "activity": projection.activity,
            "question_ref": projection.question_ref,
            "scope": projection.scope, "scope_change": projection.scope_change,
            "evidence_delta": projection.evidence_delta,
            "consecutive_zero_delta": projection.consecutive_zero_delta,
            "budget_score": projection.budget_score,
            "value_band": projection.value_band,
            "next_action": projection.next_action, "reason": projection.reason,
        }
        await self._append_events(task_id, (("agent_progress.projected", payload),))
        presentation = self._present_tool_arguments(call.name, call.arguments)
        self._notify_agent_progress(callback, AgentProgress(
            AgentProgressKind.EXPLORATION,
            phase=projection.phase, activity=projection.activity,
            question_ref=projection.question_ref, scope=projection.scope,
            scope_change=projection.scope_change,
            evidence_delta=projection.evidence_delta,
            consecutive_zero_delta=projection.consecutive_zero_delta,
            budget_score=projection.budget_score, value_band=projection.value_band,
            next_action=projection.next_action, reason=projection.reason,
            goal=goal,
            question="",
            operation=call.name,
            operation_arguments=dict(presentation.visible_arguments),
            operation_presentation=presentation.text,
            operation_presentation_mode=presentation.mode,
            scope_target=self._live_scope_target(call),
            scope_change_reason="",
            budget_reason=(
                budget_decision.reason if budget_decision is not None else ""
            ),
            exploration_actions=(
                budget_update.state.scored_actions if budget_update is not None
                else (budget_decision.scored_actions if budget_decision else 0)
            ),
            max_exploration_actions=(
                budget_update.max_scored_actions if budget_update is not None
                else (budget_decision.max_scored_actions if budget_decision else 0)
            ),
            exploration_tool_calls=(
                budget_decision.used_tool_calls
                if budget_decision else tool_calls_used
            ),
            max_exploration_tool_calls=(
                budget_update.max_total_tool_calls if budget_update is not None
                else (budget_decision.max_total_tool_calls if budget_decision else 0)
            ),
            exploration_elapsed_seconds=(
                budget_update.state.cumulative_tool_milliseconds / 1000
                if budget_update is not None else (
                    budget_decision.cumulative_tool_milliseconds / 1000
                    if budget_decision else 0
                )
            ),
            max_exploration_elapsed_seconds=(
                budget_update.max_cumulative_tool_milliseconds / 1000
                if budget_update is not None else (
                    budget_decision.max_cumulative_tool_milliseconds / 1000
                    if budget_decision else 0
                )
            ),
            low_value_streak=(
                budget_update.state.low_value_streak if budget_update is not None
                else (budget_decision.low_value_streak if budget_decision else 0)
            ),
            max_low_value_streak=(
                budget_update.max_low_value_streak if budget_update is not None
                else (budget_decision.max_low_value_streak if budget_decision else 0)
            ),
            reserve_tool_calls=(
                budget_update.reserve_tool_calls if budget_update is not None
                else (budget_decision.reserve_tool_calls if budget_decision else 0)
            ),
        ))
