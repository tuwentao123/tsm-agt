"""Deterministic plan focus and repeated-action progress guard."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tsm_agt.ports import SemanticAction, ToolCall

from .configuration import canonical_hash
from .working_memory import (
    WorkingMemorySnapshot, WorkingPlanStep, WorkingPlanStepStatus,
)


@dataclass(frozen=True, slots=True)
class GoalSlice:
    step_id: str
    description: str
    completion_criteria: str
    implicit: bool
    plan_finished: bool = False

    def to_data(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id, "description": self.description,
            "completion_criteria": self.completion_criteria,
            "implicit": self.implicit,
            "plan_finished": self.plan_finished,
        }


@dataclass(frozen=True, slots=True)
class ActionProgressState:
    signature: str = ""
    relevant_state_hash: str = ""
    consecutive_no_progress: int = 0
    evidence_aware: bool = False
    semantic_signature: str = ""
    semantic_family: str = ""

    @classmethod
    def from_data(cls, data: Mapping[str, Any] | None) -> ActionProgressState:
        if data is None:
            return cls()
        return cls(
            str(data.get("signature", "")),
            str(data.get("relevant_state_hash", "")),
            int(data.get("consecutive_no_progress", 0)),
            bool(data.get("evidence_aware", False)),
            str(data.get("semantic_signature", "")),
            str(data.get("semantic_family", "")),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "relevant_state_hash": self.relevant_state_hash,
            "consecutive_no_progress": self.consecutive_no_progress,
            "evidence_aware": self.evidence_aware,
            "semantic_signature": self.semantic_signature,
            "semantic_family": self.semantic_family,
        }


@dataclass(frozen=True, slots=True)
class PlanGuardDecision:
    goal_slice: GoalSlice
    action_signature: str
    relevant_state_hash: str
    consecutive_no_progress: int
    should_stop: bool
    semantic_signature: str = ""
    semantic_family: str = ""
    semantic_repeat: bool = False


@dataclass(frozen=True, slots=True)
class PlanGuard:
    no_progress_limit: int = 3

    def __post_init__(self) -> None:
        if self.no_progress_limit < 1:
            raise ValueError("no_progress_limit must be positive")

    def current_slice(self, memory: WorkingMemorySnapshot) -> GoalSlice:
        active = [
            step for step in memory.plan
            if step.status is WorkingPlanStepStatus.IN_PROGRESS
        ]
        if len(active) > 1:
            raise ValueError("working plan has more than one IN_PROGRESS step")
        step: WorkingPlanStep | None = active[0] if active else next((
            item for item in memory.plan
            if item.status is WorkingPlanStepStatus.PENDING
        ), None)
        if step is not None:
            return GoalSlice(
                step.step_id, step.description, step.completion_criteria, False
            )
        if memory.plan:
            return GoalSlice(
                "plan-finished", "All explicit plan steps are finished or blocked",
                "Do not start new tool work; report completed and blocked steps "
                "using existing evidence.", False, True,
            )
        return GoalSlice(
            "implicit-goal", memory.goal,
            "Answer or complete the current goal using verified evidence; disclose "
            "anything not verified.", True,
        )

    def relevant_state_hash(
        self, memory: WorkingMemorySnapshot, workspace_fingerprint: str,
    ) -> str:
        return canonical_hash({
            "working_memory_hash": memory.content_hash,
            "workspace_fingerprint": workspace_fingerprint,
            "evidence": [item.reference for item in memory.evidence],
            "goal_slice": self.current_slice(memory).to_data(),
        })

    @staticmethod
    def action_signature(
        call: ToolCall, goal_slice: GoalSlice, relevant_state_hash: str,
    ) -> str:
        return canonical_hash({
            "tool": call.name, "arguments": dict(call.arguments),
            "goal_slice": goal_slice.to_data(),
            "relevant_state_hash": relevant_state_hash,
        })

    def evaluate(
        self, call: ToolCall, memory: WorkingMemorySnapshot,
        workspace_fingerprint: str, previous: ActionProgressState,
        semantic_action: SemanticAction | None = None,
    ) -> PlanGuardDecision:
        goal_slice = self.current_slice(memory)
        state_hash = self.relevant_state_hash(memory, workspace_fingerprint)
        signature = self.action_signature(call, goal_slice, state_hash)
        repeated_without_change = (
            signature == previous.signature
            and state_hash == previous.relevant_state_hash
        )
        semantic_signature = (
            semantic_action.semantic_signature
            if semantic_action is not None else previous.semantic_signature
        )
        semantic_family = (
            semantic_action.family.value
            if semantic_action is not None else previous.semantic_family
        )
        semantic_repeat = (
            previous.evidence_aware
            if semantic_action is None
            else bool(
                semantic_signature
                and semantic_signature == previous.semantic_signature
            )
        )
        count = (
            previous.consecutive_no_progress
            if previous.evidence_aware and semantic_repeat
            else 0
            if previous.evidence_aware
            else previous.consecutive_no_progress + 1
            if repeated_without_change
            else 0
        )
        return PlanGuardDecision(
            goal_slice, signature, state_hash, count,
            goal_slice.plan_finished or count >= self.no_progress_limit,
            semantic_signature, semantic_family, semantic_repeat,
        )
