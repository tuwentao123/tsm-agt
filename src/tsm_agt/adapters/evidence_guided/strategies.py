"""Default evidence-guided exploration strategies."""

from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, EvidenceInventory, EvidenceRelation,
    EvidenceRelationAssessment, EvidenceRelationAssessmentKind,
    EvidenceRelationKind, EvidenceRelationState, ExplorationOutcomeAction,
    ExplorationOutcomeDecision, ExplorationOutcomeState, HealthState,
    HealthStatus, RejectionLoopState, RejectionRecoveryAction,
    RejectionRecoveryDecision, SemanticAction, ToolCall, ToolResult,
)


class _Lifecycle:
    """Shared lifecycle plumbing; concrete classes still own only decisions."""

    _started: bool

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "exploration strategy ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("exploration strategy is not started")


class BuiltinEvidenceRelationProvider(_Lifecycle):
    """Finds generic continuity clues between the next call and prior evidence.

    Responsibility: compare question IDs and semantic target hashes. It does not
    parse Android/iOS/Web layouts, choose whether a tool may run, or grant access.
    Input is a proposed call plus checkpoint state; output is relation metadata.
    Multiple providers can be registered or this one can be removed in Bootstrap.
    Kernel/Sandbox/approval remain the security boundary.
    """

    descriptor = AdapterDescriptor(
        "builtin.generic-evidence-relations", "1.0.0",
        "EvidenceRelationProviderPort", "1.0",
        frozenset({"project-neutral", "multi-provider", "no-authority"}),
    )

    async def discover(
        self, call: ToolCall, semantic_action: SemanticAction | None,
        state: EvidenceRelationState, inventory: EvidenceInventory,
    ) -> tuple[EvidenceRelation, ...]:
        self._require_started()
        relations: list[EvidenceRelation] = []
        question_id = (
            call.evidence_question.question_id.strip()
            if call.evidence_question is not None else ""
        )
        if question_id and question_id in state.question_ids:
            relations.append(EvidenceRelation(
                EvidenceRelationKind.SAME_QUESTION,
                self.descriptor.adapter_id, question_id, 1.0,
            ))
        elif question_id:
            relations.append(EvidenceRelation(
                EvidenceRelationKind.NEW_QUESTION,
                self.descriptor.adapter_id, question_id, 1.0,
            ))
        if (
            semantic_action is not None
            and semantic_action.target_hash in state.target_hashes
        ):
            relations.append(EvidenceRelation(
                EvidenceRelationKind.SAME_TARGET, self.descriptor.adapter_id,
                semantic_action.target_hash, semantic_action.confidence,
            ))
        if inventory.fingerprints:
            relations.append(EvidenceRelation(
                EvidenceRelationKind.KNOWN_EVIDENCE, self.descriptor.adapter_id,
                confidence=0.8,
            ))
        return tuple(relations)


class RuleBasedEvidenceRelationPolicy(_Lifecycle):
    """Turns generic relation clues into one soft exploration decision.

    Responsibility: allow a first route, a same-question follow-up, or a new user
    question, and request a bounded probe when continuity is unknown. It does not
    know framework directory conventions and cannot grant permissions. Input is
    provider relations; output is supported/unsupported/probe. Exactly one such
    policy is selected by the Bootstrap exploration profile.
    """

    descriptor = AdapterDescriptor(
        "builtin.rule-based-evidence-relation", "1.0.0",
        "EvidenceRelationPolicyPort", "1.0",
        frozenset({"project-neutral", "replaceable", "no-authority"}),
    )

    async def assess(
        self, call: ToolCall, semantic_action: SemanticAction | None,
        relations: tuple[EvidenceRelation, ...], state: EvidenceRelationState,
    ) -> EvidenceRelationAssessment:
        self._require_started()
        kinds = {item.kind for item in relations}
        if state.successful_calls == 0:
            return EvidenceRelationAssessment(
                EvidenceRelationAssessmentKind.SUPPORTED, "first_evidence_step",
                "FIRST",
            )
        for kind, reason in (
            (EvidenceRelationKind.SAME_QUESTION, "same_evidence_question"),
            (EvidenceRelationKind.SAME_TARGET, "same_semantic_target"),
        ):
            if kind in kinds:
                return EvidenceRelationAssessment(
                    EvidenceRelationAssessmentKind.SUPPORTED, reason, kind.value,
                )
        if EvidenceRelationKind.NEW_QUESTION in kinds:
            return EvidenceRelationAssessment(
                EvidenceRelationAssessmentKind.NEEDS_BOUNDED_PROBE,
                "new_question_needs_bounded_probe",
                EvidenceRelationKind.NEW_QUESTION.value,
            )
        if EvidenceRelationKind.KNOWN_EVIDENCE in kinds:
            return EvidenceRelationAssessment(
                EvidenceRelationAssessmentKind.NEEDS_BOUNDED_PROBE,
                "evidence_exists_but_relation_is_unknown",
                EvidenceRelationKind.KNOWN_EVIDENCE.value,
            )
        return EvidenceRelationAssessment(
            EvidenceRelationAssessmentKind.NEEDS_BOUNDED_PROBE,
            "no_known_relation", EvidenceRelationKind.UNKNOWN.value,
        )


class BoundedRejectionLoopPolicy(_Lifecycle):
    """Prevents a model from retrying the same soft-policy rejection forever.

    Responsibility: first return actionable feedback, then permit one bounded
    read-only probe, then stop that route. It does not rewrite paths, execute tools,
    or override security. Input is a hashed rejection identity; output is a recovery
    action and checkpoint state. Replace this singleton in Bootstrap to change the
    retry behavior without editing Kernel.
    """

    descriptor = AdapterDescriptor(
        "builtin.bounded-rejection-loop", "1.0.0",
        "RejectionLoopPolicyPort", "1.0",
        frozenset({"checkpointed", "bounded-probe", "no-authority"}),
    )

    async def decide(
        self, call: ToolCall, assessment: EvidenceRelationAssessment,
        rejection_identity: str, state: RejectionLoopState,
    ) -> RejectionRecoveryDecision:
        self._require_started()
        occurrence = int(state.rejection_counts.get(rejection_identity, 0)) + 1
        counts = dict(state.rejection_counts)
        counts[rejection_identity] = occurrence
        next_state = RejectionLoopState(counts, 1)
        if assessment.kind is EvidenceRelationAssessmentKind.NEEDS_BOUNDED_PROBE:
            if occurrence == 1:
                action = RejectionRecoveryAction.ALLOW_BOUNDED_PROBE
                reason = "unknown_relation_bounded_probe"
            else:
                action = RejectionRecoveryAction.STOP_ROUTE
                reason = "bounded_probe_already_used"
        elif occurrence == 1:
            action = RejectionRecoveryAction.RETURN_FEEDBACK
            reason = "first_soft_rejection"
        elif occurrence == 2:
            action = RejectionRecoveryAction.ALLOW_BOUNDED_PROBE
            reason = "second_soft_rejection_bounded_probe"
        else:
            action = RejectionRecoveryAction.STOP_ROUTE
            reason = "repeated_soft_rejection"
        return RejectionRecoveryDecision(
            action, reason, rejection_identity, occurrence, next_state
        )


class RuleBasedExplorationOutcomePolicy(_Lifecycle):
    """Suggests what to do after a tool result using evidence progress only.

    Responsibility: continue on progress, change method after two empty results,
    and stop the route after three. It does not decide Task completion, run tools,
    or infer project type. Input is the result and optional Evidence Delta; output
    is a standard next-route action with checkpoint state. The singleton is
    replaceable through Bootstrap registration and cannot alter security authority.
    """

    descriptor = AdapterDescriptor(
        "builtin.rule-based-exploration-outcome", "1.0.0",
        "ExplorationOutcomePolicyPort", "1.0",
        frozenset({"project-neutral", "checkpointed", "no-authority"}),
    )

    async def evaluate(
        self, call: ToolCall, result: ToolResult, evidence_delta,
        state: ExplorationOutcomeState,
    ) -> ExplorationOutcomeDecision:
        self._require_started()
        if (
            not result.ok
            and bool(result.meta.get("recoverable_input"))
        ):
            return ExplorationOutcomeDecision(
                ExplorationOutcomeAction.CONTINUE,
                "recoverable_path_context", state,
            )
        has_progress = result.ok and (
            evidence_delta is None or evidence_delta.has_progress
        )
        count = 0 if has_progress else state.consecutive_no_evidence + 1
        if count >= 3:
            action = ExplorationOutcomeAction.STOP_ROUTE
            reason = "three_results_without_new_evidence"
        elif count >= 2:
            action = ExplorationOutcomeAction.CHANGE_METHOD
            reason = "two_results_without_new_evidence"
        else:
            action = ExplorationOutcomeAction.CONTINUE
            reason = "evidence_progress" if has_progress else "observe_once_more"
        return ExplorationOutcomeDecision(
            action, reason, ExplorationOutcomeState(count, action.value)
        )
