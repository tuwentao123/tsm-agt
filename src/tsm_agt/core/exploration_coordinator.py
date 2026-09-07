"""Core orchestration for replaceable evidence-guided exploration policies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum

from tsm_agt.ports import (
    EvidenceDelta, EvidenceInventory, EvidenceRelationAssessment,
    EvidenceRelationAssessmentKind, EvidenceRelationPolicyPort,
    EvidenceRelationProviderPort, EvidenceRelationState,
    ExplorationOutcomeDecision, ExplorationOutcomePolicyPort,
    ExplorationOutcomeState, RejectionLoopPolicyPort, RejectionLoopState,
    RejectionRecoveryAction, SemanticAction, ToolCall, ToolResult,
)


class ExplorationCoordinatorAction(StrEnum):
    """Kernel-readable actions independent of any concrete strategy reason."""
    EXECUTE = "EXECUTE"
    RETURN_FEEDBACK = "RETURN_FEEDBACK"
    REWRITE = "REWRITE"
    ALLOW_BOUNDED_PROBE = "ALLOW_BOUNDED_PROBE"
    STOP_ROUTE = "STOP_ROUTE"
    ASK_USER = "ASK_USER"


@dataclass(frozen=True, slots=True)
class ExplorationCoordinatorDecision:
    """Before-call standard action, effective call and checkpoint state."""
    action: ExplorationCoordinatorAction
    reason: str
    assessment: EvidenceRelationAssessment
    call: ToolCall
    relation_state: EvidenceRelationState
    rejection_state: RejectionLoopState
    relation_count: int = 0
    rejection_occurrence: int = 0
    probe_timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ExplorationCoordinatorUpdate:
    """After-result relation, rejection and outcome state transition."""
    relation_state: EvidenceRelationState
    rejection_state: RejectionLoopState
    outcome: ExplorationOutcomeDecision


class ExplorationCoordinator:
    """Connects exploration strategy Ports without owning their decisions.

    Responsibility: collect relation clues, ask singleton policies for decisions,
    translate those decisions to standard Kernel actions, and checkpoint state. It
    does not know Android/iOS/Web conventions, execute tools, or authorize access.
    Inputs are a proposed call/result and redacted state; outputs are a standard
    action and state update. Providers/policies are replaced at Bootstrap without
    changing this class. Every resulting call still passes normal Kernel, Sandbox,
    approval and tool-risk checks, which are the real security boundary.
    """

    def __init__(
        self, providers: tuple[EvidenceRelationProviderPort, ...],
        relation_policy: EvidenceRelationPolicyPort,
        rejection_policy: RejectionLoopPolicyPort,
        outcome_policy: ExplorationOutcomePolicyPort,
    ) -> None:
        self._providers = providers
        self._relation_policy = relation_policy
        self._rejection_policy = rejection_policy
        self._outcome_policy = outcome_policy

    async def before_call(
        self, call: ToolCall, semantic_action: SemanticAction | None,
        relation_state: EvidenceRelationState,
        rejection_state: RejectionLoopState, inventory: EvidenceInventory,
    ) -> ExplorationCoordinatorDecision:
        gathered = []
        for provider in self._providers:
            gathered.extend(await provider.discover(
                call, semantic_action, relation_state, inventory
            ))
        relations = tuple(gathered)
        assessment = await self._relation_policy.assess(
            call, semantic_action, relations, relation_state
        )
        if assessment.kind is EvidenceRelationAssessmentKind.SUPPORTED:
            return ExplorationCoordinatorDecision(
                ExplorationCoordinatorAction.EXECUTE, assessment.reason,
                assessment, call, relation_state, rejection_state, len(relations),
            )
        identity = self._rejection_identity(call, semantic_action, assessment)
        recovery = await self._rejection_policy.decide(
            call, assessment, identity, rejection_state
        )
        mapped = {
            RejectionRecoveryAction.RETURN_FEEDBACK:
                ExplorationCoordinatorAction.RETURN_FEEDBACK,
            RejectionRecoveryAction.REWRITE: ExplorationCoordinatorAction.REWRITE,
            RejectionRecoveryAction.ALLOW_BOUNDED_PROBE:
                ExplorationCoordinatorAction.ALLOW_BOUNDED_PROBE,
            RejectionRecoveryAction.STOP_ROUTE:
                ExplorationCoordinatorAction.STOP_ROUTE,
            RejectionRecoveryAction.ASK_USER:
                ExplorationCoordinatorAction.ASK_USER,
        }[recovery.action]
        effective_call = (
            self._bounded_probe(call)
            if recovery.action in {
                RejectionRecoveryAction.ALLOW_BOUNDED_PROBE,
                RejectionRecoveryAction.REWRITE,
            }
            else call
        )
        return ExplorationCoordinatorDecision(
            mapped, recovery.reason, assessment, effective_call, relation_state,
            recovery.state, len(relations), recovery.occurrence,
            10.0 if recovery.action is RejectionRecoveryAction.ALLOW_BOUNDED_PROBE
            else None,
        )

    async def after_result(
        self, call: ToolCall, result: ToolResult,
        semantic_action: SemanticAction | None, evidence_delta: EvidenceDelta | None,
        relation_state: EvidenceRelationState,
        rejection_state: RejectionLoopState,
        outcome_state: ExplorationOutcomeState,
    ) -> ExplorationCoordinatorUpdate:
        questions = list(relation_state.question_ids)
        question_id = (
            call.evidence_question.question_id.strip()
            if call.evidence_question is not None else ""
        )
        if question_id and question_id not in questions:
            questions.append(question_id)
        targets = list(relation_state.target_hashes)
        if semantic_action is not None and semantic_action.target_hash not in targets:
            targets.append(semantic_action.target_hash)
        next_relations = EvidenceRelationState(
            tuple(questions[-200:]), tuple(targets[-500:]),
            relation_state.successful_calls + int(result.ok), 1,
        )
        next_rejections = (
            RejectionLoopState({}, 1)
            if evidence_delta is not None and evidence_delta.has_progress
            else rejection_state
        )
        outcome = await self._outcome_policy.evaluate(
            call, result, evidence_delta, outcome_state
        )
        return ExplorationCoordinatorUpdate(
            next_relations, next_rejections, outcome
        )

    @staticmethod
    def _bounded_probe(call: ToolCall) -> ToolCall:
        """Reduce result volume only; never broaden path or authority."""
        arguments = dict(call.arguments)
        bounds = {
            "core.search_text": ("max_matches", 50),
            "core.find_files": ("limit", 50),
            "core.list_files": ("limit", 100),
            "core.read_file": ("max_lines", 200),
        }
        bound = bounds.get(call.name)
        if bound is not None:
            key, maximum = bound
            current = arguments.get(key)
            arguments[key] = min(current, maximum) if isinstance(current, int) else maximum
        return replace(call, arguments=arguments)

    @staticmethod
    def _rejection_identity(
        call: ToolCall, semantic_action: SemanticAction | None,
        assessment: EvidenceRelationAssessment,
    ) -> str:
        question_id = (
            call.evidence_question.question_id
            if call.evidence_question is not None else ""
        )
        payload = {
            "question_id": question_id, "tool": call.name,
            "semantic_signature": (
                semantic_action.semantic_signature if semantic_action else ""
            ),
            "reason": assessment.reason,
        }
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
