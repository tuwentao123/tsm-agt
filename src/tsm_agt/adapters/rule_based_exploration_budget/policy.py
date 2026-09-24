"""Conservative project-neutral exploration budget policy."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, EvidenceDelta, ExplorationBudgetAction,
    ExplorationBudgetDecision, ExplorationBudgetObservation,
    ExplorationBudgetPolicyPort, ExplorationBudgetProbe, ExplorationBudgetState,
    ExplorationBudgetUpdate, HealthState, HealthStatus, SemanticAction,
    SemanticActionFamily, ToolCall, ToolResult,
)


class RuleBasedExplorationBudgetPolicy:
    _EXPLORATION_FAMILIES = frozenset({
        SemanticActionFamily.INSPECT_PROJECT_STRUCTURE,
        SemanticActionFamily.SEARCH_CONCEPT,
        SemanticActionFamily.SEARCH_DEFINITION,
        SemanticActionFamily.SEARCH_REFERENCES,
        SemanticActionFamily.READ_ARTIFACT,
        SemanticActionFamily.INSPECT_CONFIGURATION,
    })
    descriptor = AdapterDescriptor(
        "builtin.rule-based-exploration-budget", "1.0.0",
        "ExplorationBudgetPolicyPort", "1.0",
        frozenset({"project-neutral", "checkpointed", "redacted-events"}),
    )

    def __init__(
        self, *, max_low_value_streak: int = 2,
        max_cumulative_tool_milliseconds: int = 120_000,
        max_scored_actions: int = 24, max_total_tool_calls: int = 24,
        reserve_tool_calls: int = 2, minimum_scored_actions: int = 2,
    ) -> None:
        if min(max_low_value_streak, max_cumulative_tool_milliseconds,
               max_scored_actions, max_total_tool_calls, reserve_tool_calls,
               minimum_scored_actions) < 1:
            raise ValueError("exploration budget thresholds must be positive")
        self.max_low_value_streak = max_low_value_streak
        self.max_cumulative_tool_milliseconds = max_cumulative_tool_milliseconds
        self.max_scored_actions = max_scored_actions
        self.max_total_tool_calls = max_total_tool_calls
        self.reserve_tool_calls = reserve_tool_calls
        self.minimum_scored_actions = minimum_scored_actions
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "exploration budget ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def before_call(
        self, call: ToolCall, semantic_action: SemanticAction | None,
        probe: ExplorationBudgetProbe, state: ExplorationBudgetState,
    ) -> ExplorationBudgetDecision:
        self._require_started()
        if not self._is_exploration(semantic_action):
            return self._continue("non_exploration_action", state, probe)
        if state.scored_actions < self.minimum_scored_actions:
            return self._continue("warming_up", state, probe)
        if probe.remaining_tool_calls <= self.reserve_tool_calls:
            return self._wrap("tool_budget_reserve", state, probe)
        if probe.remaining_model_calls <= 1:
            return self._wrap("model_budget_reserve", state, probe)
        if (
            state.low_value_streak >= self.max_low_value_streak
            and semantic_action is not None
            and semantic_action.family is not SemanticActionFamily.READ_ARTIFACT
        ):
            return self._wrap("consecutive_low_value", state, probe)
        used_tool_calls = max(0, probe.max_tool_calls - probe.remaining_tool_calls)
        focus_reason = state.focus_reason if state.focus_mode else ""
        if not focus_reason and used_tool_calls >= self.max_total_tool_calls:
            focus_reason = "total_tool_action_limit"
        if not focus_reason and state.scored_actions >= self.max_scored_actions:
            focus_reason = "exploration_action_limit"
        if (
            not focus_reason
            and state.cumulative_tool_milliseconds
            >= self.max_cumulative_tool_milliseconds
        ):
            focus_reason = "cumulative_tool_time"
        if focus_reason:
            return self._focus(
                focus_reason, state, probe,
                allows_call=self._is_focused_action(call, semantic_action),
            )
        return self._continue("healthy_budget", state, probe)

    async def after_result(
        self, call: ToolCall, result: ToolResult,
        semantic_action: SemanticAction | None, evidence_delta: EvidenceDelta | None,
        observation: ExplorationBudgetObservation, state: ExplorationBudgetState,
    ) -> ExplorationBudgetUpdate:
        self._require_started()
        if (
            not result.ok
            and bool(result.meta.get("recoverable_input"))
        ):
            return ExplorationBudgetUpdate(
                state, 0, "recoverable_input", 0, False,
                max(0, int(observation.elapsed_milliseconds)),
                self.max_scored_actions, self.max_total_tool_calls,
                self.max_cumulative_tool_milliseconds,
                self.max_low_value_streak, self.reserve_tool_calls,
            )
        if not self._is_exploration(semantic_action):
            return ExplorationBudgetUpdate(
                state, 0, "ignored", 0, False,
                max(0, int(observation.elapsed_milliseconds)),
                self.max_scored_actions, self.max_total_tool_calls,
                self.max_cumulative_tool_milliseconds,
                self.max_low_value_streak, self.reserve_tool_calls,
            )
        elapsed = max(0, min(int(observation.elapsed_milliseconds), 3_600_000))
        new_evidence = evidence_delta.total_new if evidence_delta is not None else 0
        signature = (
            semantic_action.semantic_signature if semantic_action is not None else ""
        )
        previous_count = state.semantic_counts.get(signature, 0) if signature else 0
        semantic_repeat = previous_count > 0
        score = self._evidence_value(evidence_delta)
        if evidence_delta is not None and not evidence_delta.has_progress:
            score -= 40
        if not result.ok:
            score -= 30
        if elapsed >= 10_000:
            score -= 30
        elif elapsed >= 5_000:
            score -= 15
        elif elapsed >= 2_000:
            score -= 5
        if observation.broad_search:
            score -= 10
            if self._result_item_count(result) > 20:
                # A large candidate list is work to triage, not strong evidence.
                score -= 20
        if observation.scope_expanded:
            score -= 10
        if semantic_repeat:
            score -= 10
        low_value = score < 0
        counts = dict(state.semantic_counts)
        if signature:
            counts[signature] = previous_count + 1
        next_state = ExplorationBudgetState(
            scored_actions=state.scored_actions + 1,
            cumulative_tool_milliseconds=(
                state.cumulative_tool_milliseconds + elapsed
            ),
            total_new_evidence=state.total_new_evidence + new_evidence,
            low_value_streak=state.low_value_streak + 1 if low_value else 0,
            broad_searches=state.broad_searches + int(observation.broad_search),
            repeated_semantic_actions=(
                state.repeated_semantic_actions + int(semantic_repeat)
            ),
            semantic_counts=counts,
            focus_mode=state.focus_mode,
            focus_reason=state.focus_reason,
        )
        return ExplorationBudgetUpdate(
            next_state, score, "low" if low_value else "useful",
            new_evidence, semantic_repeat, elapsed,
            self.max_scored_actions, self.max_total_tool_calls,
            self.max_cumulative_tool_milliseconds,
            self.max_low_value_streak, self.reserve_tool_calls,
        )

    @staticmethod
    def _evidence_value(evidence_delta: EvidenceDelta | None) -> int:
        """Score evidence diversity with diminishing returns, not list size."""
        if evidence_delta is None:
            return 0
        counts = evidence_delta.counts
        return (
            min(counts["new_paths"], 2) * 5
            + min(counts["new_symbols"], 3) * 8
            + min(counts["new_relations"], 3) * 6
            + min(counts["new_facts"], 2) * 10
            + min(counts["new_exclusions"], 1) * 3
            + min(counts["resolved_questions"], 1) * 20
            + min(counts["new_verification"], 1) * 20
        )

    @staticmethod
    def _result_item_count(result: ToolResult) -> int:
        data = result.data
        if not isinstance(data, dict):
            return 0
        total = 0
        for field in (
            "matches", "definitions", "references",
            "implementations", "symbols",
        ):
            values = data.get(field)
            if isinstance(values, (list, tuple)):
                total += len(values)
        return total

    def _continue(
        self, reason: str, state: ExplorationBudgetState,
        probe: ExplorationBudgetProbe,
    ) -> ExplorationBudgetDecision:
        return ExplorationBudgetDecision(
            ExplorationBudgetAction.CONTINUE, reason,
            low_value_streak=state.low_value_streak,
            cumulative_tool_milliseconds=state.cumulative_tool_milliseconds,
            scored_actions=state.scored_actions,
            used_tool_calls=max(0, probe.max_tool_calls - probe.remaining_tool_calls),
            max_scored_actions=self.max_scored_actions,
            max_total_tool_calls=self.max_total_tool_calls,
            max_cumulative_tool_milliseconds=self.max_cumulative_tool_milliseconds,
            max_low_value_streak=self.max_low_value_streak,
            reserve_tool_calls=self.reserve_tool_calls,
        )

    def _wrap(
        self, reason: str, state: ExplorationBudgetState,
        probe: ExplorationBudgetProbe,
    ) -> ExplorationBudgetDecision:
        return ExplorationBudgetDecision(
            ExplorationBudgetAction.WRAP_UP, reason,
            low_value_streak=state.low_value_streak,
            cumulative_tool_milliseconds=state.cumulative_tool_milliseconds,
            scored_actions=state.scored_actions,
            used_tool_calls=max(0, probe.max_tool_calls - probe.remaining_tool_calls),
            max_scored_actions=self.max_scored_actions,
            max_total_tool_calls=self.max_total_tool_calls,
            max_cumulative_tool_milliseconds=self.max_cumulative_tool_milliseconds,
            max_low_value_streak=self.max_low_value_streak,
            reserve_tool_calls=self.reserve_tool_calls,
        )

    def _focus(
        self, reason: str, state: ExplorationBudgetState,
        probe: ExplorationBudgetProbe, *, allows_call: bool,
    ) -> ExplorationBudgetDecision:
        decision = self._continue(reason, state, probe)
        return replace(
            decision, action=ExplorationBudgetAction.FOCUS,
            focus_allows_call=allows_call,
        )

    @staticmethod
    def _is_focused_action(
        call: ToolCall, action: SemanticAction | None,
    ) -> bool:
        """Allow bounded evidence completion, not a new broad investigation."""
        if action is None:
            return False
        if call.name in {"core.search_text", "core.grep_search"}:
            path = call.arguments.get("path")
            if not isinstance(path, str) or path.strip() in {"", "."}:
                return False
            raw_limit = call.arguments.get("max_matches", 20)
            return isinstance(raw_limit, int) and 0 < raw_limit <= 20
        if call.name == "core.find_files":
            pattern = call.arguments.get("pattern")
            limit = call.arguments.get("limit", 20)
            return (
                isinstance(pattern, str)
                and bool(pattern.strip())
                and pattern.strip() not in {"*", "**/*"}
                and isinstance(limit, int) and 0 < limit <= 20
            )
        if action.family in {
            SemanticActionFamily.READ_ARTIFACT,
            SemanticActionFamily.SEARCH_DEFINITION,
            SemanticActionFamily.SEARCH_REFERENCES,
            SemanticActionFamily.INSPECT_CONFIGURATION,
        }:
            return True
        return False

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("exploration budget policy is not started")

    @classmethod
    def _is_exploration(cls, action: SemanticAction | None) -> bool:
        return action is not None and action.family in cls._EXPLORATION_FAMILIES
