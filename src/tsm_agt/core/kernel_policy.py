"""Pre/post policy probes and the Tool-resource catalog for `Kernel` (exploration budget, read hits, artifact reads).

A mixin module in the facade-plus-siblings shape (``run_agent.py`` +
``agent/session_persistence.py``): the facade ``core/kernel.py`` imports
this at module level and adds ``PolicyMixin`` to ``Kernel``'s bases, so
**every method resolves through ``Kernel``'s MRO unchanged** --
``self._dependencies``, ``self._append_events`` and ``Kernel``'s own class
attributes all still resolve. Moved bodies are verbatim: nothing is renamed
and no signature changes.

This module must never bind the facade at module level; late-import it
inside a method instead (the project's module-boundary rule).

Moved from ``core/kernel.py``:

* ``_additional_read_roots``
* ``_artifact_read_probe``
* ``_decide_stop_or_pivot``
* ``_evaluate_artifact_read_before``
* ``_evaluate_exploration_before``
* ``_evaluate_exploration_budget_before``
* ``_evaluate_read_hits_before``
* ``_resource_candidates``
* ``_resource_paths``
* ``_resource_records``
* ``_update_artifact_read_after``
* ``_update_exploration_after``
* ``_update_exploration_budget_after``
* ``_update_read_hits_after``
"""

from __future__ import annotations


from collections.abc import Mapping
from dataclasses import field
from pathlib import Path
from tsm_agt.ports import (
    ArtifactReadAction, ArtifactReadDecision, ArtifactReadProbe,
    ArtifactReadState, EvidenceInventory, EvidenceRelationState,
    ExplorationBudgetAction, ExplorationBudgetDecision,
    ExplorationBudgetState, ExplorationBudgetUpdate,
    ExplorationOutcomeState, ReadHitsAction, ReadHitsDecision,
    ReadHitsState, RejectionLoopState, StopOrPivotAction,
    StopOrPivotDecision, StopOrPivotState
)
from typing import Any
from .configuration import canonical_hash
from .execution import ToolCommitState
from .exploration_coordinator import ExplorationCoordinatorAction
from .workspace import file_sha256
from .workspace_access import WorkspaceAccessCapability


class PolicyMixin:
    async def _evaluate_read_hits_before(
        self, task_id: str, turn_id: str, call: ToolCall,
        semantic_action: SemanticAction | None, state_data: Mapping[str, Any],
    ) -> ReadHitsDecision:
        policy = self._dependencies.read_hits_policy
        if policy is None:
            return ReadHitsDecision(ReadHitsAction.ALLOW, "policy_disabled")
        try:
            decision = await policy.before_call(
                call, semantic_action, ReadHitsState.from_data(state_data)
            )
        except Exception as error:
            await self._append_events(task_id, ((
                "read_hits.policy_failed", {
                    "turn_id": turn_id,
                    "phase": "before_call",
                    "tool_call_id": call.call_id,
                    "tool_name": call.name,
                    "error_type": type(error).__name__,
                },
            ),))
            return ReadHitsDecision(ReadHitsAction.ALLOW, "policy_failed")
        await self._append_events(task_id, ((
            "read_hits.action_evaluated", {
                "turn_id": turn_id,
                "tool_call_id": call.call_id,
                "tool_name": call.name,
                "action": decision.action.value,
                "reason": decision.reason,
                "candidate_count": decision.candidate_count,
                "question_hash": decision.question_hash,
            },
        ),))
        if decision.action is ReadHitsAction.REQUIRE_READ:
            await self._append_events(task_id, ((
                "read_hits.required", {
                    "turn_id": turn_id,
                    "tool_call_id": call.call_id,
                    "tool_name": call.name,
                    "candidate_count": decision.candidate_count,
                    "question_hash": decision.question_hash,
                },
            ),))
        return decision

    async def _update_read_hits_after(
        self, task_id: str, turn_id: str, call: ToolCall, result: ToolResult,
        semantic_action: SemanticAction | None, state_data: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        policy = self._dependencies.read_hits_policy
        if policy is None:
            return dict(state_data)
        try:
            update = await policy.after_result(
                call, result, semantic_action, ReadHitsState.from_data(state_data)
            )
        except Exception as error:
            await self._append_events(task_id, ((
                "read_hits.policy_failed", {
                    "turn_id": turn_id,
                    "phase": "after_result",
                    "tool_call_id": call.call_id,
                    "tool_name": call.name,
                    "error_type": type(error).__name__,
                },
            ),))
            return dict(state_data)
        next_data = update.state.to_data()
        if update.transition != "unchanged":
            await self._append_events(task_id, ((
                "read_hits.state_changed", {
                    "turn_id": turn_id,
                    "tool_call_id": call.call_id,
                    "tool_name": call.name,
                    "transition": update.transition,
                    "candidate_count": update.candidate_count,
                    "active": update.state.active,
                    "question_hash": update.state.question_hash,
                },
            ),))
        return next_data

    def _artifact_read_probe(
        self, workspace: Path, call: ToolCall, state_data: Mapping[str, Any],
    ) -> ArtifactReadProbe:
        if call.name != "core.read_file":
            return ArtifactReadProbe()
        raw_path = call.arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return ArtifactReadProbe()
        try:
            start_line = int(call.arguments.get("start_line", 1))
            max_lines = int(call.arguments.get("max_lines", 200))
        except (TypeError, ValueError):
            return ArtifactReadProbe()
        try:
            resolved = self._dependencies.workspace_path.resolve_access_path(
                workspace, raw_path
            )
        except (OSError, PermissionError, ValueError):
            return ArtifactReadProbe()
        path_hash = canonical_hash(resolved.canonical_key)
        known = any(
            record.path_hash == path_hash
            for record in ArtifactReadState.from_data(state_data).records
        )
        current_hash = ""
        if known and resolved.path.is_file():
            try:
                current_hash = file_sha256(resolved.path)
            except OSError:
                current_hash = ""
        return ArtifactReadProbe(
            path_hash, current_hash, start_line, max_lines
        )

    async def _evaluate_artifact_read_before(
        self, task_id: str, turn_id: str, call: ToolCall,
        probe: ArtifactReadProbe, state_data: Mapping[str, Any],
    ) -> ArtifactReadDecision:
        policy = self._dependencies.artifact_read_policy
        if policy is None:
            return ArtifactReadDecision(ArtifactReadAction.ALLOW, "policy_disabled")
        try:
            decision = await policy.before_call(
                call, probe, ArtifactReadState.from_data(state_data)
            )
        except Exception as error:
            await self._append_events(task_id, ((
                "artifact_read.policy_failed", {
                    "turn_id": turn_id, "phase": "before_call",
                    "tool_call_id": call.call_id, "tool_name": call.name,
                    "error_type": type(error).__name__,
                },
            ),))
            return ArtifactReadDecision(ArtifactReadAction.ALLOW, "policy_failed")
        await self._append_events(task_id, ((
            "artifact_read.action_evaluated", {
                "turn_id": turn_id, "tool_call_id": call.call_id,
                "tool_name": call.name, "action": decision.action.value,
                "reason": decision.reason,
                "matching_record_count": decision.matching_record_count,
                "path_hash": decision.path_hash,
                "question_hash": decision.question_hash,
            },
        ),))
        if decision.action is ArtifactReadAction.REQUIRE_REUSE:
            await self._append_events(task_id, ((
                "artifact_read.reuse_required", {
                    "turn_id": turn_id, "tool_call_id": call.call_id,
                    "tool_name": call.name,
                    "matching_record_count": decision.matching_record_count,
                    "path_hash": decision.path_hash,
                    "question_hash": decision.question_hash,
                },
            ),))
        return decision

    async def _update_artifact_read_after(
        self, task_id: str, turn_id: str, call: ToolCall, result: ToolResult,
        probe: ArtifactReadProbe, state_data: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        policy = self._dependencies.artifact_read_policy
        if policy is None:
            return dict(state_data)
        try:
            update = await policy.after_result(
                call, result, probe, ArtifactReadState.from_data(state_data)
            )
        except Exception as error:
            await self._append_events(task_id, ((
                "artifact_read.policy_failed", {
                    "turn_id": turn_id, "phase": "after_result",
                    "tool_call_id": call.call_id, "tool_name": call.name,
                    "error_type": type(error).__name__,
                },
            ),))
            return dict(state_data)
        if update.transition != "unchanged":
            await self._append_events(task_id, ((
                "artifact_read.state_changed", {
                    "turn_id": turn_id, "tool_call_id": call.call_id,
                    "tool_name": call.name,
                    "transition": update.transition,
                    "record_count": update.record_count,
                    "path_hash": probe.path_hash,
                },
            ),))
        return update.state.to_data()

    async def _evaluate_exploration_before(
        self, task_id: str, turn_id: str, call: ToolCall,
        semantic_action: SemanticAction | None,
        relation_state_data: Mapping[str, Any],
        rejection_state_data: Mapping[str, Any],
        inventory_data: Mapping[str, Any],
    ):
        """Ask replaceable soft policies; authority checks still happen later."""
        coordinator = self._exploration_coordinator
        if coordinator is None:
            raise RuntimeError("exploration coordinator is not configured")
        decision = await coordinator.before_call(
            call, semantic_action,
            EvidenceRelationState.from_data(relation_state_data),
            RejectionLoopState.from_data(rejection_state_data),
            EvidenceInventory.from_data(inventory_data),
        )
        await self._append_events(task_id, ((
            "evidence_relation.evaluated", {
                "turn_id": turn_id, "tool_call_id": call.call_id,
                "tool_name": call.name,
                "assessment": decision.assessment.kind.value,
                "reason": decision.assessment.reason,
                "relation_kind": decision.assessment.relation_kind,
                "relation_count": decision.relation_count,
                "action": decision.action.value,
            },
        ),))
        if decision.rejection_occurrence:
            await self._append_events(task_id, ((
                "rejection_loop.decision_made", {
                    "turn_id": turn_id, "tool_call_id": call.call_id,
                    "tool_name": call.name, "action": decision.action.value,
                    "reason": decision.reason,
                    "occurrence": decision.rejection_occurrence,
                },
            ),))
        if decision.action is ExplorationCoordinatorAction.ALLOW_BOUNDED_PROBE:
            await self._append_events(task_id, ((
                "exploration.bounded_probe_allowed", {
                    "turn_id": turn_id, "tool_call_id": call.call_id,
                    "tool_name": call.name, "reason": decision.reason,
                    "argument_keys": sorted(decision.call.arguments),
                },
            ),))
        elif decision.action is ExplorationCoordinatorAction.REWRITE:
            await self._append_events(task_id, ((
                "exploration.action_rewritten", {
                    "turn_id": turn_id, "tool_call_id": call.call_id,
                    "tool_name": call.name, "reason": decision.reason,
                    "argument_keys": sorted(decision.call.arguments),
                },
            ),))
        elif decision.action is ExplorationCoordinatorAction.STOP_ROUTE:
            await self._append_events(task_id, ((
                "exploration.route_stopped", {
                    "turn_id": turn_id, "tool_call_id": call.call_id,
                    "tool_name": call.name, "reason": decision.reason,
                },
            ),))
        return decision

    async def _update_exploration_after(
        self, task_id: str, turn_id: str, call: ToolCall, result: ToolResult,
        semantic_action: SemanticAction | None, evidence_delta: EvidenceDelta | None,
        relation_state_data: Mapping[str, Any],
        rejection_state_data: Mapping[str, Any],
        outcome_state_data: Mapping[str, Any],
    ):
        """Checkpoint evidence route state and emit the selected next action."""
        coordinator = self._exploration_coordinator
        if coordinator is None:
            raise RuntimeError("exploration coordinator is not configured")
        update = await coordinator.after_result(
            call, result, semantic_action, evidence_delta,
            EvidenceRelationState.from_data(relation_state_data),
            RejectionLoopState.from_data(rejection_state_data),
            ExplorationOutcomeState.from_data(outcome_state_data),
        )
        await self._append_events(task_id, ((
            "exploration_outcome.evaluated", {
                "turn_id": turn_id, "tool_call_id": call.call_id,
                "tool_name": call.name,
                "action": update.outcome.action.value,
                "reason": update.outcome.reason,
                "consecutive_no_evidence": (
                    update.outcome.state.consecutive_no_evidence
                ),
            },
        ),))
        return update

    async def _decide_stop_or_pivot(
        self, task_id: str, turn_id: str, call: ToolCall,
        signals: StopOrPivotSignals, state_data: Mapping[str, Any],
    ) -> tuple[StopOrPivotDecision, Mapping[str, Any]]:
        policy = self._dependencies.stop_or_pivot_policy
        if policy is None:
            if signals.plan_should_stop:
                action = (
                    StopOrPivotAction.SUMMARIZE_WITH_EVIDENCE
                    if signals.plan_finished
                    else StopOrPivotAction.STOP_NO_PROGRESS
                )
                return (
                    StopOrPivotDecision(
                        action, "legacy_plan_stop", terminal=True
                    ), dict(state_data),
                )
            if signals.budget_wrap_up:
                return (
                    StopOrPivotDecision(
                        StopOrPivotAction.SUMMARIZE_WITH_EVIDENCE,
                        signals.budget_reason or "legacy_budget_wrap_up",
                        terminal=True,
                        low_value_streak=signals.low_value_streak,
                    ), dict(state_data),
                )
            return (
                StopOrPivotDecision(StopOrPivotAction.CONTINUE, "policy_disabled"),
                dict(state_data),
            )
        try:
            update = await policy.decide(
                call, signals, StopOrPivotState.from_data(state_data)
            )
        except Exception as error:
            await self._append_events(task_id, ((
                "stop_or_pivot.policy_failed", {
                    "turn_id": turn_id, "tool_call_id": call.call_id,
                    "tool_name": call.name, "error_type": type(error).__name__,
                },
            ),))
            return (
                StopOrPivotDecision(StopOrPivotAction.CONTINUE, "policy_failed"),
                dict(state_data),
            )
        decision = update.decision
        await self._append_events(task_id, ((
            "stop_or_pivot.decision_made", {
                "turn_id": turn_id, "tool_call_id": call.call_id,
                "tool_name": call.name, "action": decision.action.value,
                "reason": decision.reason, "terminal": decision.terminal,
                "candidate_count": decision.candidate_count,
                "low_value_streak": decision.low_value_streak,
                "semantic_signature": signals.semantic_signature,
                "semantic_family": signals.semantic_family,
            },
        ),))
        return decision, update.state.to_data()

    async def _evaluate_exploration_budget_before(
        self, task_id: str, turn_id: str, call: ToolCall,
        semantic_action: SemanticAction | None, probe: ExplorationBudgetProbe,
        state_data: Mapping[str, Any],
    ) -> ExplorationBudgetDecision:
        policy = self._dependencies.exploration_budget_policy
        if policy is None:
            return ExplorationBudgetDecision(
                ExplorationBudgetAction.CONTINUE, "policy_disabled"
            )
        try:
            decision = await policy.before_call(
                call, semantic_action, probe,
                ExplorationBudgetState.from_data(state_data),
            )
        except Exception as error:
            await self._append_events(task_id, ((
                "exploration_budget.policy_failed", {
                    "turn_id": turn_id, "phase": "before_call",
                    "tool_call_id": call.call_id, "tool_name": call.name,
                    "error_type": type(error).__name__,
                },
            ),))
            return ExplorationBudgetDecision(
                ExplorationBudgetAction.CONTINUE, "policy_failed"
            )
        await self._append_events(task_id, ((
            "exploration_budget.action_evaluated", {
                "turn_id": turn_id, "tool_call_id": call.call_id,
                "tool_name": call.name, "action": decision.action.value,
                "reason": decision.reason,
                "low_value_streak": decision.low_value_streak,
                "cumulative_tool_milliseconds": (
                    decision.cumulative_tool_milliseconds
                ),
                "remaining_model_calls": probe.remaining_model_calls,
                "remaining_tool_calls": probe.remaining_tool_calls,
            },
        ),))
        if decision.action is ExplorationBudgetAction.WRAP_UP:
            await self._append_events(task_id, ((
                "exploration_budget.wrap_up_required", {
                    "turn_id": turn_id, "tool_call_id": call.call_id,
                    "tool_name": call.name, "reason": decision.reason,
                    "low_value_streak": decision.low_value_streak,
                    "cumulative_tool_milliseconds": (
                        decision.cumulative_tool_milliseconds
                    ),
                },
            ),))
        return decision

    async def _update_exploration_budget_after(
        self, task_id: str, turn_id: str, call: ToolCall, result: ToolResult,
        semantic_action: SemanticAction | None, evidence_delta: EvidenceDelta | None,
        observation: ExplorationBudgetObservation,
        state_data: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], ExplorationBudgetUpdate | None]:
        policy = self._dependencies.exploration_budget_policy
        if policy is None:
            return dict(state_data), None
        try:
            update = await policy.after_result(
                call, result, semantic_action, evidence_delta, observation,
                ExplorationBudgetState.from_data(state_data),
            )
        except Exception as error:
            await self._append_events(task_id, ((
                "exploration_budget.policy_failed", {
                    "turn_id": turn_id, "phase": "after_result",
                    "tool_call_id": call.call_id, "tool_name": call.name,
                    "error_type": type(error).__name__,
                },
            ),))
            return dict(state_data), None
        await self._append_events(task_id, ((
            "exploration_budget.action_scored", {
                "turn_id": turn_id, "tool_call_id": call.call_id,
                "tool_name": call.name, "score": update.score,
                "value_band": update.value_band,
                "new_evidence": update.new_evidence,
                "semantic_repeat": update.semantic_repeat,
                "elapsed_milliseconds": update.elapsed_milliseconds,
                "low_value_streak": update.state.low_value_streak,
                "scored_actions": update.state.scored_actions,
                "cumulative_tool_milliseconds": (
                    update.state.cumulative_tool_milliseconds
                ),
            },
        ),))
        return update.state.to_data(), update

    @staticmethod
    def _additional_read_roots(
        task: TaskSnapshot, tool_name: str,
    ) -> tuple[Path, ...]:
        """Expose Task grants only to the four built-in read operations."""
        if tool_name not in {
            "core.read_file", "core.list_files", "core.search_files",
            "core.find_files", "core.search_text", "core.grep_search",
        }:
            return ()
        return tuple(
            Path(grant.canonical_root)
            for grant in task.workspace_access_grants
            if grant.capability is WorkspaceAccessCapability.READ
        )

    @staticmethod
    def _resource_records(
        task: TaskSnapshot,
    ) -> tuple[Mapping[str, str], ...]:
        """Project persisted discovery results into a Task-local resource catalog.

        Tool results remain the source of truth. No second database or live cache is
        needed, so references survive SQLite restore and cannot cross Task identity.
        """
        records: dict[str, Mapping[str, str]] = {}
        for execution in task.tool_executions.values():
            result = execution.result
            if (
                execution.state is not ToolCommitState.COMMITTED
                or result is None or not result.ok
                or not isinstance(result.data, Mapping)
            ):
                continue
            for field in ("entries", "matches"):
                values = result.data.get(field)
                if not isinstance(values, (list, tuple)):
                    continue
                for value in values:
                    if not isinstance(value, Mapping):
                        continue
                    required = (
                        value.get("resource_ref"), value.get("path"),
                        value.get("resolved_path"), value.get("resolved_root"),
                        value.get("root_kind"),
                    )
                    if not all(isinstance(item, str) and item for item in required):
                        continue
                    reference = str(required[0])
                    records[reference] = {
                        "resource_ref": reference,
                        "path": str(required[1]),
                        "resolved_path": str(required[2]),
                        "resolved_root": str(required[3]),
                        "root_kind": str(required[4]),
                    }
        return tuple(records[key] for key in sorted(records))

    @classmethod
    def _resource_paths(cls, task: TaskSnapshot) -> Mapping[str, str]:
        return {
            record["resource_ref"]: record["resolved_path"]
            for record in cls._resource_records(task)
        }

    @classmethod
    def _resource_candidates(
        cls, task: TaskSnapshot,
    ) -> Mapping[str, tuple[Mapping[str, str], ...]]:
        grouped: dict[str, list[Mapping[str, str]]] = {}
        for record in cls._resource_records(task):
            grouped.setdefault(record["path"], []).append(record)
        return {
            path: tuple(sorted(values, key=lambda item: item["resolved_path"]))
            for path, values in grouped.items()
        }
