"""Microkernel task, bounded Agent loop, and tool execution boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import shlex
import time
from contextlib import AsyncExitStack, asynccontextmanager
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from tsm_agt.ports import (
    ProcessEnvironmentPolicy,
    CrossProcessLockPort,
    EvidenceDeltaEvaluatorPort,
    EvidenceDelta,
    EvidenceInventory,
    SemanticAction,
    SemanticActionClassifierPort,
    ReadHitsAction,
    ReadHitsDecision,
    ReadHitsPolicyPort,
    ReadHitsState,
    ArtifactReadAction,
    ArtifactReadDecision,
    ArtifactReadPolicyPort,
    ArtifactReadProbe,
    ArtifactReadState,
    ProgressiveScopeAction,
    ProgressiveScopeDecision,
    ProgressiveScopePolicyPort,
    ProgressiveScopeState,
    ScopeProbe,
    ExplorationBudgetAction,
    ExplorationBudgetDecision,
    ExplorationBudgetObservation,
    ExplorationBudgetPolicyPort,
    ExplorationBudgetProbe,
    ExplorationBudgetState,
    ExplorationBudgetUpdate,
    StopOrPivotAction,
    StopOrPivotDecision,
    StopOrPivotPolicyPort,
    StopOrPivotSignals,
    StopOrPivotState,
    AgentProgressProjection,
    AgentProgressProjectorPort,
    AgentProgressSignals,
    EvidenceLevelEvaluatorPort,
    RecoverableToolProtocolError,
    EvidenceLevelSignals,
    EVIDENCE_CATEGORIES,
    InvestigationStatusProjection,
    InvestigationStatusProjectorPort,
    InvestigationStatusSignals,
    ToolArgumentPresentation, ToolArgumentPresenterPort,
    InvestigationFlowProjectorPort,
    FinishReason,
    HealthState,
    HealthStatus,
    ImageBlock,
    Message,
    MessageBlock,
    MessageRole,
    ModelProviderPort,
    ModelRequest,
    ModelResponse,
    ConclusionProtocolMode,
    ModelStreamCompleted,
    ModelTextDelta,
    ModelTransportProgress,
    ModelAttemptFailure, ModelFailureCategory, ModelRecoveryAction,
    ModelRecoveryExhausted, ModelRecoveryPolicyPort, ModelRecoveryProbe,
    ModelRetrySafety,
    StreamingModelProviderPort,
    ModelUsage,
    BackgroundProcessStartResult,
    ProcessCommandResult,
    ProcessExecutorPort,
    ProcessExitStatus,
    ProcessLogs,
    ProcessResult,
    ProcessStartRequest,
    process_result_succeeded,
    RuntimeEvent,
    RuntimeAdapter,
    RuntimeStorePort,
    StoredProjectTrust,
    StoredProjectOnboarding,
    RuntimeUnitOfWork,
    SessionEvent, SessionUnitOfWork, SessionTaskUnitOfWork,
    SandboxRequest,
    SandboxPort,
    TextBlock,
    ToolCall,
    OutcomeBindingMode,
    ToolCallBlock,
    ToolEffect,
    ToolIdempotency,
    ToolInvocationContext,
    ToolMemoryControl,
    ToolWorkingMemoryControl,
    ToolTaskSpecControl,
    ToolProcessControl,
    ToolWorkspaceControl,
    ToolProviderPort,
    ToolResult,
    ToolResultBlock,
    ToolRecoveryKind,
    ToolRisk,
    ToolSpec,
    WorkspaceFilesystemPort,
    WorkspacePathPort,
    is_sensitive_read_path,
    LocalIdentityPort,
    ProjectMemoryPort,
    SessionInputRelation,
    SessionInputRelationJudgement,
    SessionInputRelationPort,
    SessionInputResolverPort,
    TaskSpecPlannerPort,
    JudgeVerdict,
    RubricEvidence,
    RubricJudgePort,
    RubricJudgement,
    CheckpointCompatibilityAction,
    CheckpointCompatibilityDecision,
    CheckpointCompatibilityPolicyPort,
    CheckpointCompatibilityProbe,
    EvidenceRelationProviderPort,
    EvidenceRelationPolicyPort,
    EvidenceRelationState,
    RejectionLoopPolicyPort,
    RejectionLoopState,
    ExplorationOutcomePolicyPort,
    ExplorationOutcomeState,
    ExplorationOutcomeAction,
    ToolScopeConsistencyAction,
    ToolScopeConsistencyDecision,
    ToolScopeConsistencyPolicyPort,
    ToolScopeConsistencyProbe,
    ToolScopeRelation,
    CompletionGap,
    CompletionReadinessAction,
    CompletionReadinessDecision,
    CompletionReadinessMode,
    CompletionReadinessPolicyPort,
    CompletionReadinessProbe,
    CompletionReadinessState,
    FinalAcceptanceAction,
    FinalAcceptanceDecision,
    FinalAcceptancePolicyPort,
    FinalAcceptanceProbe,
    FinalAcceptanceViolation,
    FinalQuestionEvidence,
    AssistantConclusion,
    ConclusionBlock,
    ConclusionReferenceValidation,
    ConclusionValidationStatus,
)

from .agent_loop import (
    AgentProgress,
    AgentProgressKind,
    AgentLoopLimitExceeded,
    AgentCheckpointConflict,
    AgentTurnCheckpoint,
    ToolBatchSnapshot, ToolBatchStatus,
    AgentTurnResult,
    AgentTurnSuspended,
    PhaseAction,
    PhaseVerdict,
    TurnState,
    AgentClarificationSuspended,
    AgentContinuationSuspended,
    ProviderCapabilityMismatch,
)
from .approval import (
    ApprovalDecision,
    ApprovalKind,
    ApprovalNotPending,
    ApprovalPayloadMismatch,
    ApprovalRequest,
    ApprovalRequired,
    utc_now,
)
from .workspace_access import WorkspaceAccessCapability, WorkspaceAccessGrant
from .clarification import (
    ClarificationChoice, ClarificationKind, ClarificationNotPending, ClarificationRequest,
    ClarificationRequired, ClarificationTokenMismatch,
)
from .cancellation import (
    CancellationScope,
    TaskCancellationSignal,
    note_cancel_requested,
    note_interrupting_settled,
    note_scope_closed,
    note_scope_opened,
)
from .execution import (
    IdempotencyConflict,
    ToolCommitState,
    ToolExecutionInProgress,
    ToolExecutionRecord,
)
from .execution_facts import (
    EffectStatus,
    FactResolution,
    classify_effect_status,
    project_execution_facts,
    project_unresolved_failures,
)
from .flow import FlowProjection, FlowProjector
from .replay import (
    FlowReplay, FlowReplayBoundary, FlowReplayIndex, FlowReplayPlayback,
    FlowReplaySnapshot, FlowReplaySpeed,
)
from .policy import CoreToolPolicy
from .configuration import (
    EffectiveConfigurationSnapshot, canonical_hash, effective_toolset_hash,
)
from .context import ContextWindowExceeded, ContextWindowManager
from .conclusion_reference_validator import ConclusionReferenceValidator
from .document_references import extract_document_references
from .prompt import PromptAssemblyReceipt, PromptTemplate
from .memory import MemoryRecord, MemoryScope, MemorySourceKind, MemoryView
from .onboarding import (
    ONBOARDING_PHASES, OnboardingCheckpoint, ProjectOnboardingScanner,
    ProjectOnboardingSnapshot,
)
from .project_instructions import (
    ProjectInstructionsSnapshot, load_project_instructions,
)
from .process import (
    BackgroundProcessRecord,
    BackgroundProcessState,
    ProcessSandboxDenied,
)
from .kernel_tool import (
    ToolMixin,
)
from .kernel_workspace import (
    WorkspaceMixin,
)
from .kernel_process import (
    ProcessMixin,
)
from .kernel_projection import (
    ProjectionMixin,
)
from .kernel_onboarding import (
    OnboardingMixin,
)
from .kernel_progress import (
    ProgressMixin,
)
from .kernel_approval import (
    ApprovalMixin,
)
from .kernel_memory import (
    MemoryMixin,
)
from .kernel_task import (
    TaskMixin,
)
from .kernel_policy import (
    PolicyMixin,
)
from .kernel_model import (
    ModelMixin,
)
from .kernel_input import (
    InputMixin,
)
from .kernel_steering import (
    SteeringMixin,
)
from .kernel_checkpoint import (
    CheckpointMixin,
)
from .task import LEGAL_TRANSITIONS, SUCCESS_PATH, TaskNotFound, TaskSnapshot, TaskState
from .verification_command import is_verification_command, is_verification_request
from .task_spec import (
    TaskAcceptanceCriterion, TaskContinuationMode, TaskCriterionKind,
    OutcomeBindingAction, OutcomeBindingDecision, OutcomeBindingReason,
    TaskExecutionFocus, TaskExecutionFocusProjector,
    TaskOutcomeCompletionPolicy, TaskOutcomeEligibilityCalculator,
    TaskOutcomeKind, TaskOutcomeProposal, TaskOutcomeSnapshot,
    TaskOutcomeStatus,
    TaskSpecProjector, TaskSpecProposal, TaskSpecSnapshot,
    REQUIRED_EFFECT_CRITERION_ID, RUNTIME_AUTHORED_CRITERION_KINDS,
    validate_authored_criterion,
)
from .evidence_coverage import (
    ESCALATED_GAP_KINDS, ProofEnforcementMode,
)
from .semantic_review import normalize_judgement
from .session import (
    SessionChoiceOption, SessionInteractionKind, SessionInteractionRequest,
    SessionSnapshot, SessionState, standalone_session_id,
)
from .session_interaction import (
    DeterministicSessionChoiceResolver, SessionChoiceAction,
    SessionChoiceDecision,
)
from .session_context import (
    SessionActiveCheckpoint, SessionContextProjector,
    SessionConversationProjection, SessionPromptProjection, SessionWorkingState,
    ContextAuthority, ContextScope,
)
from .working_memory import (
    EffectiveWorkingMemory, EffectiveWorkingMemoryProjector,
    WorkingMemoryProjector, WorkingMemorySnapshot, WorkingPlanStepStatus,
)
from .steering import SteeringKind, SteeringProjection, SteeringProjector
from .runtime_input import (
    ApprovalResolutionInput, CancelTaskInput, ClarificationReplyInput,
    InterruptTaskInput, QueuedFollowUp, RuntimeInputContext,
    RuntimeInputEvent, RuntimeInputIntent, RuntimeInputRoute, RuntimeInputRouter,
    RuntimeTextInput, SessionTextInput, SessionContinuationDecision,
    SessionContinuationMode,
    SessionResumeCandidate, SessionResumeSafety, SessionInputAction,
    SessionInputDecision, SessionInputGrounding, SessionRouteDisposition,
    SessionTaskCatalogEntry, SessionTaskRelation,
)
from .kernel_session import (
    SessionMixin,
)
from .session_handoff import (
    build_session_follow_up_goal, clip_text, find_background_leak,
)
from .exploration_coordinator import (
    ExplorationCoordinator, ExplorationCoordinatorAction,
)
from .plan_guard import ActionProgressState, PlanGuard
from .trust import (
    ProjectTrustBinding, ProjectTrustLevel, new_trust_binding,
    workspace_fingerprint,
)
from .workspace import (
    MutationOperation, MutationRecord, WorkspaceChangeSet,
    WorkspaceMutationConflict, WorkspacePatchConflict,
    WorkspaceTransactionEntry,
    WorkspaceTransactionManifest,
    capture_workspace_baseline, commit_prepared_workspace_deletion,
    commit_prepared_workspace_mutation, compare_workspace_baseline,
    prepare_workspace_bytes_write, prepare_workspace_file_delete,
    prepare_workspace_text_write, read_mutation_backup, file_sha256,
    read_workspace_transaction_manifests, remove_workspace_transaction_manifest,
    restore_prepared_workspace_deletion, restore_prepared_workspace_mutation,
    workspace_mutation_lock_file, workspace_mutation_lock_key,
    write_mutation_backup,
    write_workspace_transaction_manifest,
)
from .verification import (
    AcceptanceResult, AcceptanceStatus, Evidence, TaskVerificationResult,
    terminal_state_for,
)
from .verification_criteria import (
    seed_acceptance_criteria,
)
from .evidence_question import (
    EvidenceQuestionProjector, EvidenceQuestionProjection,
    EvidenceQuestionStatus, ToolActionDisposition,
)
from .session_resources import (
    SessionQuestionReference, SessionResourceKind, SessionResourceReference,
)
from .turn import (
    InvalidModelResponse, InvalidTurnState, ModelInvocationFailed,
    SessionAnswerRequiresTask, TurnResult,
)
from .tool import (
    DuplicateToolName,
    InvalidToolArguments,
    InvalidToolResult,
    ToolNotFound,
    validate_tool_arguments,
)

# The one Tool a direct Session answer may call. It grants no capability and
# only reports that the ANSWER route cannot serve this message.


#: Runtime input event type -> the handler that owns it. A table rather than an
#: if/elif ladder, so a new input kind is a row and cannot silently fall through
#: to a neighbouring branch (Hermes ``AGENTS.md:249``). The lookup walks
#: ``type(event).__mro__``, which keeps ``isinstance`` semantics for a subclass
#: of a registered event.

@dataclass(frozen=True, slots=True)
class KernelDependencies:
    model: ModelProviderPort
    store: RuntimeStorePort
    sandbox: SandboxPort
    cross_process_lock: CrossProcessLockPort
    workspace_filesystem: WorkspaceFilesystemPort
    workspace_path: WorkspacePathPort
    local_identity: LocalIdentityPort
    project_memory: ProjectMemoryPort
    model_recovery_policy: ModelRecoveryPolicyPort | None = None
    evidence_delta_evaluator: EvidenceDeltaEvaluatorPort | None = None
    semantic_action_classifier: SemanticActionClassifierPort | None = None
    read_hits_policy: ReadHitsPolicyPort | None = None
    artifact_read_policy: ArtifactReadPolicyPort | None = None
    progressive_scope_policy: ProgressiveScopePolicyPort | None = None
    tool_scope_consistency_policy: ToolScopeConsistencyPolicyPort | None = None
    completion_readiness_policy: CompletionReadinessPolicyPort | None = None
    completion_readiness_mode: CompletionReadinessMode = (
        CompletionReadinessMode.LEGACY_GATE
    )
    #: How strongly the proof layer participates in the Task decision.
    #: ``OBSERVE`` (the only mode P0/P1 use) records without intercepting.
    proof_enforcement_mode: ProofEnforcementMode = ProofEnforcementMode.OBSERVE
    final_acceptance_policy: FinalAcceptancePolicyPort | None = None
    exploration_budget_policy: ExplorationBudgetPolicyPort | None = None
    stop_or_pivot_policy: StopOrPivotPolicyPort | None = None
    evidence_relation_providers: tuple[EvidenceRelationProviderPort, ...] = ()
    evidence_relation_policy: EvidenceRelationPolicyPort | None = None
    rejection_loop_policy: RejectionLoopPolicyPort | None = None
    exploration_outcome_policy: ExplorationOutcomePolicyPort | None = None
    agent_progress_projector: AgentProgressProjectorPort | None = None
    tool_argument_presenter: ToolArgumentPresenterPort | None = None
    evidence_level_evaluator: EvidenceLevelEvaluatorPort | None = None
    investigation_status_projector: (
        InvestigationStatusProjectorPort | None
    ) = None
    investigation_flow_projector: InvestigationFlowProjectorPort | None = None
    runtime_input_classifier: RuntimeInputClassifierPort | None = None
    session_input_relation: SessionInputRelationPort | None = None
    session_input_resolver: SessionInputResolverPort | None = None
    task_spec_planner: TaskSpecPlannerPort | None = None
    #: Bounded judge for `rubric` acceptance criteria. When absent, rubric
    #: criteria are recorded but never block (their P0 behaviour).
    rubric_judge: RubricJudgePort | None = None
    rubric_judge_max_attempts: int = 2
    #: INV-10 bound: how much proof a judge may read. A judge that only receives
    #: opaque references cannot decide anything, so the Runtime resolves them
    #: into bounded excerpts.
    rubric_evidence_max_items: int = 4
    rubric_evidence_excerpt_characters: int = 800
    checkpoint_compatibility_policy: (
        CheckpointCompatibilityPolicyPort | None
    ) = None
    process_executor: ProcessExecutorPort | None = None
    tools: tuple[ToolProviderPort, ...] = ()
    runtime_adapters: tuple[RuntimeAdapter, ...] = ()
    configuration_metadata: Mapping[str, Any] = field(default_factory=dict)
    #: Hermes allows 90 iterations per parent turn (50 per subagent) and Codex
    #: has no ceiling at all, relying on compaction. 40 turned ordinary
    #: engineering work into ``max_model_calls`` failures, which the user could
    #: only answer by continuing -- and each continuation re-read context.
    default_max_model_calls: int = 90
    default_max_tool_calls: int = 120
    default_max_output_tokens: int = 1024
    #: INV-4 bound: how many consecutive no-progress continuations are allowed
    #: before an unclosable required gap is finalized as NEEDS_REVIEW instead of
    #: suspending again. Keep aligned with the readiness policy's own
    #: ``max_stalled_continuations`` so the two thresholds never drift.
    completion_max_stalled_continuations: int = 2
    finalization_model_calls: int = 2
    execution_reserve_model_calls: int = 1
    recovery_reserve_model_calls: int = 1
    verification_reserve_model_calls: int = 1
    prompt_template: PromptTemplate = field(default_factory=PromptTemplate.default)
    context_manager: ContextWindowManager = field(
        default_factory=ContextWindowManager
    )
    session_context_projector: SessionContextProjector = field(
        default_factory=SessionContextProjector
    )
    working_memory_projector: WorkingMemoryProjector = field(
        default_factory=WorkingMemoryProjector
    )
    effective_working_memory_projector: EffectiveWorkingMemoryProjector = field(
        default_factory=EffectiveWorkingMemoryProjector
    )
    require_evidence_questions: bool = True


#: Who may drive a Task into each terminal state. "runtime" enforces limits and
#: verification, "user" is an explicit cancel, and "model" may only *propose* an
#: outcome by finishing a Turn (it never writes a terminal state directly).
#: INV: a terminal state must be reachable only by an authorised actor.


@dataclass(slots=True)
class _PathLockEntry:
    lock: asyncio.Lock
    users: int = 0

class _WorkspacePathLockCoordinator:
    """Serialize one canonical path across Tasks, Kernels, and local processes."""

    def __init__(
        self, cross_process_lock: CrossProcessLockPort,
        workspace_path: WorkspacePathPort,
        workspace_filesystem: WorkspaceFilesystemPort,
    ) -> None:
        self._guard = asyncio.Lock()
        self._entries: dict[str, _PathLockEntry] = {}
        self._cross_process_lock = cross_process_lock
        self._workspace_path = workspace_path
        self._workspace_filesystem = workspace_filesystem

    @asynccontextmanager
    async def hold(self, workspace: Path, relative_path: str):
        key = workspace_mutation_lock_key(
            workspace, relative_path, self._workspace_path
        )
        async with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _PathLockEntry(asyncio.Lock())
                self._entries[key] = entry
            entry.users += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            lock_file = workspace_mutation_lock_file(
                workspace, key, self._workspace_path,
                self._workspace_filesystem,
            )
            lease_id: str | None = None
            try:
                lease_id = await self._cross_process_lock.acquire(lock_file)
                yield key
            finally:
                if lease_id is not None:
                    await self._cross_process_lock.release(lease_id)
        finally:
            if acquired:
                entry.lock.release()
            async with self._guard:
                entry.users -= 1
                if entry.users == 0 and not entry.lock.locked():
                    self._entries.pop(key, None)

    @asynccontextmanager
    async def hold_many(self, workspace: Path, relative_paths: tuple[str, ...]):
        """Acquire distinct path locks in one deterministic global order."""
        keyed = tuple(
            (workspace_mutation_lock_key(workspace, path, self._workspace_path), path)
            for path in relative_paths
        )
        keys = tuple(key for key, _path in keyed)
        if len(set(keys)) != len(keys):
            raise ValueError("batch mutation paths must resolve to distinct files")
        async with AsyncExitStack() as stack:
            for _key, path in sorted(keyed):
                await stack.enter_async_context(self.hold(workspace, path))
            yield tuple(sorted(keys))





def _task_spec_requires_command_verification(spec: TaskSpecSnapshot) -> bool:
    """Derive command verification solely from the declared Task contract."""
    return any(
        criterion.verification_kind is TaskCriterionKind.POST_MUTATION_COMMAND
        for criterion in spec.acceptance_criteria
    )


#: Effects whose delivery is a performed side effect. ``observe`` and
#: ``interact`` are deliberately excluded: observation is governed by the
#: evidence criteria, and asking the user is closed by the interaction flow.
#: Neither is a claim that work was carried out, so neither can be faked by
#: prose and neither belongs in a delivery assertion.
_DELIVERABLE_EFFECTS: frozenset[ToolEffect] = frozenset({
    ToolEffect.MUTATE, ToolEffect.EXECUTE,
})


def _required_effect_deliveries(spec: TaskSpecSnapshot) -> frozenset[ToolEffect]:
    """Side effects the contract requires the Task to leave a record of.

    Keyed strictly on a required Outcome's declared effects. A bare
    ``workspace_integrity`` criterion does NOT imply a required change -- with
    an empty Mutation Journal it is trivially satisfied -- so it must not turn
    an answer-only Task into a blocked one.
    """
    return _required_effects_from_outcomes(spec.outcomes)


def _required_effects_from_outcomes(
    outcomes: Sequence[TaskOutcomeProposal | TaskOutcomeSnapshot],
) -> frozenset[ToolEffect]:
    """Same derivation, usable before a snapshot exists (Planner proposals)."""
    return frozenset(
        effect
        for outcome in outcomes
        if outcome.required
        for effect in outcome.required_effects
        if effect in _DELIVERABLE_EFFECTS
    )


def _recorded_effect_deliveries(task: TaskSnapshot) -> frozenset[ToolEffect]:
    """Side effects that left a durable record on this Task.

    Delivery asserts that the action *was performed*, not that it succeeded: a
    command that ran and exited non-zero is still an execution, and whether its
    result is good enough is the contract's own command criteria to judge.
    """
    recorded = set()
    if _has_active_workspace_mutation(task):
        recorded.add(ToolEffect.MUTATE)
    if any(
        execution.effect is ToolEffect.EXECUTE
        and execution.state is ToolCommitState.COMMITTED
        and execution.result is not None and execution.result.ok
        for execution in task.tool_executions.values()
    ):
        recorded.add(ToolEffect.EXECUTE)
    return frozenset(recorded)


#: Kinds INV-3 must never demote because a bounded, explicit escape exists
#: instead of a vacuous success: a judged gap is closed by the judge, and an
#: effect failure the producer declared unfixable is escalated by the readiness
#: gate to EXHAUSTED -- a resumable stop the stall cap turns into human review.
#: Demoting the latter would let a Task whose required side effect never
#: happened report success.
_JUDGED_GAP_KINDS = frozenset({
    "TASK_SPEC_RUBRIC", "EVIDENCE_QUESTION",
    "JUDGED_CRITERION_WITHOUT_JUDGE", "UNRESOLVED_EFFECT_FAILURE",
})

#: Coverage gaps are exempt for a *different* reason -- the coverage layer owes a
#: bounded escalation to human review -- so they live in their own set
#: (SPEC §12.6 D3). Merging the two would let one reason's evolution silently
#: change the other's behaviour.
_EXEMPT_GAP_KINDS = _JUDGED_GAP_KINDS | ESCALATED_GAP_KINDS


def _gap_has_contract_source(gap: CompletionGap) -> bool:
    """Whether a readiness gap names the contract/ledger fact it came from.

    INV-3 may drop a required gap that nothing can close, but *what* it may drop
    matters: a gap with no provenance is bookkeeping the Runtime created for
    itself, while a gap that names a contract entry (or a durable ``event:`` /
    ``tool_call:`` / ``mutation:`` reference) is a requirement somebody actually
    made. Demoting the latter would turn "we cannot prove it yet" into "we never
    needed it" -- the silent-requirement-loss hole AG-9 closes.
    """

    return bool((gap.evidence_reference or "").strip())


def _readiness_event_payload(
    decision: CompletionReadinessDecision,
    probe: CompletionReadinessProbe,
    *,
    turn_id: str,
    mode: CompletionReadinessMode,
    forced_wrap_up: bool,
) -> dict[str, Any]:
    """Durable payload for ``completion.readiness_evaluated``.

    SPEC §3.5 rule 6: the coverage layer gets no readiness event of its own --
    it identifies its contribution by id on this one. The ids stay empty unless
    the proof gate is enforcing, so OBSERVE-mode and historical Task events
    remain comparable, and a reader can tell "a real requirement the Runtime
    could not prove" apart from "an ordinary unfinished step".
    """

    return {
        "turn_id": turn_id, "action": decision.action.value,
        "raw_action": decision.action.value,
        "reason": decision.reason,
        "mode": mode.value,
        "enforced": mode is CompletionReadinessMode.LEGACY_GATE,
        "forced_wrap_up": forced_wrap_up,
        "remaining_model_calls": probe.remaining_model_calls,
        "remaining_tool_calls": probe.remaining_tool_calls,
        "available_effects": sorted(
            effect.value for effect in probe.available_effects
        ),
        "gap_count": len(decision.gaps),
        "coverage_gap_ids": [
            gap.gap_id for gap in decision.gaps
            if gap.kind in ESCALATED_GAP_KINDS
        ],
        "gaps": [gap.to_data() for gap in decision.gaps],
        "state": decision.state.to_data(),
    }


def _apply_inv3_demotion(
    gaps: Sequence[CompletionGap],
) -> tuple[list[CompletionGap], list[CompletionGap]]:
    """Enforce INV-3 and report what was dropped.

    Returns ``(gaps, violations)`` where ``violations`` are the gaps whose
    ``required`` flag was cleared. A gap is dropped only when it has no effective
    effect, no exemption, **and** no contract provenance -- so the deadlock guard
    survives without ever silently deleting a real requirement.
    """

    violations = [
        gap for gap in gaps
        if gap.required
        and not gap.effective_required_effects
        and gap.kind not in _EXEMPT_GAP_KINDS
        and not _gap_has_contract_source(gap)
    ]
    if not violations:
        return list(gaps), []
    violation_ids = {gap.gap_id for gap in violations}
    demoted = [
        replace(gap, required=False) if gap.gap_id in violation_ids else gap
        for gap in gaps
    ]
    return demoted, violations


def _missing_effect_deliveries(
    task: TaskSnapshot, spec: TaskSpecSnapshot,
) -> tuple[ToolEffect, ...]:
    """Required side effects with no durable record, in a stable order."""
    return tuple(sorted(
        _required_effect_deliveries(spec) - _recorded_effect_deliveries(task),
        key=lambda item: item.value,
    ))


def _effect_candidate_tools(
    visible_tools: tuple[ToolSpec, ...], effects: frozenset[ToolEffect],
) -> tuple[str, ...]:
    return tuple(sorted(
        tool.name for tool in visible_tools
        if not tool.is_internal_state and tool.effect in effects
    ))








def _required_effect_delivery_result(
    criterion_id: str, task: TaskSnapshot, spec: TaskSpecSnapshot,
) -> AcceptanceResult:
    """Judge the delivery criterion from durable records, never from prose."""
    required = _required_effect_deliveries(spec)
    missing = set(_missing_effect_deliveries(task, spec))
    evidence = tuple(
        Evidence(
            "tool_effect",
            f"the contract requires a performed {effect.value} action",
            ("a durable record exists" if effect not in missing
             else "no durable record exists"),
            "verifier", effect not in missing,
        )
        for effect in sorted(required, key=lambda item: item.value)
    )
    return AcceptanceResult(
        criterion_id,
        (AcceptanceStatus.PASSED if not missing else AcceptanceStatus.FAILED),
        evidence,
    )


def _has_active_workspace_mutation(task: TaskSnapshot) -> bool:
    """Report whether the Task left a net change on disk.

    The mutation journal is append-only and a rollback is itself a new record,
    so counting records cannot answer this: a write followed by its rollback
    leaves two entries and no surviving change. Compare the first observed
    state of each path with its last one instead. A ``None`` hash means the
    file did not exist, so creations and deletions need no special case.
    """
    by_path: dict[str, tuple[MutationRecord, MutationRecord]] = {}
    for mutation in sorted(
        task.mutation_journal, key=lambda item: item.created_at
    ):
        first, _ = by_path.get(mutation.path, (mutation, mutation))
        by_path[mutation.path] = (first, mutation)
    return any(
        first.before_hash != last.after_hash
        for first, last in by_path.values()
    )


@dataclass(frozen=True, slots=True)
class _PostMutationVerification:
    """One inspectable read of the post-mutation verification contract."""

    passed: bool = False
    latest_argv: tuple[str, ...] = ()
    latest_exit_code: int | None = None
    latest_output: str = ""
    latest_reference: str = ""


def _matches_declared_command(
    arguments: Mapping[str, Any], declared: Sequence[str],
) -> bool:
    """Whether a recorded call is the command a criterion bound itself to.

    Prefix match: the criterion declares the entry point and the model may add
    targets (``python -m pytest tests/x``). Case-insensitive because the argv
    compare happens on a normalized command, not a filesystem path.
    """

    if not declared:
        return True
    raw_argv = arguments.get("argv")
    if not isinstance(raw_argv, (list, tuple)):
        return False
    argv = tuple(str(item) for item in raw_argv)
    if len(argv) < len(declared):
        return False
    return all(
        actual.casefold() == expected.casefold()
        for actual, expected in zip(argv, declared)
    )


def _post_mutation_verification(
    task: TaskSnapshot, declared_command: Sequence[str] = (),
) -> _PostMutationVerification:
    """Read the verification contract without discarding the failure reason.

    ``declared_command`` is the criterion's own structured binding. When set,
    only a run of that command can close the criterion; when empty the legacy
    recognizer decides, so historical criteria keep their behaviour.
    """
    if not task.mutation_journal:
        return _PostMutationVerification()
    latest_mutation = max(
        task.mutation_journal, key=lambda item: item.created_at
    )
    attempts: list[tuple[ToolExecutionRecord, Mapping[str, Any]]] = []
    for execution in task.tool_executions.values():
        data = execution.result.data if execution.result is not None else None
        if (
            execution.call.name != "core.run_command"
            or not is_verification_request(execution.call.arguments)
            or not _matches_declared_command(
                execution.call.arguments, declared_command,
            )
            or execution.updated_at < latest_mutation.created_at
            or execution.state is not ToolCommitState.COMMITTED
            or execution.result is None or not execution.result.ok
            or not isinstance(data, Mapping)
            or data.get("mode") != "foreground"
        ):
            continue
        attempts.append((execution, data))
    if not attempts:
        return _PostMutationVerification()
    latest, data = max(attempts, key=lambda item: item[0].updated_at)
    if process_result_succeeded(data):
        return _PostMutationVerification(passed=True)
    output = ""
    for field in ("stderr", "stdout"):
        raw = data.get(field)
        text = raw.get("text") if isinstance(raw, Mapping) else raw
        if isinstance(text, str) and text.strip():
            output = text.strip()
            break
    exit_code = data.get("exit_code")
    return _PostMutationVerification(
        latest_argv=tuple(
            str(item) for item in latest.call.arguments.get("argv", ())
        ),
        latest_exit_code=exit_code if isinstance(exit_code, int) else None,
        latest_output=output[:600],
        latest_reference=latest.invocation_id,
    )


def _post_mutation_verification_description(
    verification: _PostMutationVerification,
    declared_command: Sequence[str] = (),
) -> str:
    """State the requirement in the vocabulary Runtime actually accepts."""
    if declared_command:
        return (
            "The workspace changed but the command this criterion binds to has "
            "not produced a successful run after the latest mutation. Run it in "
            "foreground mode and let it exit 0: `"
            + " ".join(str(item) for item in declared_command)
            + "`. Only that command closes this criterion."
        )
    return (
        "The workspace changed but no successful foreground verification "
        "command was recorded after the latest mutation. Only a recognized "
        "test or build command counts, and it must run in foreground mode and "
        "exit 0. Static syntax checks such as `python -m compileall` or "
        "`python -m py_compile` are not verification and never satisfy this. "
        "Run the project's own test or build target, for example "
        "`python -m pytest`, `npm test`, `cargo test`, or the equivalent "
        "build/lint task for this project."
    )


def _post_mutation_verification_observed(
    verification: _PostMutationVerification, interpreter: str,
) -> str:
    """Explain why the last recognized attempt did not count."""
    parts: list[str] = []
    if verification.latest_argv:
        attempted = " ".join(verification.latest_argv)
        exit_code = (
            verification.latest_exit_code
            if verification.latest_exit_code is not None else "unknown"
        )
        parts.append(f"Last recognized attempt: `{attempted}` exited {exit_code}.")
        if verification.latest_output:
            parts.append(f"Its output was: {verification.latest_output}")
    else:
        parts.append("No recognized test or build command has been run yet.")
    if interpreter:
        parts.append(
            "This workspace provides its own interpreter; run the tests with "
            f"`{interpreter}` rather than a system Python, whose environment "
            "usually lacks the project's test dependencies."
        )
    return " ".join(parts)


def _temporal_context_instruction(now: datetime | None = None) -> str:
    """Return the per-call "what day is it" anchor for the model.

    Models otherwise fall back to their training prior: a real, freshly
    retrieved item dated with the current year gets dismissed as a "future" or
    "simulated" timeline, and the answer degrades to stale recollection. Kept
    short because it is carried on every model call.
    """
    moment = (now or datetime.now()).astimezone()
    offset = moment.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}" if offset else ""
    return (
        f"Today is {moment.strftime('%Y-%m-%d')} "
        f"({moment.tzname() or 'local'} UTC{offset}). Retrieved publish dates at "
        "or before today are current, never a future or simulated timeline; "
        "dated tool results from this turn outrank earlier conversation claims "
        "about what is current."
    )


def _continuation_made_no_progress(
    previous: Mapping[str, Any], required_gap_ids: tuple[str, ...],
    remaining_outcome_ids: tuple[str, ...],
) -> bool:
    """Return whether a resumed continuation left the contract exactly as it was.

    The same required gaps and the same outstanding Outcomes mean the last
    attempt achieved nothing that the Runtime can observe, so replenishing its
    capacity would repeat an identical attempt.
    """
    if not previous:
        return False
    previous_gaps = tuple(sorted(
        str(item) for item in previous.get("gap_ids", [])
    ))
    previous_outcomes = tuple(sorted(
        str(item) for item in previous.get("remaining_outcome_ids", [])
    ))
    return bool(
        required_gap_ids
        and previous_gaps == required_gap_ids
        and previous_outcomes == remaining_outcome_ids
    )


def _goal_matches_command_argv(
    goal: str, arguments: Mapping[str, Any],
) -> bool:
    """Return true only when the user's whole goal is the executed command.

    This intentionally does not infer commands from prose such as "run the
    tests and explain failures". P0 only closes the provable false-success
    case: the complete Task goal parses to the exact structured argv proposed
    by core.run_command. Structured execution remains shell-free.
    """
    raw_argv = arguments.get("argv")
    if (
        not isinstance(raw_argv, (list, tuple))
        or not raw_argv
        or any(not isinstance(item, str) or not item for item in raw_argv)
    ):
        return False
    normalized = goal.strip()
    if not normalized or "\n" in normalized or "\r" in normalized:
        return False
    expected = tuple(raw_argv)
    if tuple(normalized.split()) == expected:
        return True
    try:
        return tuple(shlex.split(normalized, posix=True)) == expected
    except ValueError:
        return False










#: reason_code -> repair text for one recoverable tool-protocol error.
#:
#: A table keyed on the code rather than an if/elif ladder, per the shape rule
#: (AGENTS.md:252). Each entry names the one thing to REPAIR: a generic
#: "invalid envelope" message once sent the model back to fix the wrong thing,
#: so a missing-argument failure repeated itself (task-ce19b33b...).

#: Used when the code is unknown: one retry with an explicitly correct envelope.


class Kernel(CheckpointMixin, SteeringMixin, InputMixin, ModelMixin, PolicyMixin, TaskMixin, MemoryMixin, ApprovalMixin, ProgressMixin, OnboardingMixin, ProjectionMixin, ProcessMixin, WorkspaceMixin, ToolMixin, SessionMixin):
    def __init__(self, dependencies: KernelDependencies) -> None:
        self._dependencies = dependencies
        self._tool_policy = CoreToolPolicy()
        self._background_deadline_tasks: dict[str, asyncio.Task[None]] = {}
        #: Per-Task in-process cancellation resources (task-cancellation-spec
        #: §5.1). Not persisted; closed when the Task settles.
        self._cancellation_scopes: dict[str, CancellationScope] = {}
        #: In-memory cancel snapshot behind CancellationSignal.requested().
        #: Durable truth stays in task.state_changed; this is only a fast read.
        self._cancelled_task_ids: set[str] = set()
        self._workspace_path_locks = _WorkspacePathLockCoordinator(
            dependencies.cross_process_lock, dependencies.workspace_path,
            dependencies.workspace_filesystem,
        )
        self._exploration_coordinator = (
            ExplorationCoordinator(
                dependencies.evidence_relation_providers,
                dependencies.evidence_relation_policy,
                dependencies.rejection_loop_policy,
                dependencies.exploration_outcome_policy,
            )
            if (
                dependencies.evidence_relation_policy is not None
                and dependencies.rejection_loop_policy is not None
                and dependencies.exploration_outcome_policy is not None
            )
            else None
        )

    @property
    def dependencies(self) -> KernelDependencies:
        return self._dependencies

    @property
    def completion_readiness_mode(self) -> CompletionReadinessMode:
        return self._dependencies.completion_readiness_mode

    @property
    def proof_enforcement_mode(self) -> ProofEnforcementMode:
        return self._dependencies.proof_enforcement_mode





























    async def request_task_outcome_completion(
        self, task_id: str, outcome_id: str, *, completion_summary: str,
        evidence_refs: tuple[str, ...], remaining_work: tuple[str, ...],
        writer: str,
    ) -> Mapping[str, Any]:
        """Validate and close one Outcome without granting new authority.

        Ordinary tool success records progress only.  This explicit boundary is
        the sole model-facing way to claim that a multi-step Outcome is done.
        Runtime accepts the claim only when durable evidence and execution state
        contain no required gap.
        """
        normalized_id = outcome_id.strip()
        normalized_summary = completion_summary.strip()
        normalized_refs = tuple(item.strip() for item in evidence_refs)
        normalized_remaining = tuple(
            item.strip() for item in remaining_work if item.strip()
        )
        if not normalized_id or not normalized_summary or not writer.strip():
            raise ValueError(
                "outcome_id, completion_summary, and writer are required"
            )
        if len(normalized_summary) > 2000:
            raise ValueError("completion_summary exceeds 2000 characters")
        if any(not item for item in normalized_refs):
            raise ValueError("evidence_refs must not contain empty values")
        if len(set(normalized_refs)) != len(normalized_refs):
            raise ValueError("evidence_refs must be unique")

        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        events = await self._dependencies.store.read_events(task_id)
        spec = TaskSpecProjector.project(task_id, task.goal, events)
        outcome = next((
            item for item in spec.outcomes if item.outcome_id == normalized_id
        ), None)
        if outcome is None:
            raise ValueError("completion request references unknown outcome")
        if outcome.status.is_closed:
            raise ValueError("completion request references a closed outcome")
        statuses = {item.outcome_id: item.status for item in spec.outcomes}
        if not all(statuses[item].is_closed for item in outcome.depends_on):
            raise ValueError("outcome dependencies are not complete")

        gaps = await self._task_outcome_completion_gaps(
            task, spec, outcome, normalized_refs, normalized_remaining, writer
        )
        request_reference = f"completion:{writer}:{normalized_id}"
        requested = RuntimeEvent(
            f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
            "task_outcome.completion_requested", {
                "outcome_id": normalized_id,
                "completion_summary_hash": canonical_hash(normalized_summary),
                "evidence_refs": list(normalized_refs),
                "remaining_work": list(normalized_remaining),
                "writer": writer,
                "request_reference": request_reference,
            },
        )
        requesting = RuntimeEvent(
            f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 2,
            "task_outcome.state_changed", {
                "outcome_id": normalized_id,
                "status": TaskOutcomeStatus.COMPLETION_REQUESTED.value,
                "fulfillment_ref": "",
                "reason": "completion_request_received",
            },
        )
        state = RuntimeEvent(
            f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 3,
            "task_outcome.state_changed", {
                "outcome_id": normalized_id,
                "status": (TaskOutcomeStatus.IN_PROGRESS.value if gaps else
                           TaskOutcomeStatus.DELIVERED.value),
                "fulfillment_ref": "" if gaps else request_reference,
                "reason": (
                    "completion_request_rejected" if gaps else
                    "completion_request_accepted"
                ),
                "completion_gaps": [gap.to_data() for gap in gaps],
            },
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, task.to_data(), (requested, requesting, state)
        ))
        return {
            "accepted": not gaps,
            "outcome_id": normalized_id,
            "status": (
                TaskOutcomeStatus.IN_PROGRESS.value if gaps else
                TaskOutcomeStatus.DELIVERED.value
            ),
            "completion_gaps": [gap.to_data() for gap in gaps],
            "request_reference": request_reference,
        }

    async def _task_outcome_completion_gaps(
        self, task: TaskSnapshot, spec: TaskSpecSnapshot,
        outcome: TaskOutcomeSnapshot,
        evidence_refs: tuple[str, ...], remaining_work: tuple[str, ...],
        writer: str,
    ) -> tuple[CompletionGap, ...]:
        """Return durable gaps for one explicit Outcome completion claim."""
        gaps: list[CompletionGap] = []
        known_refs = set(outcome.fulfillment_refs)
        unknown_refs = tuple(ref for ref in evidence_refs if ref not in known_refs)
        if unknown_refs:
            gaps.append(CompletionGap(
                f"outcome-evidence:{outcome.outcome_id}",
                "UNVERIFIED_EVIDENCE_REFERENCE",
                "Completion cited evidence not bound to this Outcome: "
                + ", ".join(unknown_refs[:5]),
                "MISSING", evidence_reference=unknown_refs[0],
            ))
        fulfilled_effects = {
            ToolEffect(ref.rsplit(":", 1)[-1])
            for ref in evidence_refs
            if ref.rsplit(":", 1)[-1] in {item.value for item in ToolEffect}
        }
        missing_effects = tuple(
            effect for effect in outcome.required_effects
            if effect not in fulfilled_effects
        )
        if missing_effects:
            gaps.append(CompletionGap(
                f"outcome-effects:{outcome.outcome_id}",
                "REQUIRED_EVIDENCE_MISSING", outcome.description, "MISSING",
                required_effects=missing_effects,
            ))
        if remaining_work:
            gaps.append(CompletionGap(
                f"outcome-remaining:{outcome.outcome_id}", "REMAINING_WORK",
                "; ".join(remaining_work[:10]), "PENDING",
            ))

        effective_memory = await self.get_effective_working_memory(task.task_id)
        open_required = tuple(
            item for item in spec.outcomes
            if item.required and not item.status.is_closed
        )
        authored_remaining = effective_memory.snapshot.remaining_work
        if len(open_required) == 1 and authored_remaining:
            gaps.append(CompletionGap(
                f"working-memory:{outcome.outcome_id}", "REMAINING_WORK",
                "Working memory still records required work: "
                + "; ".join(authored_remaining[:10]), "PENDING",
            ))
        open_steps = tuple(
            step for step in effective_memory.snapshot.plan
            if step.status in {
                WorkingPlanStepStatus.PENDING, WorkingPlanStepStatus.IN_PROGRESS,
                WorkingPlanStepStatus.BLOCKED,
            }
        )
        if len(open_required) == 1 and open_steps:
            gaps.append(CompletionGap(
                f"working-plan:{outcome.outcome_id}", "PLAN_STEP",
                "Working plan still has open steps: " + "; ".join(
                    step.description for step in open_steps[:10]
                ), "PENDING",
            ))

        active = tuple(
            execution for execution in task.tool_executions.values()
            if execution.invocation_id != writer and execution.state in {
                ToolCommitState.PREPARED, ToolCommitState.RUNNING,
                ToolCommitState.UNKNOWN_OUTCOME,
            }
        )
        if active:
            gaps.append(CompletionGap(
                f"open-tool-execution:{outcome.outcome_id}", "OPEN_TOOL_BATCH",
                "Tool executions are not reconciled: " + ", ".join(
                    item.execution_id for item in active[:10]
                ), "OPEN",
            ))
        current_call_id = next((
            execution.call.call_id
            for execution in task.tool_executions.values()
            if execution.invocation_id == writer
        ), "")
        raw_pending = (task.active_agent_checkpoint or {}).get(
            "pending_tool_calls", []
        )
        pending_call_ids = tuple(
            str(item.get("call_id", ""))
            for item in raw_pending if isinstance(item, Mapping)
            and str(item.get("call_id", "")) != current_call_id
        ) if isinstance(raw_pending, list) else ()
        if pending_call_ids:
            gaps.append(CompletionGap(
                f"open-tool-batch:{outcome.outcome_id}", "OPEN_TOOL_BATCH",
                "The current ToolBatch still has pending calls: "
                + ", ".join(pending_call_ids[:10]), "OPEN",
            ))
        if task.pending_approval is not None or task.pending_clarification is not None:
            gaps.append(CompletionGap(
                f"pending-user-action:{outcome.outcome_id}",
                "PENDING_USER_ACTION",
                "A required approval or clarification is still pending.", "OPEN",
            ))

        if (
            ToolEffect.EXECUTE in outcome.required_effects
            and task.mutation_journal
        ):
            verification = _post_mutation_verification(task)
            if not verification.passed:
                gaps.append(CompletionGap(
                    f"outcome-verification:{outcome.outcome_id}",
                    "MISSING_VERIFICATION",
                    _post_mutation_verification_description(verification),
                    "MISSING", required_effects=(ToolEffect.EXECUTE,),
                    observed=_post_mutation_verification_observed(
                        verification, self._local_interpreter(task)
                    ),
                ))
        return tuple(gaps)


    def _demote_unresolvable_criteria(
        self, task: TaskSnapshot, events: tuple[RuntimeEvent, ...],
        criteria: tuple[TaskAcceptanceCriterion, ...],
    ) -> tuple[tuple[TaskAcceptanceCriterion, ...], list[dict[str, str]]]:
        """Demote criteria whose verification channel cannot decide them.

        Two shapes are demoted to the advisory ``rubric`` channel:

        * an ``evidence_reference`` criterion asserts a fact that must already
          be true; it is never work that execution can produce. A reference no
          event/tool_call/mutation can resolve is permanently unverifiable and
          must not survive as a required gate.
        * a ``post_mutation_command`` criterion promises a *command* decides it.
          A criterion that declares no command, or declares one that cannot
          falsify a claim (``git status``, ``py_compile``), is asking the runtime
          to close a gate with evidence it can never accept -- the shape behind
          ``task-c2d21bce8ea74f0bbcac07aba4aefedf``. Demotion keeps the
          requirement visible to a judge instead of burning the turn budget on
          a debt nothing can pay.

        Demotion happens only on the authoring path, so historical snapshots
        with unbound command criteria keep replaying unchanged.
        """
        kept: list[TaskAcceptanceCriterion] = []
        demoted: list[dict[str, str]] = []
        for criterion in criteria:
            if (
                criterion.verification_kind
                is TaskCriterionKind.POST_MUTATION_COMMAND
            ):
                declared = tuple(criterion.command)
                if declared and is_verification_command(declared):
                    kept.append(criterion)
                    continue
                demoted.append({
                    "criterion_id": criterion.criterion_id,
                    "from": TaskCriterionKind.POST_MUTATION_COMMAND.value,
                    "to": TaskCriterionKind.RUBRIC.value,
                    "reference": " ".join(declared),
                    "reason": (
                        "UNRECOGNIZED_VERIFICATION_COMMAND"
                        if declared else "NO_DECLARED_VERIFICATION_COMMAND"
                    ),
                })
                kept.append(TaskAcceptanceCriterion(
                    criterion_id=criterion.criterion_id,
                    description=criterion.description,
                    verification_kind=TaskCriterionKind.RUBRIC,
                    evidence_reference=None,
                ))
                continue
            if (
                criterion.verification_kind
                is not TaskCriterionKind.EVIDENCE_REFERENCE
            ):
                kept.append(criterion)
                continue
            reference = criterion.evidence_reference or ""
            evidence = self._verify_task_spec_reference(
                task, events, reference, criterion.description
            )
            if evidence.passed:
                kept.append(criterion)
                continue
            kept.append(TaskAcceptanceCriterion(
                criterion_id=criterion.criterion_id,
                description=criterion.description,
                verification_kind=TaskCriterionKind.RUBRIC,
                evidence_reference=None,
            ))
            demoted.append({
                "criterion_id": criterion.criterion_id,
                "from": TaskCriterionKind.EVIDENCE_REFERENCE.value,
                "to": TaskCriterionKind.RUBRIC.value,
                "reference": reference,
                "reason": evidence.observed,
            })
        return tuple(kept), demoted

    def _demote_background_leaks(
        self, criteria: tuple[TaskAcceptanceCriterion, ...],
        planning_context: Mapping[str, Any], current_request: str,
    ) -> tuple[tuple[TaskAcceptanceCriterion, ...], list[dict[str, str]]]:
        """INV-9: a criterion copied from non-authoritative text is demoted.

        Only text the user did not already say counts as copyable background, so
        a criterion that legitimately restates the current request is never
        mistaken for a leak. Demotion is to the advisory ``rubric`` channel, so
        the criterion survives inspection but can no longer gate completion.
        """
        request_normalized = " ".join(current_request.casefold().split())
        background_texts = tuple(
            text for text in self._background_texts(planning_context)
            if " ".join(text.casefold().split()) not in request_normalized
        )
        if not background_texts:
            return criteria, []
        kept: list[TaskAcceptanceCriterion] = []
        demoted: list[dict[str, str]] = []
        for criterion in criteria:
            leak = find_background_leak(
                criterion.criterion_id + " " + criterion.description,
                background_texts,
            )
            if leak is None or (
                criterion.verification_kind is TaskCriterionKind.RUBRIC
            ):
                kept.append(criterion)
                continue
            kept.append(TaskAcceptanceCriterion(
                criterion_id=criterion.criterion_id,
                description=criterion.description,
                verification_kind=TaskCriterionKind.RUBRIC,
                evidence_reference=None,
            ))
            demoted.append({
                "criterion_id": criterion.criterion_id,
                "from": criterion.verification_kind.value,
                "to": TaskCriterionKind.RUBRIC.value,
                "leaked_span": leak,
                "source": ContextAuthority.SCOPED_BACKGROUND.value,
            })
        return tuple(kept), demoted

    @staticmethod
    def _background_texts(
        planning_context: Mapping[str, Any],
    ) -> tuple[str, ...]:
        """Every non-authoritative string the Planner was shown."""
        texts: list[str] = []
        related = planning_context.get("related_task")
        if isinstance(related, Mapping):
            goal = related.get("goal")
            if isinstance(goal, str):
                texts.append(goal)
            for key in ("historical_remaining_work", "completed_work"):
                values = related.get(key)
                if isinstance(values, (list, tuple)):
                    texts.extend(
                        str(item) for item in values if isinstance(item, str)
                    )
        session = planning_context.get("session")
        working = session.get("working_state") if isinstance(session, Mapping) else None
        if isinstance(working, Mapping):
            for value in working.values():
                if isinstance(value, str):
                    texts.append(value)
                elif isinstance(value, (list, tuple)):
                    texts.extend(
                        str(item) for item in value if isinstance(item, str)
                    )
        return tuple(text for text in texts if text.strip())
















    @staticmethod
    def _validate_working_evidence(
        task: TaskSnapshot, events: tuple[RuntimeEvent, ...], state: Mapping[str, Any],
    ) -> None:
        raw = state.get("evidence")
        if not isinstance(raw, list):
            raise ValueError("working memory evidence must be a list")
        event_sequences = {event.sequence for event in events}
        tool_calls = {
            execution.call.call_id for execution in task.tool_executions.values()
        }
        mutations = {item.mutation_id for item in task.mutation_journal}
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError("working memory evidence entries must be objects")
            reference = str(item.get("reference") or "")
            if reference.startswith("event:"):
                try:
                    exists = int(reference.removeprefix("event:")) in event_sequences
                except ValueError:
                    exists = False
            elif reference.startswith("tool_call:"):
                exists = reference.removeprefix("tool_call:") in tool_calls
            elif reference.startswith("mutation:"):
                exists = reference.removeprefix("mutation:") in mutations
            else:
                raise ValueError(
                    "working memory evidence reference must use event:, tool_call:, "
                    "or mutation:"
                )
            if not exists:
                raise ValueError(f"working memory evidence does not exist: {reference}")


























    def _cancellation_scope(self, task_id: str) -> CancellationScope:
        """Return the Task's in-process resource scope, creating it on demand."""
        scope = self._cancellation_scopes.get(task_id)
        if scope is None:
            scope = CancellationScope()
            self._cancellation_scopes[task_id] = scope
            note_scope_opened()
        return scope

    def cancellation_signal(self, task_id: str) -> TaskCancellationSignal:
        """Spec §5 facade: the Task-level cancellation signal."""
        return TaskCancellationSignal(
            task_id, self._cancelled_task_ids.__contains__, self._request_cancel,
        )

    def _request_cancel(self, task_id: str, reason: str) -> None:
        """Write durable cancel intent from a synchronous facade call."""
        loop = asyncio.get_running_loop()
        loop.create_task(self.dispatch_input_event(
            CancelTaskInput(task_id, reason)
        ))

    async def _close_cancellation_scope(self, task_id: str) -> None:
        """Release every in-process resource of a settled Task generation."""
        scope = self._cancellation_scopes.pop(task_id, None)
        if scope is None:
            return
        note_scope_closed(await scope.close_all())



    async def _require_stored_task(self, task_id: str):
        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        return stored
















    def _compensate_prepared(self, kind: str, prepared: Any) -> None:
        if kind == "write":
            self._compensate_prepared_mutation(prepared)
            return
        if kind != "delete":
            raise ValueError(f"unsupported compensation kind: {kind}")
        if not prepared.path.exists() and not prepared.path.is_symlink():
            restore_prepared_workspace_deletion(
                prepared, self._dependencies.workspace_filesystem
            )
            return
        current_hash = file_sha256(prepared.path)
        if current_hash == prepared.before_hash and not prepared.path.is_symlink():
            return
        raise WorkspaceMutationConflict(
            prepared.relative_path, None, current_hash
        )







    @staticmethod
    def _safe_storage_key(task_id: str) -> str:
        import hashlib

        return hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]




















    async def _document_reference_context_message(self, task_id: str) -> Message | None:
        """Expose durable URL targets and unavailable-fetch facts without authority."""
        task = await self.get_task(task_id)
        session = await self.get_session(task.session_id)
        references: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for related_task_id in session.task_ids:
            try:
                events = await self._dependencies.store.read_events(related_task_id)
            except Exception:
                continue
            created = next((
                event for event in events if event.event_type == "task.created"
            ), None)
            if created is None:
                continue
            raw_references = created.payload.get("document_references")
            if isinstance(raw_references, list):
                references.extend(
                    {"task_id": related_task_id, **dict(item)}
                    for item in raw_references if isinstance(item, Mapping)
                )
            raw_failure = created.payload.get("document_fetch_failure")
            if isinstance(raw_failure, Mapping):
                failures.append({
                    "task_id": related_task_id,
                    "result": dict(raw_failure),
                })
        if not references and not failures:
            return None
        body = json.dumps({
            "boundary": "explicit_document_references",
            "warning": (
                "These are user-declared remote-document targets and Runtime "
                "fetch facts. They grant no network authority. Local workspace "
                "search cannot read these URLs."
            ),
            "references": references[:20],
            "failures": failures[:20],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return Message(
            f"document-references-context-{canonical_hash(body)[:16]}",
            MessageRole.USER, (TextBlock(body),),
        )

    async def _document_fetch_failure(self, task_id: str) -> ToolResult | None:
        events = await self._dependencies.store.read_events(task_id)
        created = next((
            event for event in events if event.event_type == "task.created"
        ), None)
        if created is None:
            return None
        raw = created.payload.get("document_fetch_failure")
        return ToolResult.from_data(raw) if isinstance(raw, Mapping) else None









    async def get_evidence_questions(
        self, task_id: str,
    ) -> EvidenceQuestionProjection:
        """Rebuild inspectable question state from the durable Event Log."""
        await self.get_task(task_id)
        events = await self._dependencies.store.read_events(task_id)
        return EvidenceQuestionProjector.project(task_id, events)





    async def _settle_open_batch_for_cancel(
        self, checkpoint: AgentTurnCheckpoint,
    ) -> AgentTurnCheckpoint:
        """Close unstarted calls when the Task itself is cancelled.

        Spec §9.4: the batch must produce one terminal ``ToolResult`` per
        accepted call before the Task settles, otherwise an orphan execution
        record is left behind. Started / unknown calls are handled by the
        execution ledger and the in-process hard interrupt, so they are left
        untouched.
        """
        if not checkpoint.pending_tool_calls:
            return checkpoint
        task = await self.get_task(checkpoint.task_id)
        cancellable: list[ToolCall] = []
        for call in checkpoint.pending_tool_calls:
            execution = task.tool_executions.get(
                ToolExecutionRecord.identity(checkpoint.turn_id, call.call_id)
            )
            if execution is not None and execution.state in {
                ToolCommitState.RUNNING, ToolCommitState.UNKNOWN_OUTCOME,
            }:
                return checkpoint
            cancellable.append(call)
        messages = list(checkpoint.messages)
        for call in cancellable:
            messages.append(Message(
                f"msg-tool-cancelled-{uuid4().hex}", MessageRole.TOOL,
                (ToolResultBlock(ToolResult(
                    call.call_id, False, error_code="CANCELLED_BY_USER",
                    message="The call was not started because the Task was cancelled.",
                    recovery_kind=ToolRecoveryKind.TERMINAL,
                    meta={"cancelled_by_user_input": True},
                )),),
            ))
        return replace(
            checkpoint, revision=checkpoint.revision + 1,
            messages=tuple(messages), pending_tool_calls=(),
            tool_batch=(
                checkpoint.tool_batch.with_pending(
                    (), status=ToolBatchStatus.CANCELLED
                ) if checkpoint.tool_batch is not None else None
            ),
        )

    async def _cancel_open_batch_for_queued_replace(
        self, checkpoint: AgentTurnCheckpoint,
    ) -> AgentTurnCheckpoint:
        """Close unstarted calls when a queued Replace supersedes them.

        This is protocol bookkeeping, not natural-language interpretation. The
        input router already classified Replace; here the Kernel only preserves
        one ToolResult for every accepted ToolCall before exposing new input to
        the model. Started/unknown calls are handled by the execution ledger.
        """
        if not checkpoint.pending_tool_calls:
            return checkpoint
        events = await self._dependencies.store.read_events(checkpoint.task_id)
        projection = SteeringProjector.project(checkpoint.task_id, events)
        pending_inputs = tuple(
            item for item in projection.pending
            if item.inbound_sequence > checkpoint.last_steering_inbound_sequence
        )
        if not any(item.kind is SteeringKind.REPLACE for item in pending_inputs):
            return checkpoint
        task = await self.get_task(checkpoint.task_id)
        cancellable: list[ToolCall] = []
        for call in checkpoint.pending_tool_calls:
            execution = task.tool_executions.get(
                ToolExecutionRecord.identity(checkpoint.turn_id, call.call_id)
            )
            if execution is not None and execution.state in {
                ToolCommitState.RUNNING, ToolCommitState.UNKNOWN_OUTCOME,
            }:
                return checkpoint
            cancellable.append(call)
        messages = list(checkpoint.messages)
        for call in cancellable:
            messages.append(Message(
                f"msg-tool-cancelled-{uuid4().hex}", MessageRole.TOOL,
                (ToolResultBlock(ToolResult(
                    call.call_id, False, error_code="CANCELLED_BY_REPLACE",
                    message="The call was not started because the user replaced the goal.",
                    recovery_kind=ToolRecoveryKind.TERMINAL,
                    meta={"cancelled_by_user_input": True},
                )),),
            ))
        updated = replace(
            checkpoint, revision=checkpoint.revision + 1,
            messages=tuple(messages), pending_tool_calls=(),
            tool_batch=(
                checkpoint.tool_batch.with_pending(
                    (), status=ToolBatchStatus.CANCELLED
                ) if checkpoint.tool_batch is not None else None
            ),
        )
        await self._save_agent_checkpoint(updated, "tool-batch-cancelled-by-replace")
        return updated

    @staticmethod
    def _assistant_conclusion(message: Message) -> AssistantConclusion | None:
        conclusions = [
            block.conclusion for block in message.content
            if isinstance(block, ConclusionBlock)
        ]
        if len(conclusions) > 1:
            raise InvalidModelResponse(
                "assistant response may contain at most one conclusion block"
            )
        return conclusions[0] if conclusions else None

    @staticmethod
    def _source_user_message(messages: Sequence[Message]) -> Message | None:
        generated_prefixes = (
            "project-onboarding-context-", "project-memory-context-",
            "session-context-", "working-memory-context-", "task-spec-context-",
            "project-instructions-context-", "completion-readiness-",
        )
        return next((
            message for message in messages
            if message.role is MessageRole.USER
            and not message.message_id.startswith(generated_prefixes)
        ), None)

    async def _commit_final_agent_result(
        self, *, task_id: str, turn_id: str, user_message: Message,
        assistant_message: Message, llm_payload: Mapping[str, Any],
        additional_events: tuple[tuple[str, Mapping[str, Any]], ...] = (),
        clear_agent_checkpoint: bool = True,
    ) -> ConclusionReferenceValidation | None:
        """Atomically record the final Task answer and its Session handoff.

        This boundary is intentionally archival only.  Completion readiness has
        already selected the answer; a conclusion validation can never change
        task state, trigger a retry, or reinterpret its visible text.
        """
        conclusion = self._assistant_conclusion(assistant_message)
        for attempt in range(3):
            stored = await self._require_stored_task(task_id)
            task = TaskSnapshot.from_data(stored.data)
            events = await self._dependencies.store.read_events(task_id)
            validation = (
                ConclusionReferenceValidator().validate(task, events, conclusion)
                if conclusion is not None else None
            )
            updated_task = (
                task.with_agent_checkpoint(None)
                if clear_agent_checkpoint else task
            )
            # The turn's durable counters live in the checkpoint being retired.
            # Read them *before* clearing it: `turn.completed` is the only place
            # a finished turn's tool-call count is recoverable afterwards, and a
            # missing key would silently report zero tools for every Task.
            final_checkpoint = (
                task.active_agent_checkpoint
                if isinstance(task.active_agent_checkpoint, Mapping) else {}
            )
            raw_tool_calls = final_checkpoint.get("tool_calls")
            completed_tool_calls = (
                raw_tool_calls
                if isinstance(raw_tool_calls, int)
                and not isinstance(raw_tool_calls, bool)
                else 0
            )
            llm_event = RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
                "llm.completed", dict(llm_payload),
            )
            task_events: list[RuntimeEvent] = [llm_event]
            if validation is not None:
                validation_event_type = {
                    ConclusionValidationStatus.VALID: "conclusion.references_validated",
                    ConclusionValidationStatus.INVALID: "conclusion.references_invalid",
                    ConclusionValidationStatus.INCOMPLETE: "conclusion.references_incomplete",
                }[validation.status]
                task_events.append(RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id,
                    stored.last_event_sequence + len(task_events) + 1,
                    validation_event_type, {
                        "turn_id": turn_id,
                        "assistant_message_id": assistant_message.message_id,
                        "conclusion": conclusion.to_data(),
                        "validation": validation.to_data(),
                    },
                ))
            for event_type, payload in additional_events:
                task_events.append(RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id,
                    stored.last_event_sequence + len(task_events) + 1,
                    event_type, payload,
                ))
            task_events.append(RuntimeEvent(
                f"evt-{uuid4().hex}", task_id,
                stored.last_event_sequence + len(task_events) + 1,
                "turn.completed", {
                    "turn_id": turn_id,
                    "model_calls": llm_payload.get("model_call"),
                    "tool_calls": completed_tool_calls,
                },
            ))
            session_unit = await self._session_task_result_unit(
                updated_task, turn_id, user_message, assistant_message,
                assistant_conclusion=conclusion,
                conclusion_validation=validation,
                answer_event_ref=llm_event.event_id,
            )
            command_id = "final-agent-result:" + canonical_hash({
                "task_id": task_id,
                "turn_id": turn_id,
                "assistant_message": assistant_message.to_data(),
                "conclusion": conclusion.to_data() if conclusion is not None else None,
                "validation": validation.to_data() if validation is not None else None,
            })
            try:
                await self._dependencies.store.commit_session_and_task(
                    SessionTaskUnitOfWork(
                        session_unit,
                        RuntimeUnitOfWork(
                            task_id, stored.version, updated_task.to_data(),
                            tuple(task_events),
                        ),
                        command_id,
                    )
                )
                return validation
            except ValueError as error:
                # Final Task/session results are a single archival boundary.
                # Stores must provide an atomic commit (SQLite schema v2 does);
                # retrying separate task and session writes could publish a
                # half-result after a process interruption.
                if "version conflict" not in str(error) or attempt == 2:
                    raise
        raise RuntimeError("unreachable final result retry state")



    async def _completion_readiness_gaps(
        self, task_id: str, visible_tools: tuple[ToolSpec, ...],
    ) -> tuple[CompletionGap, ...]:
        """Return no completion gaps: completion has no obligation record.

        Hermes and Codex carry no obligation/gap vocabulary at all -- a turn
        ends when the model stops calling tools, and nothing has to be proven
        first. The Runtime has already stopped building them; this seam stays
        only until the specs that consult it are removed with their batch.
        """
        return ()

    async def _unfinished_plan_instruction(self, task_id: str) -> str | None:
        """Advisory nudge for an un-updated scratchpad plan.

        The plan is the model's temporary state, not a delivery contract, so an
        unfinished step must never reject completion. A short instruction keeps
        the scratchpad honest without letting it override recorded facts.
        """
        task = await self.get_task(task_id)
        events = await self._dependencies.store.read_events(task_id)
        memory = self._dependencies.working_memory_projector.project(
            task_id, task.goal, events
        )
        unfinished = any(
            step.status in {
                WorkingPlanStepStatus.PENDING,
                WorkingPlanStepStatus.IN_PROGRESS,
            }
            for step in memory.plan
        )
        if not unfinished:
            return None
        return (
            "Working-memory plan still lists unfinished steps. If the underlying "
            "work is already done, update the scratchpad with "
            "core.working_memory_update before finishing; the plan itself is not "
            "a delivery contract."
        )

    async def _evaluate_completion_readiness(
        self, task_id: str, turn_id: str, checkpoint: AgentTurnCheckpoint,
        visible_tools: tuple[ToolSpec, ...], *, forced_wrap_up: bool,
    ) -> CompletionReadinessDecision:
        """Ask the replaceable policy whether a proposed final may finish."""
        policy = self._dependencies.completion_readiness_policy
        state = CompletionReadinessState.from_data(
            checkpoint.completion_readiness_state
        )
        # Completion has no obligation record (Hermes/Codex): no gaps exist.
        gaps = ()
        available_read_tools = tuple(
            sorted(tool.name for tool in visible_tools
                   if tool.is_read_only and not tool.is_internal_state)
        )
        available_effects = frozenset(
            ToolEffect.OBSERVE
            if tool.effect is ToolEffect.UNSPECIFIED and tool.is_read_only
            else tool.effect
            for tool in visible_tools
            if not tool.is_internal_state
            and (
                tool.effect is not ToolEffect.UNSPECIFIED
                or tool.is_read_only
            )
        )
        available_tools = tuple(sorted(
            tool.name for tool in visible_tools if not tool.is_internal_state
        ))
        task = await self.get_task(task_id)
        inventory = EvidenceInventory.from_data(checkpoint.evidence_inventory)
        specs_by_name = {tool.name: tool for tool in visible_tools}
        execution_facts = project_execution_facts(
            task.tool_executions.values(), specs_by_name,
            known_mutation_ids=frozenset(
                mutation.mutation_id for mutation in task.mutation_journal
            ),
            include_successful=True,
        )
        unresolved_failure_facts = tuple(
            fact for fact in execution_facts
            if fact.is_blocking_failure
            and fact.resolution is FactResolution.UNRESOLVED
        )
        non_succeeded_ids = {
            fact.execution_id for fact in execution_facts
            if fact.effect_status is not EffectStatus.SUCCEEDED
        }
        probe = CompletionReadinessProbe(
            goal=task.goal, gaps=gaps,
            remaining_model_calls=max(
                0, checkpoint.max_model_calls - checkpoint.model_calls
            ),
            remaining_tool_calls=max(
                0, checkpoint.max_tool_calls - checkpoint.tool_calls
            ),
            available_read_tools=available_read_tools,
            available_effects=available_effects,
            available_tools=available_tools,
            forced_wrap_up=forced_wrap_up,
            evidence_item_count=sum(
                len(values) for values in inventory.fingerprints.values()
            ),
            successful_tool_calls=sum(
                execution.state is ToolCommitState.COMMITTED
                and execution.result is not None and execution.result.ok
                and execution.execution_id not in non_succeeded_ids
                for execution in task.tool_executions.values()
            ),
            unresolved_failures=unresolved_failure_facts,
        )
        if policy is None:
            # A missing policy is a configuration fact, not a crash. Keep
            # COMPLETE for compositions that intentionally omit it.
            decision = CompletionReadinessDecision(
                CompletionReadinessAction.COMPLETE, "policy_not_configured",
                state, gaps,
            )
        else:
            try:
                decision = await policy.evaluate(probe, state)
            except Exception as error:
                await self._append_events(task_id, ((
                    "completion.readiness_failed", {
                        "turn_id": turn_id,
                        "error_type": type(error).__name__,
                        "fail_closed": True,
                    },
                ),))
                # INV-5: never accept a final answer on a crashed judge. The
                # caller surfaces a defined failure (Task FAILED) instead of
                # success. Diagnostics callers wrap this in their own guard.
                raise
        await self._append_events(task_id, ((
            "completion.readiness_evaluated",
            _readiness_event_payload(
                decision, probe, turn_id=turn_id,
                mode=self.completion_readiness_mode,
                forced_wrap_up=forced_wrap_up,
            ),
        ),))
        return decision

    async def _inject_agent_decides_completion_diagnostics(
        self, checkpoint: AgentTurnCheckpoint,
        visible_tools: tuple[ToolSpec, ...],
    ) -> AgentTurnCheckpoint:
        """Give AGENT_DECIDES factual readiness context without directives."""
        if self.completion_readiness_mode is not CompletionReadinessMode.AGENT_DECIDES:
            return checkpoint
        try:
            decision = await self._evaluate_completion_readiness(
                checkpoint.task_id, checkpoint.turn_id, checkpoint, visible_tools,
                forced_wrap_up=False,
            )
        except Exception:
            # Diagnostics are observational and must never break a Turn.
            return checkpoint
        diagnostic = {
            "boundary": "completion_readiness_diagnostics",
            "mode": self.completion_readiness_mode.value,
            "action": decision.action.value,
            "reason": decision.reason,
            "gaps": [gap.to_data() for gap in decision.gaps],
        }
        return replace(
            checkpoint,
            messages=(*checkpoint.messages, Message(
                f"completion-diagnostics-{uuid4().hex}", MessageRole.USER,
                (TextBlock(json.dumps(diagnostic, sort_keys=True,
                                      separators=(",", ":"))),),
            )),
        )

    @staticmethod
    def _completion_correction_message(
        decision: CompletionReadinessDecision,
    ) -> Message:
        continue_work = decision.action is CompletionReadinessAction.CONTINUE
        incomplete_recoverable = (
            decision.action
            is CompletionReadinessAction.REPORT_INCOMPLETE_RECOVERABLE
        )
        body = {
            "boundary": "completion_readiness",
            "action": decision.action.value,
            "instruction": (
                "The proposed final answer was not accepted. Perform the "
                "remaining required in-scope work now using an available "
                "capability or the candidate tools named in the gaps. If the "
                "approach that just failed cannot work, take a different one -- "
                "another tool, or different arguments -- for the same scope: a "
                "missing path or a rejected call is not evidence that the "
                "requirement is unprovable. Normal argument validation, policy, "
                "approval, and sandbox rules still apply. Do not merely offer to "
                "continue later."
                if continue_work else
                (
                    "Do not call tools. Report that this Turn is incomplete but "
                    "resumable, identify the last safe checkpoint and remaining "
                    "required work, and do not describe it as blocked or successful."
                    if incomplete_recoverable else
                    "Do not call tools. Give the user the exact blocker, completed "
                    "evidence, and unverified requirement. Do not claim success."
                )
            ),
            "gaps": [gap.to_data() for gap in decision.gaps],
        }
        return Message(
            f"completion-readiness-{uuid4().hex}", MessageRole.USER,
            (TextBlock(json.dumps(body, sort_keys=True, separators=(",", ":"))),),
            scaffolding="completion-readiness",
        )






    async def _rebuild_evidence_inventory(
        self, task_id: str, turn_id: str,
    ) -> Mapping[str, Any]:
        """Rebuild Turn evidence fingerprints from their durable Events."""
        fingerprints: dict[str, set[str]] = {
            category: set() for category in EVIDENCE_CATEGORIES
        }
        consecutive_zero_delta = 0
        for event in await self._dependencies.store.read_events(task_id):
            if (
                event.event_type != "evidence.delta_evaluated"
                or event.payload.get("turn_id") != turn_id
            ):
                continue
            raw_items = event.payload.get("items", [])
            if isinstance(raw_items, list):
                for item in raw_items:
                    if not isinstance(item, Mapping):
                        continue
                    category = str(item.get("category", ""))
                    fingerprint = str(item.get("fingerprint", ""))
                    if category in fingerprints and fingerprint:
                        fingerprints[category].add(fingerprint)
            consecutive_zero_delta = max(
                0, int(event.payload.get("consecutive_zero_delta", 0))
            )
        return EvidenceInventory(
            {
                category: tuple(sorted(values))
                for category, values in fingerprints.items()
            },
            consecutive_zero_delta,
        ).to_data()








    async def complete_task(self, task_id: str, reason: str) -> TaskSnapshot:
        """Close a Task whose turn ended without pending tool calls.

        This is the whole completion rule (Hermes/Codex semantics): the model
        stops calling tools, so the turn is over and the Task succeeded. No
        verdict, no evidence, no criterion is consulted.

        ``VERIFYING`` and ``FINALIZING`` are pure accounting hops here: the
        success path lives in ``core/task.py`` next to ``LEGAL_TRANSITIONS``.
        """
        for target in SUCCESS_PATH:
            task = await self.transition_task(task_id, target, reason)
        return task

    async def finalize_acceptance(
        self, task_id: str, status: AcceptanceStatus,
    ) -> TaskSnapshot:
        """Move a VERIFYING Task to the one terminal state its verdict implies.

        Every entry point (CLI / SDK / Web / Local API) must call this instead of
        mapping a verdict itself. The mapping is Runtime policy: when clients map
        for themselves, the same verdict becomes ``FAILED`` on one surface and
        ``NEEDS_REVIEW`` on another -- which is precisely what the CLI used to do
        for a ``BLOCKED`` verdict.

        ``PASSED`` still passes through ``FINALIZING`` because that is the only
        legal edge to ``SUCCEEDED``.
        """
        target = terminal_state_for(status)
        stored = await self._require_stored_task(task_id)
        current = TaskSnapshot.from_data(stored.data)
        if current.state is target:
            return current
        if current.state.is_terminal:
            raise InvalidTurnState(
                f"task {task_id} is already terminal in {current.state.value}; "
                f"cannot apply acceptance verdict {status.value}"
            )
        if target is TaskState.SUCCEEDED:
            await self.transition_task(
                task_id, TaskState.FINALIZING, "acceptance passed"
            )
        return await self.transition_task(
            task_id, target, f"acceptance verdict {status.value}"
        )

    async def verify_task_acceptance(
        self, task_id: str, *, include_semantic: bool = True,
    ) -> TaskVerificationResult:
        """Verify persisted effects without rerunning model or command side effects.

        ``include_semantic=False`` runs only the deterministic stages, so a
        read-only diagnostic caller never pays for a judge call (SPEC §7 P0).
        A judged criterion is then reported as undetermined, never as passed.
        """
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        observation_mode = (
            task.state is TaskState.EXECUTING
            and self.completion_readiness_mode
            is not CompletionReadinessMode.LEGACY_GATE
        )
        if task.state is not TaskState.VERIFYING and not observation_mode:
            raise InvalidTurnState(
                f"task {task_id} must be VERIFYING, got {task.state.value}"
            )
        events = await self._dependencies.store.read_events(task_id)
        task_spec = TaskSpecProjector.project(task_id, task.goal, events)
        mandatory_answer_completeness = any(
            event.event_type == "turn.completed" for event in events
        )
        answer_result = (
            self._verify_answer_completeness(events)
            if mandatory_answer_completeness else None
        )
        answer_reference = next((
            f"event:{event.sequence}" for event in reversed(events)
            if event.event_type == "llm.completed"
        ), "")
        # Read/search evidence belongs to the Task, even when the semantic
        # planner separates evidence collection from the final ANSWER outcome.
        # Reuse only durable observation references here. Side-effect evidence
        # (mutate/execute/control/interact) remains bound to its own Outcome and
        # can never satisfy another result implicitly.
        shared_observation_refs = tuple(dict.fromkeys(
            reference
            for candidate in task_spec.outcomes
            for reference in candidate.fulfillment_refs
            if reference.rsplit(":", 1)[-1] == ToolEffect.OBSERVE.value
        ))
        if task_spec.schema_version == 0:
            events = await self._dependencies.store.read_events(task_id)
            task_spec = TaskSpecProjector.project(task_id, task.goal, events)
        answer_evidence_result = (
            self._verify_answer_evidence_sufficiency(task, events)
            if mandatory_answer_completeness else None
        )
        spec_kinds = {item.verification_kind for item in task_spec.acceptance_criteria}
        mandatory_post_mutation = bool(
            task.mutation_journal
            and _task_spec_requires_command_verification(task_spec)
            and TaskCriterionKind.POST_MUTATION_COMMAND not in spec_kinds
        )
        goal_command_result = self._verify_goal_command_outcome(
            task, task_spec.goal
        )
        question_projection = EvidenceQuestionProjector.project(task_id, events)
        evidence_questions = await self._evidence_relevant_questions(
            question_projection, events
        )
        incomplete_questions = tuple(
            record for record in evidence_questions.records
            if record.status in {
                EvidenceQuestionStatus.OPEN, EvidenceQuestionStatus.BLOCKED,
            }
        )
        final_acceptance = await self._evaluate_final_acceptance(
            task, events, evidence_questions
        )
        await self._append_events(task_id, (("verify.started", {
            "criterion_count": (
                len(task_spec.acceptance_criteria)
                + (1 if mandatory_post_mutation else 0)
                + (1 if mandatory_answer_completeness else 0)
                + (1 if answer_evidence_result is not None else 0)
                + (1 if goal_command_result is not None else 0)
                + (1 if incomplete_questions else 0)
                + (1 if final_acceptance is not None else 0)
            ),
            "mutation_count": len(task.mutation_journal),
            "task_spec_revision": task_spec.revision,
            "task_spec_hash": task_spec.content_hash,
        }),))

        criteria, deferred_semantic = seed_acceptance_criteria(
            task_spec=task_spec,
            mandatory_answer_completeness=mandatory_answer_completeness,
            answer_result=answer_result,
            answer_evidence_result=answer_evidence_result,
            goal_command_result=goal_command_result,
            incomplete_questions=incomplete_questions,
            final_acceptance=final_acceptance,
        )
        workspace_evidence: list[Evidence] = []
        root = Path(task.workspace)
        active_mutations: dict[str, MutationRecord] = {}
        reverted = {
            mutation.reverts_mutation_id for mutation in task.mutation_journal
            if mutation.reverts_mutation_id is not None
        }
        for mutation in task.mutation_journal:
            if mutation.mutation_id not in reverted:
                active_mutations[mutation.path] = mutation
        for path, mutation in sorted(active_mutations.items()):
            try:
                resolved = self._dependencies.workspace_path.resolve_mutation_path(
                    root, path
                ).path
                actual_hash = file_sha256(resolved) if resolved.exists() else None
                passed = actual_hash == mutation.after_hash
            except (OSError, ValueError):
                actual_hash = None
                passed = False
            workspace_evidence.append(Evidence(
                "workspace_hash",
                "current workspace state matches the committed Mutation Journal",
                (
                    "current hash matches journal" if passed
                    else "current hash differs from journal"
                ),
                mutation.step_id, passed, path,
            ))
        # Integrity only: "did the disk keep what the journal recorded?" An
        # empty journal trivially satisfies it. Whether a change was *required*
        # is the delivery criterion's business, not this one's.
        workspace_passed = all(item.passed for item in workspace_evidence)
        workspace_result = AcceptanceResult(
            "workspace-integrity",
            (AcceptanceStatus.PASSED if workspace_passed
             else AcceptanceStatus.FAILED),
            tuple(workspace_evidence) or (Evidence(
                "workspace_hash", "task introduced no workspace mutation",
                "no mutation required verification", "verifier", True,
            ),),
        )

        command_result: AcceptanceResult | None = None
        if (
            task.mutation_journal
            and _task_spec_requires_command_verification(task_spec)
        ):
            latest_mutation = max(
                task.mutation_journal, key=lambda item: item.created_at
            )
            verification_runs = [
                execution for execution in task.tool_executions.values()
                if execution.call.name == "core.run_command"
                and is_verification_request(execution.call.arguments)
                and execution.updated_at >= latest_mutation.created_at
                and execution.state is ToolCommitState.COMMITTED
                and execution.result is not None and execution.result.ok
                and isinstance(execution.result.data, Mapping)
                and execution.result.data.get("mode") == "foreground"
            ]
            latest_run = max(
                verification_runs, key=lambda item: item.updated_at, default=None
            )
            command_passed = bool(
                latest_run is not None and latest_run.result is not None
                and isinstance(latest_run.result.data, Mapping)
                and process_result_succeeded(latest_run.result.data)
            )
            command_result = AcceptanceResult(
                "post-mutation-command",
                AcceptanceStatus.PASSED if command_passed else (
                    AcceptanceStatus.FAILED if latest_run is not None
                    else AcceptanceStatus.BLOCKED
                ),
                (Evidence(
                    "process_exit",
                    "a foreground build/test command succeeded after the last mutation",
                    (
                        "exit_code=0 after mutation" if command_passed
                        else "latest post-mutation command did not exit successfully"
                        if latest_run is not None else "no post-mutation command recorded"
                    ),
                    (latest_run.invocation_id
                     if latest_run is not None else "verifier"),
                    command_passed,
                ),),
            )

        for criterion in task_spec.acceptance_criteria:
            if criterion.verification_kind is TaskCriterionKind.WORKSPACE_INTEGRITY:
                criteria.append(AcceptanceResult(
                    criterion.criterion_id, workspace_result.status,
                    tuple(Evidence(
                        item.kind, criterion.description, item.observed,
                        item.source_step_id, item.passed, item.artifact_path,
                    ) for item in workspace_result.evidence),
                ))
            elif criterion.verification_kind is TaskCriterionKind.POST_MUTATION_COMMAND:
                if not _has_active_workspace_mutation(task):
                    criteria.append(AcceptanceResult(
                        criterion.criterion_id, AcceptanceStatus.NOT_APPLICABLE,
                        (Evidence(
                            "process_exit", criterion.description,
                            "not applicable: task produced no surviving "
                            "workspace mutation",
                            "verifier", True,
                        ),),
                    ))
                else:
                    # Per criterion: a bound criterion is decided by its own
                    # declared command, so two criteria with different bindings
                    # cannot be collapsed into one shared result.
                    verification = _post_mutation_verification(
                        task, criterion.command,
                    )
                    if verification.passed:
                        status = AcceptanceStatus.PASSED
                        observed = "exit_code=0 after mutation"
                    elif verification.latest_argv:
                        status = AcceptanceStatus.FAILED
                        observed = (
                            "latest post-mutation command did not exit successfully"
                        )
                    else:
                        status = AcceptanceStatus.BLOCKED
                        observed = "no post-mutation command recorded"
                    criteria.append(AcceptanceResult(
                        criterion.criterion_id, status,
                        (Evidence(
                            "process_exit", criterion.description, observed,
                            verification.latest_reference or "verifier",
                            status is AcceptanceStatus.PASSED,
                        ),),
                    ))
            elif criterion.verification_kind is TaskCriterionKind.REQUIRED_EFFECT:
                # Completion-claim gate: the contract declares side effects, so a
                # Task that produced none is missing required delivery -- the
                # claim fails here rather than passing as "nothing to verify".
                criteria.append(_required_effect_delivery_result(
                    criterion.criterion_id, task, task_spec,
                ))
            elif criterion.verification_kind in {
                TaskCriterionKind.RUBRIC, TaskCriterionKind.GOAL_ALIGNMENT,
            }:
                # Deferred to the semantic stage below: a judgement criterion has
                # no machine proof, and its cost must never be paid before the
                # deterministic verdict is known.
                deferred_semantic.append(criterion)
                continue
            else:
                reference = criterion.evidence_reference or ""
                evidence = self._verify_task_spec_reference(
                    task, events, reference, criterion.description
                )
                criteria.append(AcceptanceResult(
                    criterion.criterion_id,
                    AcceptanceStatus.PASSED if evidence.passed
                    else AcceptanceStatus.BLOCKED,
                    (evidence,),
                ))

        if mandatory_post_mutation and command_result is not None:
            criteria.append(command_result)

        # Stage 3 (semantic). Runs only now, so the deterministic verdict is
        # already known. INV-10 still holds either way: a judged criterion that
        # was not evaluated is BLOCKED -- undetermined -- never passed.
        deterministic_passed = all(
            item.status is AcceptanceStatus.PASSED for item in criteria
        )
        for criterion in deferred_semantic:
            if not include_semantic:
                criteria.append(AcceptanceResult(
                    criterion.criterion_id, AcceptanceStatus.BLOCKED,
                    (Evidence(
                        "rubric_deferred", criterion.description,
                        "the semantic stage was skipped for this read-only "
                        "call; the criterion is undetermined, not passed",
                        "rubric-judge", False,
                    ),),
                ))
                continue
            if not deterministic_passed:
                criteria.append(AcceptanceResult(
                    criterion.criterion_id, AcceptanceStatus.BLOCKED,
                    (Evidence(
                        "rubric_not_reached", criterion.description,
                        "the deterministic stage did not pass, so this "
                        "judgement criterion was not evaluated; the criterion is "
                        "undetermined, not passed",
                        "rubric-judge", False,
                    ),),
                ))
                continue
            if self._dependencies.rubric_judge is None:
                criteria.append(AcceptanceResult(
                    criterion.criterion_id, AcceptanceStatus.BLOCKED,
                    (Evidence(
                        "rubric_unavailable", criterion.description,
                        "no rubric judge is configured; a judged criterion "
                        "cannot pass",
                        "rubric-judge", False,
                    ),),
                ))
                continue
            judgement, referenced_ids = await self._judge_rubric_criterion(
                task, events, criterion, answer_reference,
                shared_observation_refs,
            )
            # SPEC §6.2: an affirmation that cites nothing it read is not a pass.
            outcome = normalize_judgement(
                obligation_ids=(),
                verdict=judgement.verdict,
                reason=judgement.reason,
                # Auto-attribution belongs here, where the packet is known: a
                # judge that affirms what it was handed cites what it was handed,
                # and a second implementation of this rule inside each adapter
                # would be the same decision maintained twice. An explicit
                # citation still wins, so naming evidence can only add.
                cited_evidence_ids=(
                    judgement.cited_evidence_ids or referenced_ids
                ),
                known_evidence_ids=frozenset(referenced_ids),
            )
            passed = outcome.satisfied
            criteria.append(AcceptanceResult(
                criterion.criterion_id,
                (AcceptanceStatus.PASSED if passed
                 else AcceptanceStatus.BLOCKED),
                (Evidence(
                    # The disposition (SATISFIED / NEEDS_REWORK / NEEDS_REVIEW)
                    # is part of the audit trail: a bounded rework request and an
                    # undecidable review are different outcomes even though both
                    # withhold success here.
                    f"rubric:{outcome.disposition}",
                    criterion.description, outcome.reason,
                    "rubric-judge", passed,
                ),),
            ))

        status = AcceptanceStatus.PASSED
        if any(item.status is AcceptanceStatus.FAILED for item in criteria):
            status = AcceptanceStatus.FAILED
        elif any(item.status is AcceptanceStatus.BLOCKED for item in criteria):
            status = AcceptanceStatus.BLOCKED
        evidence_level = None
        evaluator = self._dependencies.evidence_level_evaluator
        if evaluator is not None:
            counts = {category: 0 for category in (
                "new_paths", "new_symbols", "new_relations",
                "new_facts", "new_exclusions", "resolved_questions",
                "new_verification",
            )}
            for event in events:
                if event.event_type != "evidence.delta_evaluated":
                    continue
                raw_counts = event.payload.get("counts")
                if not isinstance(raw_counts, Mapping):
                    continue
                for category in counts:
                    value = raw_counts.get(category, 0)
                    if isinstance(value, int) and value > 0:
                        counts[category] += value
            criterion_by_id = {
                item.criterion_id: item for item in task_spec.acceptance_criteria
            }
            passed_reference_count = sum(
                item.status is AcceptanceStatus.PASSED
                and criterion_by_id.get(item.criterion_id) is not None
                and criterion_by_id[item.criterion_id].verification_kind
                is TaskCriterionKind.EVIDENCE_REFERENCE
                for item in criteria
            )
            passed_command_count = sum(
                item.status is AcceptanceStatus.PASSED
                and (
                    item.criterion_id == "post-mutation-command"
                    or (
                        criterion_by_id.get(item.criterion_id) is not None
                        and criterion_by_id[item.criterion_id].verification_kind
                        is TaskCriterionKind.POST_MUTATION_COMMAND
                    )
                )
                for item in criteria
            )
            evidence_level = await evaluator.assess(EvidenceLevelSignals(
                evidence_counts=counts,
                successful_tool_calls=sum(
                    execution.state is ToolCommitState.COMMITTED
                    and execution.result is not None and execution.result.ok
                    for execution in task.tool_executions.values()
                ),
                verification_status=status.value,
                passed_evidence_references=passed_reference_count,
                passed_post_mutation_commands=passed_command_count,
                failed_criteria=sum(
                    item.status is AcceptanceStatus.FAILED for item in criteria
                ),
                blocked_criteria=sum(
                    item.status is AcceptanceStatus.BLOCKED for item in criteria
                ),
            ))
        result = TaskVerificationResult(
            task_id, status, tuple(criteria), evidence_level
        )
        for criterion in result.criteria:
            await self._append_events(task_id, (("verify.criterion_completed", {
                "criterion_id": criterion.criterion_id,
                "status": criterion.status.value,
                "evidence": [item.to_data() for item in criterion.evidence],
            }),))
        await self._append_events(task_id, (("verify.completed", {
            "status": result.status.value,
            "criterion_count": len(result.criteria),
        }),))
        if result.evidence_level is not None:
            await self._append_events(task_id, ((
                "evidence.level_assessed", result.evidence_level.to_data()
            ),))
        return result

    async def _evidence_relevant_questions(
        self, projection: EvidenceQuestionProjection,
        events: tuple[RuntimeEvent, ...],
    ) -> EvidenceQuestionProjection:
        """Exclude legacy questions created for non-evidence protocols.

        Older checkpoints wrapped every Tool Call in an Evidence Question,
        including user interaction.  ToolSpec result authority is now the source
        of truth.  Unknown tools remain conservative; only records whose every
        bound call belongs to a known non-evidence tool are excluded.
        """
        specs = {spec.name: spec for spec in await self.list_tools()}
        tool_name_by_call = {
            str(event.payload.get("tool_call_id", "")):
            str(event.payload.get("tool_name", ""))
            for event in events
            if event.event_type == "evidence.question_bound"
        }
        relevant = []
        for record in projection.records:
            known_specs = []
            has_unknown = False
            for call_id in record.tool_call_ids:
                name = tool_name_by_call.get(call_id, "")
                spec = specs.get(name)
                if spec is None:
                    has_unknown = True
                else:
                    known_specs.append(spec)
            if has_unknown or not known_specs or any(
                spec.requires_evidence_question for spec in known_specs
            ):
                relevant.append(record)
        return replace(projection, records=tuple(relevant))

    async def _evaluate_final_acceptance(
        self, task: TaskSnapshot, events: tuple[RuntimeEvent, ...],
        questions: EvidenceQuestionProjection,
    ) -> FinalAcceptanceDecision | None:
        """Verify final traceability using only durable structured facts."""
        policy = self._dependencies.final_acceptance_policy
        if policy is None:
            return None
        scope_relations = {
            str(event.payload.get("tool_call_id", "")): str(
                event.payload.get("relation", "")
            )
            for event in events
            if event.event_type == "tool.scope_consistency_evaluated"
        }
        executions_by_call = {
            execution.call.call_id: execution
            for execution in task.tool_executions.values()
        }
        question_facts: list[FinalQuestionEvidence] = []
        for record in questions.records:
            if record.status is EvidenceQuestionStatus.DROPPED:
                continue
            trusted: list[str] = []
            wrong_scope: list[str] = []
            for call_id in record.tool_call_ids:
                reference = f"tool_call:{call_id}"
                execution = executions_by_call.get(call_id)
                relation = scope_relations.get(call_id, "")
                is_scoped_file_call = bool(
                    execution is not None
                    and execution.call.name in {
                        "core.read_file", "core.list_files",
                        "core.find_files", "core.search_text",
                        "core.grep_search",
                    }
                    and record.expected_scope.strip()
                )
                if relation == ToolScopeRelation.MISMATCH.value:
                    wrong_scope.append(reference)
                    continue
                if (
                    execution is not None
                    and execution.state is ToolCommitState.COMMITTED
                    and execution.result is not None
                    and execution.result.ok
                    and (
                        not is_scoped_file_call
                        or relation == ToolScopeRelation.MATCH.value
                    )
                ):
                    trusted.append(reference)
            question_facts.append(FinalQuestionEvidence(
                record.question_ref, record.status.value, record.expected_scope,
                tuple(trusted), tuple(wrong_scope),
                record.blocking_reason or "",
            ))

        # Reserved seam: the probe field stays for a future runtime-owned plan
        # contract, but the model's scratchpad is no longer a delivery contract.
        # See 任务证明闭环实施SPEC.md §2 (INV-1).
        required_plan_steps: tuple[str, ...] = ()
        readiness_events = [
            event for event in events
            if event.event_type == "completion.readiness_evaluated"
        ]
        completion_gaps: tuple[str, ...] = ()
        if readiness_events:
            raw_gaps = readiness_events[-1].payload.get("gaps", [])
            if isinstance(raw_gaps, list):
                completion_gaps = tuple(
                    str(item.get("gap_id", ""))
                    for item in raw_gaps if isinstance(item, Mapping)
                    and bool(item.get("required", True))
                    and str(item.get("gap_id", "")).strip()
                )
        probe = FinalAcceptanceProbe(
            tuple(question_facts), required_plan_steps, completion_gaps
        )
        try:
            decision = await policy.evaluate(probe)
        except Exception as error:
            decision = FinalAcceptanceDecision(
                FinalAcceptanceAction.BLOCK, "final_acceptance_policy_failed",
                (FinalAcceptanceViolation(
                    "POLICY_FAILURE", "final-acceptance-policy",
                    f"policy failed: {type(error).__name__}",
                ),),
            )
        await self._append_events(task.task_id, ((
            "verify.final_evidence_evaluated", {
                "action": decision.action.value, "reason": decision.reason,
                "question_count": len(question_facts),
                "required_plan_step_count": len(required_plan_steps),
                "completion_gap_count": len(completion_gaps),
                "violations": [
                    {
                        "code": item.code,
                        "subject_ref": item.subject_ref,
                        "observed": item.observed,
                    }
                    for item in decision.violations
                ],
            },
        ),))
        return decision

    @staticmethod
    def _verify_goal_command_outcome(
        task: TaskSnapshot, goal: str,
    ) -> AcceptanceResult | None:
        """Verify an exact direct-command goal from the persisted execution ledger.

        P0 deliberately avoids guessing intent from natural-language tasks. The
        criterion exists only when a core.run_command argv exactly represents
        the complete Task goal. If the same command is retried, the latest
        durable attempt decides the outcome.
        """
        attempts = [
            execution for execution in task.tool_executions.values()
            if execution.call.name == "core.run_command"
            and _goal_matches_command_argv(goal, execution.call.arguments)
        ]
        if not attempts:
            return None
        latest = max(attempts, key=lambda item: item.updated_at)
        result = latest.result
        error_code = result.error_code if result is not None else None
        status = AcceptanceStatus.BLOCKED
        observed = "direct command has no trustworthy terminal result"
        passed = False

        if latest.state is ToolCommitState.COMMITTED and result is not None:
            data = result.data if isinstance(result.data, Mapping) else {}
            mode = str(data.get("mode", "foreground"))
            if mode == "background":
                started = data.get("started")
                passed = bool(
                    result.ok and bool(data.get("process_id"))
                    and (started if isinstance(started, bool) else True)
                )
                status = (
                    AcceptanceStatus.PASSED
                    if passed else AcceptanceStatus.FAILED
                )
                observed = (
                    "direct background command started"
                    if passed else "direct background command did not start"
                )
            else:
                passed = bool(result.ok and process_result_succeeded(data))
                status = (
                    AcceptanceStatus.PASSED
                    if passed else AcceptanceStatus.FAILED
                )
                observed = (
                    "direct command exited with code 0"
                    if passed else
                    f"direct command exit status={data.get('status', 'unknown')}; "
                    f"exit_code={data.get('exit_code', 'unknown')}"
                )
        elif latest.state is ToolCommitState.FAILED:
            status = (
                AcceptanceStatus.BLOCKED
                if error_code == "PERMISSION_DENIED"
                else AcceptanceStatus.FAILED
            )
            observed = (
                f"direct command was not executed: {error_code or 'TOOL_FAILED'}"
                if status is AcceptanceStatus.BLOCKED else
                f"direct command failed: {error_code or 'TOOL_FAILED'}"
            )
        elif latest.state in {
            ToolCommitState.CANCELLED, ToolCommitState.UNKNOWN_OUTCOME,
            ToolCommitState.PREPARED, ToolCommitState.RUNNING,
        }:
            observed = f"direct command outcome is {latest.state.value}"

        return AcceptanceResult(
            "goal-command-outcome", status,
            (Evidence(
                "process_outcome",
                "the command requested as the complete Task goal succeeded",
                observed, latest.invocation_id, passed,
            ),),
        )

    @staticmethod
    def _verify_answer_completeness(
        events: tuple[RuntimeEvent, ...],
    ) -> AcceptanceResult:
        """Prove the completed Turn ended in user-facing text, not protocol."""
        completed_turns = [
            event for event in events if event.event_type == "turn.completed"
        ]
        latest_turn_id = (
            str(completed_turns[-1].payload.get("turn_id", ""))
            if completed_turns else ""
        )
        model_events = [
            event for event in events
            if event.event_type == "llm.completed"
            and str(event.payload.get("turn_id", "")) == latest_turn_id
        ]
        message = model_events[-1].payload.get("message") if model_events else None
        content = message.get("content") if isinstance(message, Mapping) else None
        text_parts: list[str] = []
        has_tool_call = False
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                block_type = block.get("type")
                if block_type == "text" and isinstance(block.get("text"), str):
                    text_parts.append(str(block["text"]))
                elif block_type == "tool_call":
                    has_tool_call = True
        text = "".join(text_parts).strip()
        protocol_markup = Kernel._looks_like_exact_text_tool_call(text)
        passed = bool(text) and not has_tool_call and not protocol_markup
        if passed:
            observed = "final assistant message contains user-facing text"
        elif has_tool_call:
            observed = "completed Turn still contains a pending tool call"
        elif protocol_markup:
            observed = "final assistant message is tool protocol markup"
        else:
            observed = "completed Turn has no user-facing assistant text"
        return AcceptanceResult(
            "answer-completeness",
            AcceptanceStatus.PASSED if passed else AcceptanceStatus.FAILED,
            (Evidence(
                "answer_structure",
                "completed Agent Turn ends with a user-facing answer",
                observed,
                (
                    f"model-event:{model_events[-1].sequence}"
                    if model_events else "verifier"
                ),
                passed,
            ),),
        )

    @staticmethod
    def _verify_answer_evidence_sufficiency(
        task: TaskSnapshot, events: tuple[RuntimeEvent, ...],
    ) -> AcceptanceResult | None:
        """Block a self-declared unverified answer after failed artifact reads.

        This deliberately avoids inferring the project type or the user's domain. It
        uses only two persisted facts from the latest Turn: every attempted artifact
        read failed, and the final answer itself says the needed material was not read
        or verified. A successful read in the same Turn disables this conservative
        gate because the Verifier cannot infer which artifact was decisive.
        """
        completed = [
            event for event in events if event.event_type == "turn.completed"
        ]
        if not completed:
            return None
        turn_id = str(completed[-1].payload.get("turn_id", ""))
        reads = [
            execution for execution in task.tool_executions.values()
            if execution.turn_id == turn_id
            and execution.call.name == "core.read_file"
        ]
        if not reads:
            return None
        successful = [
            execution for execution in reads
            if execution.state is ToolCommitState.COMMITTED
            and execution.result is not None and execution.result.ok
        ]
        failed = [
            execution for execution in reads
            if execution.state is ToolCommitState.FAILED
            or (execution.result is not None and not execution.result.ok)
        ]
        if successful or not failed:
            return None
        model_events = [
            event for event in events
            if event.event_type == "llm.completed"
            and str(event.payload.get("turn_id", "")) == turn_id
        ]
        message = model_events[-1].payload.get("message") if model_events else None
        content = message.get("content") if isinstance(message, Mapping) else None
        text = "".join(
            str(block.get("text", ""))
            for block in content if isinstance(block, Mapping)
            and block.get("type") == "text"
        ).strip() if isinstance(content, list) else ""
        if not text:
            return None
        unverified = re.search(
            r"(?:没法|无法|不能|未能|还没|尚未|没有)(?:继续)?(?:成功)?"
            r"(?:读取|读到|看到|查看|获得|验证|确认|检查|访问)|"
            r"(?:could\s+not|couldn't|cannot|can't|unable\s+to|"
            r"have\s+not|haven't|did\s+not|didn't)\s+(?:successfully\s+)?"
            r"(?:read|inspect|access|verify|confirm|check)",
            text, re.IGNORECASE,
        )
        if unverified is None:
            return None
        error_codes = sorted({
            execution.result.error_code or "UNKNOWN"
            for execution in failed if execution.result is not None
        })
        observed = (
            "final answer explicitly says required material was not read or "
            f"verified; latest Turn has {len(failed)} failed artifact read(s), "
            f"no successful artifact read, errors={','.join(error_codes) or 'UNKNOWN'}"
        )
        return AcceptanceResult(
            "answer-evidence-sufficiency", AcceptanceStatus.BLOCKED,
            (Evidence(
                "answer_evidence_sufficiency",
                "a final behavior or implementation answer is backed by a successful "
                "artifact read when the answer says source evidence is required",
                observed,
                (f"model-event:{model_events[-1].sequence}"
                 if model_events else "verifier"),
                False,
            ),),
        )

    async def _judge_rubric_criterion(
        self, task: TaskSnapshot, events: tuple[RuntimeEvent, ...],
        criterion: TaskAcceptanceCriterion,
        answer_reference: str, observation_refs: tuple[str, ...],
    ) -> tuple[RubricJudgement, tuple[str, ...]]:
        """Evaluate one judgement criterion under a bounded attempt budget.

        A judge that keeps erroring is not retried forever; every attempt is
        recorded and the last verdict is returned for final acceptance. The
        judge receives bounded excerpts, not opaque references, or it would be
        judging blind.
        """
        judge = self._dependencies.rubric_judge
        references = tuple(dict.fromkeys(
            ([answer_reference] if answer_reference else [])
            + list(observation_refs)
        ))
        # The goal is always evidence a judge may read: a judgement criterion is
        # by definition about *this* request, and a Task with no tool execution and
        # no answer event would otherwise hand the judge an empty packet -- which
        # ``normalize_judgement`` turns into "affirmed without citing evidence",
        # i.e. an automatic NEEDS_REVIEW for a criterion nobody could judge.
        evidence = (
            RubricEvidence("task-goal", task.goal),
        ) + self._rubric_evidence(task, events, references)
        attempts = max(1, self._dependencies.rubric_judge_max_attempts)
        judgement = RubricJudgement(
            criterion.criterion_id, JudgeVerdict.UNDECIDABLE,
            "rubric judge was not invoked",
        )
        for attempt in range(1, attempts + 1):
            assert judge is not None
            judgement = await judge.judge(
                criterion.criterion_id, criterion.description, evidence,
            )
            await self._append_events(task.task_id, ((
                "rubric.evaluated", {
                    "attempt": attempt,
                    "evidence_count": len(evidence),
                    **judgement.to_data(),
                },
            ),))
            if judgement.verdict is not JudgeVerdict.JUDGE_ERROR:
                break
        return judgement, tuple(item.reference for item in evidence)

    def _rubric_evidence(
        self, task: TaskSnapshot, events: tuple[RuntimeEvent, ...],
        references: tuple[str, ...],
    ) -> tuple[RubricEvidence, ...]:
        """Resolve evidence references into bounded text a judge can read."""
        limit = self._dependencies.rubric_evidence_excerpt_characters
        maximum = self._dependencies.rubric_evidence_max_items
        executions = {
            execution.call.call_id: execution
            for execution in task.tool_executions.values()
        }
        mutations = {
            mutation.mutation_id: mutation
            for mutation in task.mutation_journal
        }
        resolved: list[RubricEvidence] = []
        for reference in references:
            excerpt: str | None = None
            if reference.startswith("event:"):
                try:
                    sequence = int(reference.removeprefix("event:"))
                except ValueError:
                    sequence = -1
                event = next(
                    (item for item in events if item.sequence == sequence), None,
                )
                if event is not None:
                    excerpt = self._event_excerpt(event)
            elif reference.startswith("tool_call:"):
                execution = executions.get(
                    reference.removeprefix("tool_call:")
                )
                if execution is not None and execution.result is not None:
                    excerpt = json.dumps(
                        execution.result.data,
                        ensure_ascii=False, sort_keys=True, default=str,
                    )
            elif reference.startswith("mutation:"):
                mutation = mutations.get(reference.removeprefix("mutation:"))
                if mutation is not None:
                    excerpt = (
                        f"path={mutation.path} "
                        f"before={mutation.before_hash} "
                        f"after={mutation.after_hash}"
                    )
            if excerpt is None:
                continue
            resolved.append(RubricEvidence(reference, excerpt[:limit]))
            if len(resolved) >= maximum:
                break
        return tuple(resolved)


    def _verify_task_spec_reference(
        self, task: TaskSnapshot, events: tuple[RuntimeEvent, ...],
        reference: str, assertion: str,
    ) -> Evidence:
        """Validate a Task SPEC reference against persisted trusted state."""
        if reference.startswith("event:"):
            try:
                sequence = int(reference.removeprefix("event:"))
            except ValueError:
                sequence = -1
            event = next((item for item in events if item.sequence == sequence), None)
            return Evidence(
                "event_reference", assertion,
                (f"event_type={event.event_type}" if event else "event not found"),
                reference, event is not None,
            )
        if reference.startswith("tool_call:"):
            call_id = reference.removeprefix("tool_call:")
            execution = next((
                item for item in task.tool_executions.values()
                if item.call.call_id == call_id
            ), None)
            passed = bool(
                execution is not None
                and execution.state is ToolCommitState.COMMITTED
                and execution.result is not None and execution.result.ok
            )
            return Evidence(
                "tool_result", assertion,
                ("committed successful tool result" if passed
                 else "successful committed tool result not found"),
                reference, passed,
            )
        if reference.startswith("mutation:"):
            mutation_id = reference.removeprefix("mutation:")
            mutation = next((
                item for item in task.mutation_journal
                if item.mutation_id == mutation_id
            ), None)
            passed = False
            if mutation is not None:
                try:
                    path = self._dependencies.workspace_path.resolve_mutation_path(
                        Path(task.workspace), mutation.path
                    ).path
                    actual_hash = file_sha256(path) if path.exists() else None
                    passed = actual_hash == mutation.after_hash
                except (OSError, ValueError):
                    passed = False
            return Evidence(
                "mutation_reference", assertion,
                ("mutation exists and current hash matches" if passed
                 else "matching current mutation not found"),
                reference, passed, mutation.path if mutation else None,
            )
        return Evidence(
            "invalid_reference", assertion,
            "reference must use event:, tool_call:, or mutation:",
            reference or "verifier", False,
        )


    async def run_text_turn(
        self, task_id: str, user_text: str, max_output_tokens: int | None = None
    ) -> TurnResult:
        normalized_text = user_text.strip()
        max_output_tokens = (
            self._dependencies.default_max_output_tokens
            if max_output_tokens is None else max_output_tokens
        )
        if not normalized_text:
            raise ValueError("user text must not be empty")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")

        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        task = TaskSnapshot.from_data(stored.data)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        turn_id = f"turn-{uuid4().hex}"
        user_message = Message(
            message_id=f"msg-{uuid4().hex}",
            role=MessageRole.USER,
            content=(TextBlock(normalized_text),),
        )
        started = RuntimeEvent(
            event_id=f"evt-{uuid4().hex}",
            task_id=task_id,
            sequence=stored.last_event_sequence + 1,
            event_type="turn.started",
            payload={
                "turn_id": turn_id,
                "input_role": user_message.role.value,
                "input_character_count": len(normalized_text),
                "max_output_tokens": max_output_tokens,
            },
        )
        await self._dependencies.store.commit(
            RuntimeUnitOfWork(
                task_id=task_id,
                expected_version=stored.version,
                next_state=task.to_data(),
                events=(started,),
            )
        )

        try:
            project_context = await self._project_context_messages(task_id)
            # Keep the current user goal first for ContextWindowManager's protected
            # first-message rule. PromptTemplate deterministically places tagged
            # memory immediately before ordinary conversation at assembly time.
            conversation = (user_message,) + project_context
            prepared = self._dependencies.context_manager.prepare(
                conversation=conversation, tools=(),
                prompt_template=self._dependencies.prompt_template,
                context_window=self._dependencies.model.capabilities.context_window,
                max_output_tokens=max_output_tokens,
                runtime_instruction=_temporal_context_instruction(),
            )
        except ContextWindowExceeded as error:
            await self._record_turn_failure(task_id, turn_id, error)
            raise ModelInvocationFailed(turn_id, str(error)) from error
        prompt = self._dependencies.prompt_template.assemble(
            prepared.messages, (), _temporal_context_instruction()
        )
        request = ModelRequest(
            turn_id=turn_id,
            messages=prompt.messages,
            max_output_tokens=max_output_tokens,
            conclusion_protocol_mode=(
                ConclusionProtocolMode.REQUIRE_STRUCTURED
                if self._dependencies.model.capabilities.structured_conclusion
                else ConclusionProtocolMode.OBSERVE
            ),
        )
        try:
            response = await self._dependencies.model.complete(request)
            if response.message.role is not MessageRole.ASSISTANT:
                raise InvalidModelResponse("model response message must have assistant role")
            if response.finish_reason.value not in {"stop", "length"}:
                raise InvalidModelResponse(
                    f"unsupported finish reason in text turn: {response.finish_reason.value}"
                )
        except Exception as error:
            await self._record_turn_failure(task_id, turn_id, error, prompt.receipt)
            raise ModelInvocationFailed(turn_id, str(error)) from error

        additional_events: tuple[tuple[str, Mapping[str, Any]], ...] = ()
        if prepared.compaction is not None:
            additional_events = (("context.compacted", {
                "turn_id": turn_id, "model_call": 1,
                **prepared.compaction.event_data(),
            }),)
        await self._commit_final_agent_result(
            task_id=task_id, turn_id=turn_id, user_message=user_message,
            assistant_message=response.message,
            llm_payload={
                "turn_id": turn_id,
                "message": response.message.to_data(),
                "finish_reason": response.finish_reason.value,
                "usage": response.usage.to_data(),
                "response_diagnostics": self._response_diagnostics(response),
                "context_window": prepared.budget.context_window,
                "context_estimated_input_tokens": prepared.budget.estimated_input_tokens,
                "context_estimation_method": prepared.budget.estimation_method,
                "context_budget": prepared.budget.event_data(),
                **prompt.receipt.event_data(),
            },
            additional_events=additional_events,
            clear_agent_checkpoint=False,
        )
        return TurnResult(
            turn_id=turn_id, task_id=task_id,
            assistant_message=response.message,
            finish_reason=response.finish_reason, usage=response.usage,
        )

        after_model = await self._dependencies.store.load_task(task_id)
        if after_model is None:
            raise TaskNotFound(f"task disappeared during turn: {task_id}")
        next_sequence = after_model.last_event_sequence + 1
        completed_events_list = [RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=next_sequence,
                event_type="llm.completed",
                payload={
                    "turn_id": turn_id,
                    "message": response.message.to_data(),
                    "finish_reason": response.finish_reason.value,
                    "usage": response.usage.to_data(),
                    "response_diagnostics": self._response_diagnostics(response),
                    "context_window": prepared.budget.context_window,
                    "context_estimated_input_tokens": (
                        prepared.budget.estimated_input_tokens
                    ),
                    "context_estimation_method": (
                        prepared.budget.estimation_method
                    ),
                    "context_budget": prepared.budget.event_data(),
                    **prompt.receipt.event_data(),
                },
            )]
        if prepared.compaction is not None:
            completed_events_list.append(RuntimeEvent(
                event_id=f"evt-{uuid4().hex}", task_id=task_id,
                sequence=next_sequence + len(completed_events_list),
                event_type="context.compacted",
                payload={
                    "turn_id": turn_id, "model_call": 1,
                    **prepared.compaction.event_data(),
                },
            ))
        completed_events_list.append(RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=next_sequence + len(completed_events_list),
                event_type="turn.completed",
                payload={"turn_id": turn_id},
            ))
        completed_events = tuple(completed_events_list)
        await self._dependencies.store.commit(
            RuntimeUnitOfWork(
                task_id=task_id,
                expected_version=after_model.version,
                next_state=after_model.data,
                events=completed_events,
            )
        )
        await self._record_session_task_result(
            task_id, turn_id, user_message, response.message
        )
        return TurnResult(
            turn_id=turn_id,
            task_id=task_id,
            assistant_message=response.message,
            finish_reason=response.finish_reason,
            usage=response.usage,
        )

    async def run_agent_turn(
        self,
        task_id: str,
        user_text: str,
        user_blocks: tuple[MessageBlock, ...] | None = None,
        max_model_calls: int | None = None,
        max_tool_calls: int | None = None,
        max_output_tokens: int | None = None,
        tool_timeout_seconds: float = 30.0,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ) -> AgentTurnResult | AgentTurnSuspended | AgentClarificationSuspended:
        normalized_text = user_text.strip()
        max_model_calls = (
            self._dependencies.default_max_model_calls
            if max_model_calls is None else max_model_calls
        )
        max_tool_calls = (
            self._dependencies.default_max_tool_calls
            if max_tool_calls is None else max_tool_calls
        )
        max_output_tokens = (
            self._dependencies.default_max_output_tokens
            if max_output_tokens is None else max_output_tokens
        )
        if not normalized_text:
            raise ValueError("user text must not be empty")
        if max_model_calls <= 0:
            raise ValueError("max_model_calls must be positive")
        if max_tool_calls <= 0:
            raise ValueError("max_tool_calls must be positive")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds must be positive")

        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        task = TaskSnapshot.from_data(stored.data)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        turn_id = f"turn-{uuid4().hex}"
        user_message = Message(
            message_id=f"msg-{uuid4().hex}",
            role=MessageRole.USER,
            content=user_blocks or (TextBlock(normalized_text),),
        )
        visible_tools = await self.list_tools()
        capabilities = self._dependencies.model.capabilities
        if visible_tools and not capabilities.tools:
            raise ProviderCapabilityMismatch(
                "selected model provider does not support tools, but the Agent turn "
                f"exposes {len(visible_tools)} tool(s)"
            )
        await self._append_events(
            task_id,
            (
                (
                    "turn.started",
                    {
                        "turn_id": turn_id,
                        "input_role": user_message.role.value,
                        "input_character_count": len(normalized_text),
                        "max_model_calls": max_model_calls,
                        "max_tool_calls": max_tool_calls,
                        "max_output_tokens": max_output_tokens,
                        "visible_tools": [tool.name for tool in visible_tools],
                        "provider_capabilities": capabilities.to_data(),
                    },
                ),
            ),
        )

        project_context = await self._project_context_messages(task_id)
        initial_messages = (user_message,) + project_context
        checkpoint = AgentTurnCheckpoint(
            task_id=task_id,
            turn_id=turn_id,
            revision=1,
            messages=initial_messages,
            pending_tool_calls=(),
            seen_call_ids=(),
            model_calls=0,
            tool_calls=0,
            input_tokens=0,
            output_tokens=0,
            max_model_calls=max_model_calls,
            max_tool_calls=max_tool_calls,
            max_output_tokens=max_output_tokens,
            tool_timeout_seconds=tool_timeout_seconds,
        )
        checkpoint = await self._bind_agent_checkpoint(task, checkpoint, visible_tools)
        await self._save_agent_checkpoint(checkpoint, "turn-started")
        return await self._continue_agent_turn(
            checkpoint, visible_tools, on_text_delta=on_text_delta,
            on_progress=on_progress,
        )

    async def resume_agent_turn(
        self,
        task_id: str,
        request_id: str,
        payload_hash: str,
        decision: ApprovalDecision,
        reason: str,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ) -> AgentTurnResult | AgentTurnSuspended | AgentClarificationSuspended:
        # Spec §5.1: a resume starts a new in-process scope generation.
        await self._close_cancellation_scope(task_id)
        await self.recover_workspace_transactions(task_id)
        await self.reconcile_background_processes(task_id)
        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        task = TaskSnapshot.from_data(stored.data)
        approval = task.pending_approval
        if approval is None or approval.agent_checkpoint is None:
            raise ApprovalNotPending(
                f"task {task_id} has no resumable Agent approval"
            )
        checkpoint = AgentTurnCheckpoint.from_data(approval.agent_checkpoint)
        if checkpoint.task_id != task_id or checkpoint.turn_id != approval.turn_id:
            raise ApprovalPayloadMismatch(
                "approval checkpoint does not belong to the pending task and turn"
            )
        visible_tools = await self.list_tools()
        await self._validate_agent_checkpoint(task, checkpoint, visible_tools)
        result = await self._resolve_approval(
            task_id, request_id, payload_hash, decision, reason,
            timeout_seconds=checkpoint.tool_timeout_seconds,
            allow_agent_checkpoint=True,
            resume_revision=checkpoint.revision + 1,
        )
        approved_call = checkpoint.pending_tool_calls[0]
        evidence_inventory, evidence_delta = await self._evaluate_evidence_delta(
            task_id, checkpoint.turn_id, approved_call, result,
            checkpoint.evidence_inventory,
        )
        read_hits_state = await self._update_read_hits_after(
            task_id, checkpoint.turn_id, approved_call, result, None,
            checkpoint.read_hits_state,
        )
        artifact_probe = self._artifact_read_probe(
            Path(task.workspace), approved_call, checkpoint.artifact_read_state
        )
        artifact_read_state = await self._update_artifact_read_after(
            task_id, checkpoint.turn_id, approved_call, result, artifact_probe,
            checkpoint.artifact_read_state,
        )
        approved_semantic_action = await self._classify_semantic_action(
            task_id, checkpoint.turn_id, approved_call
        )
        exploration_budget_state, _ = await self._update_exploration_budget_after(
            task_id, checkpoint.turn_id, approved_call, result,
            approved_semantic_action, evidence_delta,
            ExplorationBudgetObservation(0),
            checkpoint.exploration_budget_state,
        )
        if evidence_delta is not None:
            result = replace(
                result, meta={
                    **dict(result.meta),
                    "evidence_delta": {
                        "question_id": evidence_delta.question_id,
                        "has_progress": evidence_delta.has_progress,
                        "total_new": evidence_delta.total_new,
                        "counts": evidence_delta.counts,
                        "consecutive_zero_delta": (
                            evidence_delta.consecutive_zero_delta
                        ),
                    },
                },
            )
        question_projection = await self._observe_evidence_question(
            task_id, checkpoint.turn_id, approved_call, result, evidence_delta
        )
        previous_action = ActionProgressState.from_data(
            checkpoint.action_progress
        )
        resumed = AgentTurnCheckpoint(
            task_id=checkpoint.task_id,
            turn_id=checkpoint.turn_id,
            revision=checkpoint.revision + 1,
            messages=checkpoint.messages,
            pending_tool_calls=checkpoint.pending_tool_calls[1:],
            tool_batch=(
                checkpoint.tool_batch.with_pending(
                    checkpoint.pending_tool_calls[1:]
                ) if checkpoint.tool_batch is not None else None
            ),
            seen_call_ids=checkpoint.seen_call_ids,
            model_calls=checkpoint.model_calls,
            tool_calls=checkpoint.tool_calls + 1,
            input_tokens=checkpoint.input_tokens,
            output_tokens=checkpoint.output_tokens,
            max_model_calls=checkpoint.max_model_calls,
            max_tool_calls=checkpoint.max_tool_calls,
            max_output_tokens=checkpoint.max_output_tokens,
            tool_timeout_seconds=checkpoint.tool_timeout_seconds,
            model_budget_renewal_count=(
                checkpoint.model_budget_renewal_count
            ),
            model_budget_total_granted=(
                checkpoint.model_budget_total_granted
            ),
            workspace_fingerprint=checkpoint.workspace_fingerprint,
            effective_config_hash=checkpoint.effective_config_hash,
            toolset_hash=checkpoint.toolset_hash,
            prompt_manifest_hash=checkpoint.prompt_manifest_hash,
            session_id=checkpoint.session_id,
            session_context_hash=checkpoint.session_context_hash,
            working_memory_hash=(await self.get_working_memory(task_id)).content_hash,
            last_steering_inbound_sequence=(
                checkpoint.last_steering_inbound_sequence
            ),
            goal_revision=checkpoint.goal_revision,
            action_progress=(
                ActionProgressState(
                    previous_action.signature,
                    previous_action.relevant_state_hash,
                    evidence_delta.consecutive_zero_delta,
                    True,
                    previous_action.semantic_signature,
                    previous_action.semantic_family,
                ).to_data()
                if evidence_delta is not None else checkpoint.action_progress
            ),
            evidence_inventory=evidence_inventory,
            evidence_question_state=question_projection.to_data(),
            read_hits_state=read_hits_state,
            artifact_read_state=artifact_read_state,
            progressive_scope_state=checkpoint.progressive_scope_state,
            exploration_budget_state=exploration_budget_state,
            stop_or_pivot_state=checkpoint.stop_or_pivot_state,
            evidence_relation_state=checkpoint.evidence_relation_state,
            rejection_loop_state=checkpoint.rejection_loop_state,
            exploration_outcome_state=checkpoint.exploration_outcome_state,
            completion_readiness_state=checkpoint.completion_readiness_state,
        )
        tool_message = Message(
            message_id=f"msg-tool-{uuid4().hex}",
            role=MessageRole.TOOL,
            content=(ToolResultBlock(result),),
        )
        if result.error_code == "UNKNOWN_OUTCOME":
            blocked_checkpoint = replace(
                resumed, messages=resumed.messages + (tool_message,),
            )
            return await self._suspend_unknown_tool_outcome(
                blocked_checkpoint, approved_call, result
            )
        return await self._continue_agent_turn(
            resumed, visible_tools, initial_tool_message=tool_message,
            on_text_delta=on_text_delta,
            on_progress=on_progress,
        )

    async def resume_checkpointed_agent_turn(
        self, task_id: str,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ) -> AgentTurnResult | AgentTurnSuspended | AgentClarificationSuspended:
        """Resume an interrupted Agent loop without granting new authority."""
        # Spec §5.1: a resume starts a new in-process scope generation.
        await self._close_cancellation_scope(task_id)
        await self.recover_workspace_transactions(task_id)
        await self.reconcile_background_processes(task_id)
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        if task.state is TaskState.AWAITING_APPROVAL:
            raise ApprovalNotPending(
                "task is waiting for explicit approve/reject; resume cannot grant approval"
            )
        if task.state is TaskState.AWAITING_USER:
            raise ClarificationNotPending(
                "task is waiting for an answer; resume requires request_id and token"
            )
        if task.state not in {
            TaskState.EXECUTING, TaskState.INTERRUPTED, TaskState.CONFLICT,
        }:
            raise InvalidTurnState(
                f"task {task_id} cannot resume from {task.state.value}"
            )
        if task.active_agent_checkpoint is None:
            raise LookupError(f"task has no active Agent checkpoint: {task_id}")
        checkpoint = AgentTurnCheckpoint.from_data(task.active_agent_checkpoint)
        visible_tools = await self.list_tools()
        decision = await self._evaluate_checkpoint_compatibility(
            task, checkpoint, visible_tools
        )
        if (
            task.state is TaskState.INTERRUPTED
            and decision.action is CheckpointCompatibilityAction.EXACT_RESUME
        ):
            checkpoint = await self._reconcile_agent_checkpoint(
                task, checkpoint, visible_tools, decision
            )
            task = await self.get_task(task_id)
        elif decision.action is CheckpointCompatibilityAction.RECONCILE_REQUIRED:
            checkpoint = await self._reconcile_agent_checkpoint(
                task, checkpoint, visible_tools, decision
            )
            task = await self.get_task(task_id)
        elif decision.action is CheckpointCompatibilityAction.REBASE_REQUIRED:
            checkpoint = await self._rebase_agent_checkpoint(
                task, checkpoint, visible_tools, decision
            )
            task = await self.get_task(task_id)
        elif decision.action is not CheckpointCompatibilityAction.EXACT_RESUME:
            await self._mark_checkpoint_conflict(
                task.task_id, checkpoint, decision.conflict_reasons
            )
            details = (
                " (" + ", ".join(decision.conflict_reasons) + ")"
                if decision.conflict_reasons else ""
            )
            raise AgentCheckpointConflict(
                "Agent checkpoint cannot resume: "
                + decision.reason_code + details
            )
        if task.state in {TaskState.INTERRUPTED, TaskState.CONFLICT}:
            stored = await self._require_stored_task(task_id)
            current = TaskSnapshot.from_data(stored.data)
            resuming = current.transition(TaskState.RESUMING)
            executing = resuming.transition(TaskState.EXECUTING)
            events = (
                RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id,
                    stored.last_event_sequence + 1, "task.state_changed",
                    {"previous_state": current.state.value,
                     "next_state": resuming.state.value,
                     "reason": "resume checkpoint validation passed"},
                ),
                RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id,
                    stored.last_event_sequence + 2, "task.state_changed",
                    {"previous_state": resuming.state.value,
                     "next_state": executing.state.value,
                     "reason": "resume Agent turn"},
                ),
            )
            await self._dependencies.store.commit(RuntimeUnitOfWork(
                task_id, stored.version, executing.to_data(), events
            ))
        await self._append_events(task_id, (("turn.resumed", {
            "turn_id": checkpoint.turn_id,
            "previous_revision": checkpoint.revision,
            "revision": checkpoint.revision + 1,
            "checkpoint_hash": checkpoint.checkpoint_hash,
            "reason": "process restart or explicit resume",
        }),))
        resumed = replace(checkpoint, revision=checkpoint.revision + 1)
        await self._save_agent_checkpoint(resumed, "turn-resumed")
        return await self._continue_agent_turn(
            resumed, visible_tools, recover_interrupted=True,
            on_text_delta=on_text_delta,
            on_progress=on_progress,
        )


    @staticmethod
    def _recovery_batch_blocker_message(
        recovery_batch: Sequence[ToolResult],
    ) -> Message:
        """Render authoritative failed ToolResults without model paraphrasing."""
        failures = [
            {
                "call_id": result.call_id,
                "error_code": result.error_code,
                "message": result.message,
                "recovery_kind": result.effective_recovery_kind.value,
                "recovery_action": dict(result.recovery_action),
            }
            for result in recovery_batch
        ]
        return Message(
            f"tool-recovery-blocker-{uuid4().hex}", MessageRole.ASSISTANT,
            (TextBlock(json.dumps({
                "boundary": "unresolved_tool_batch",
                "instruction": (
                    "Tool execution did not complete. The Task is paused until "
                    "the next user continuation; prior assistant prose is not "
                    "evidence of completion."
                ),
                "failures": failures,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),),
        )


    async def resume_agent_continuation(
        self, task_id: str, user_text: str, *, input_id: str,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ) -> (
        AgentTurnResult | AgentTurnSuspended | AgentClarificationSuspended
        | AgentContinuationSuspended
    ):
        """Resume a model-selected completed-unit boundary without authority."""
        # Spec §5.1: a resume starts a new in-process scope generation.
        await self._close_cancellation_scope(task_id)
        normalized = user_text.strip()
        if not normalized or not input_id.strip():
            raise ValueError("continuation input and input_id are required")
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        if task.state is not TaskState.AWAITING_USER:
            raise InvalidTurnState(
                f"task {task_id} is not awaiting continuation"
            )
        if task.pending_clarification is not None:
            raise ClarificationNotPending(
                "clarification must be answered through its request protocol"
            )
        if task.active_agent_checkpoint is None:
            raise LookupError("continuation lost its active Agent checkpoint")
        checkpoint = AgentTurnCheckpoint.from_data(task.active_agent_checkpoint)
        pending = checkpoint.pending_user_action
        if pending.get("kind") != "CONTINUATION":
            raise ClarificationNotPending("task has no pending continuation")
        readiness_state = CompletionReadinessState.from_data(
            checkpoint.completion_readiness_state
        )
        if (
            readiness_state.stalled_continuations
            >= self._dependencies.completion_max_stalled_continuations
        ):
            # INV-4: never re-enter a loop that is already known to be
            # unproductive. Finalize for human review instead.
            return await self._finalize_needs_review(
                checkpoint, (), reason="stalled_continuation_refused",
                stalled=readiness_state.stalled_continuations,
                message=checkpoint.messages[-1],
            )
        # An explicit user continuation is a new authorization to spend, so the
        # resumed Turn must be able to act. Without this, a continuation that
        # was suspended at an exhausted budget resumes with zero capacity, dies
        # on the next model call, and leaves the Task terminally FAILED - after
        # which the same request can only be retried as a brand-new Task that
        # repeats work already done. Capacity is judged by what is left, not by
        # which reason string produced the suspension.
        exhausted_capacity = (
            checkpoint.model_calls >= checkpoint.max_model_calls
            or checkpoint.tool_calls >= checkpoint.max_tool_calls
        )
        replenish_capacity = exhausted_capacity or pending.get("reason") in {
            "incomplete_recoverable", "unresolved_tool_batch",
        }
        visible_tools = await self.list_tools()
        compatibility = await self._evaluate_checkpoint_compatibility(
            task, checkpoint, visible_tools
        )
        if (
            compatibility.action
            is CheckpointCompatibilityAction.RECONCILE_REQUIRED
        ):
            checkpoint = await self._reconcile_agent_checkpoint(
                task, checkpoint, visible_tools, compatibility
            )
        elif compatibility.action is CheckpointCompatibilityAction.REBASE_REQUIRED:
            checkpoint = await self._rebase_agent_checkpoint(
                task, checkpoint, visible_tools, compatibility
            )
        elif compatibility.action is not CheckpointCompatibilityAction.EXACT_RESUME:
            await self._append_events(task_id, ((
                "continuation.resume_rejected", {
                    "turn_id": checkpoint.turn_id,
                    "reason_code": compatibility.reason_code,
                    "compatibility_action": compatibility.action.value,
                    "conflict_reasons": list(
                        compatibility.conflict_reasons
                        or compatibility.rebase_reasons
                    ),
                    "task_state_preserved": task.state.value,
                },
            ),))
            raise AgentCheckpointConflict(
                "Agent continuation cannot resume safely: "
                + compatibility.reason_code
                + (
                    " (" + ", ".join(
                        compatibility.conflict_reasons
                        or compatibility.rebase_reasons
                    ) + ")"
                    if (compatibility.conflict_reasons
                        or compatibility.rebase_reasons) else ""
                )
            )
        # Rebase/reconciliation commits a new configuration and checkpoint.
        # Reload before the atomic AWAITING_USER -> EXECUTING transition so the
        # continuation never writes through a stale Task version.
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        if task.state is not TaskState.AWAITING_USER:
            raise InvalidTurnState(
                f"task {task_id} changed while preparing continuation"
            )
        if task.active_agent_checkpoint is None:
            raise LookupError("continuation checkpoint disappeared during validation")
        checkpoint = AgentTurnCheckpoint.from_data(task.active_agent_checkpoint)
        pending = checkpoint.pending_user_action
        if pending.get("kind") != "CONTINUATION":
            raise ClarificationNotPending(
                "continuation changed while compatibility was validated"
            )
        resumed_checkpoint = replace(
            checkpoint, revision=checkpoint.revision + 1,
            messages=self._compact_continuation_messages(
                checkpoint.messages, normalized, input_id
            ),
            pending_user_action={},
            max_model_calls=(
                checkpoint.model_calls + self._dependencies.default_max_model_calls
                if replenish_capacity else checkpoint.max_model_calls
            ),
            max_tool_calls=(
                checkpoint.tool_calls + self._dependencies.default_max_tool_calls
                if replenish_capacity else checkpoint.max_tool_calls
            ),
            exploration_budget_state=(
                {} if replenish_capacity else checkpoint.exploration_budget_state
            ),
            completion_readiness_state=(
                # The three per-attempt counters restart so the new attempt gets
                # its own bounded corrections, but a stalled zero-progress count
                # must survive: it is what stops the same unsatisfiable
                # requirement from being retried forever.
                CompletionReadinessState(
                    stalled_continuations=CompletionReadinessState.from_data(
                        checkpoint.completion_readiness_state
                    ).stalled_continuations,
                ).to_data()
                if replenish_capacity else checkpoint.completion_readiness_state
            ),
        )
        resumed_task = task.transition(TaskState.EXECUTING).with_agent_checkpoint(
            resumed_checkpoint.to_data()
        )
        steering = SteeringProjector.project(
            task_id, await self._dependencies.store.read_events(task_id)
        )
        steering_hash = canonical_hash({
            "kind": SteeringKind.STEER.value, "text": normalized,
        })
        events = (
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id,
                stored.last_event_sequence + 1, "continuation.resolved", {
                    "turn_id": checkpoint.turn_id,
                    "input_id": input_id,
                    "input_hash": canonical_hash(normalized),
                    "completed_outcome_ids": list(
                        pending.get("completed_outcome_ids", [])
                    ),
                    "remaining_outcome_ids": list(
                        pending.get("remaining_outcome_ids", [])
                    ),
                    "capacity_replenished": replenish_capacity,
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id,
                stored.last_event_sequence + 2, "task.state_changed", {
                    "previous_state": task.state.value,
                    "next_state": resumed_task.state.value,
                    "reason": "user input continued a completed unit",
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id,
                stored.last_event_sequence + 3, "checkpoint.saved", {
                    "turn_id": checkpoint.turn_id,
                    "revision": resumed_checkpoint.revision,
                    "checkpoint_hash": resumed_checkpoint.checkpoint_hash,
                    "reason": "continuation-resolved",
                    "pending_user_action": {},
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id,
                stored.last_event_sequence + 4, "steering.queued", {
                    "steering_id": input_id,
                    "inbound_sequence": steering.latest_inbound_sequence + 1,
                    "kind": SteeringKind.STEER.value,
                    "text": normalized,
                    "text_hash": canonical_hash(normalized),
                    "request_hash": steering_hash,
                },
            ),
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, resumed_task.to_data(), events
        ))
        return await self._continue_agent_turn(
            resumed_checkpoint, visible_tools,
            on_text_delta=on_text_delta, on_progress=on_progress,
        )


    async def resolve_agent_clarification(
        self, request_id: str, resume_token: str, answer: str | None = None, *,
        selected_choice: str | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ) -> AgentTurnResult | AgentTurnSuspended | AgentClarificationSuspended:
        """Bind one answer to one pending request and resume the same turn."""
        if not request_id.strip() or not resume_token.strip():
            raise ValueError("request_id and resume_token must not be empty")
        normalized_answer = answer.strip() if answer is not None else ""
        normalized_choice = (
            selected_choice.strip() if selected_choice is not None else ""
        )
        if normalized_answer and normalized_choice:
            raise ValueError(
                "clarification accepts either answer or selected_choice, not both"
            )
        stored = await self._dependencies.store.find_task_by_pending_clarification(
            request_id
        )
        if stored is None:
            raise ClarificationNotPending(
                f"clarification is not pending: {request_id}"
            )
        task = TaskSnapshot.from_data(stored.data)
        request = task.pending_clarification
        if task.state is not TaskState.AWAITING_USER or request is None:
            raise ClarificationNotPending(
                f"task {task.task_id} has no pending clarification"
            )
        if request.request_id != request_id or not request.accepts_token(resume_token):
            raise ClarificationTokenMismatch(
                "clarification token is invalid or expired"
            )
        if task.active_agent_checkpoint is None:
            raise ClarificationNotPending("pending clarification lost its checkpoint")
        checkpoint = AgentTurnCheckpoint.from_data(task.active_agent_checkpoint)
        if request.kind is ClarificationKind.OUTCOME_RECONCILIATION:
            pending = checkpoint.pending_user_action
            if (
                pending.get("kind") != request.kind.value
                or pending.get("request_id") != request.request_id
                or pending.get("execution_id") != request.execution_id
            ):
                raise ClarificationTokenMismatch(
                    "outcome reconciliation does not match the active checkpoint"
                )
            return await self._resolve_outcome_reconciliation(
                task, request, checkpoint, normalized_answer,
                normalized_choice, resume_token, on_text_delta, on_progress,
            )
        if (
            checkpoint.task_id != task.task_id
            or checkpoint.turn_id != request.turn_id
            or not checkpoint.pending_tool_calls
            or checkpoint.pending_tool_calls[0] != request.call
        ):
            raise ClarificationTokenMismatch(
                "clarification does not match the active task, turn, and tool call"
            )
        visible_tools = await self.list_tools()
        await self._validate_agent_checkpoint(task, checkpoint, visible_tools)
        allowed_values = {choice.value for choice in request.choices}
        if request.choices:
            # New clients submit selected_choice. For one compatibility cycle,
            # an old client may still place the exact stable value in answer.
            selected = normalized_choice or (
                normalized_answer if normalized_answer in allowed_values else ""
            )
            if not selected or selected not in allowed_values:
                raise ValueError(
                    "single-choice clarification requires a valid selected_choice"
                )
            resolved_answer = selected
        else:
            if normalized_choice:
                raise ValueError(
                    "free-text clarification does not accept selected_choice"
                )
            if not normalized_answer:
                raise ValueError("clarification answer must not be empty")
            selected = None
            resolved_answer = normalized_answer
        result = ToolResult(
            call_id=request.call.call_id, ok=True,
            data={
                "answer": resolved_answer,
                "selected_choice": selected,
                "input_mode": request.input_mode,
            },
            meta={"request_id": request.request_id, "answered_by_user": True},
        )
        question_projection = await self._observe_evidence_question(
            task.task_id, request.turn_id, request.call, result, None
        )
        stored = await self._require_stored_task(task.task_id)
        task = TaskSnapshot.from_data(stored.data)
        tool_message = Message(
            message_id=f"msg-tool-{uuid4().hex}", role=MessageRole.TOOL,
            content=(ToolResultBlock(result),),
        )
        resumed_checkpoint = replace(
            checkpoint, revision=checkpoint.revision + 1,
            messages=checkpoint.messages + (tool_message,),
            pending_tool_calls=checkpoint.pending_tool_calls[1:],
            tool_batch=(
                checkpoint.tool_batch.with_pending(
                    checkpoint.pending_tool_calls[1:]
                ) if checkpoint.tool_batch is not None else None
            ),
            tool_calls=checkpoint.tool_calls + 1,
            evidence_question_state=question_projection.to_data(),
            pending_user_action={},
        )
        resumed = task.resolve_clarification().with_agent_checkpoint(
            resumed_checkpoint.to_data()
        )
        answer_hash = canonical_hash({
            "answer": resolved_answer, "selected_choice": selected,
        })
        events = (
            RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + 1, "clarification.resolved",
                {
                    "request_id": request.request_id,
                    "turn_id": request.turn_id,
                    "outcome_ref": request.call.outcome_ref,
                    "answer_hash": answer_hash,
                    "selected_choice": selected,
                    "input_mode": request.input_mode,
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + 2, "task.state_changed",
                {
                    "previous_state": task.state.value,
                    "next_state": resumed.state.value,
                    "reason": "user answered pending clarification",
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + 3, "turn.resumed",
                {
                    "turn_id": request.turn_id,
                    "previous_revision": checkpoint.revision,
                    "revision": resumed_checkpoint.revision,
                    "reason": "clarification resolved",
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + 4, "checkpoint.saved",
                {
                    "turn_id": request.turn_id,
                    "revision": resumed_checkpoint.revision,
                    "checkpoint_hash": resumed_checkpoint.checkpoint_hash,
                    "reason": "clarification-result-recorded",
                },
            ),
        )
        if request.call.outcome_ref is not None:
            spec = await self.get_task_spec(task.task_id)
            outcome = next(
                item for item in spec.outcomes
                if item.outcome_id == request.call.outcome_ref
            )
            reference = f"user:{request.request_id}:interact"
            next_status = (
                TaskOutcomeStatus.DELIVERED
                if set(outcome.required_effects).issubset({ToolEffect.INTERACT})
                else TaskOutcomeStatus.IN_PROGRESS
            )
            events += (RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + len(events) + 1,
                "task_outcome.state_changed", {
                    "outcome_id": outcome.outcome_id,
                    "status": next_status.value,
                    "fulfillment_ref": reference,
                    "reason": "user_decision_received",
                    "tool_call_id": request.call.call_id,
                    "tool_effect": ToolEffect.INTERACT.value,
                },
            ),)
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task.task_id, stored.version, resumed.to_data(), events
        ))
        return await self._continue_agent_turn(
            resumed_checkpoint, visible_tools, on_text_delta=on_text_delta,
            on_progress=on_progress,
        )

    async def _resolve_outcome_reconciliation(
        self, task: TaskSnapshot, request: ClarificationRequest,
        checkpoint: AgentTurnCheckpoint, normalized_answer: str,
        normalized_choice: str, resume_token: str,
        on_text_delta: Callable[[str], None] | None,
        on_progress: Callable[[AgentProgress], None] | None,
    ) -> AgentTurnResult | AgentTurnSuspended | AgentClarificationSuspended:
        """Apply an explicit observation without rewriting tool history."""
        allowed = {choice.value for choice in request.choices}
        selected = normalized_choice or (
            normalized_answer if normalized_answer in allowed else ""
        )
        if selected not in allowed:
            raise ValueError(
                "outcome reconciliation requires a displayed selected_choice"
            )
        if selected == "KEEP_BLOCKED":
            return AgentClarificationSuspended(
                task.task_id, request.turn_id, checkpoint.revision,
                request.request_id, request.question,
                tuple((item.value, item.label) for item in request.choices),
                request.reason, request.required, resume_token, request.kind.value,
            )
        execution_id = request.execution_id or ""
        execution = task.tool_executions.get(execution_id)
        if execution is None or execution.state is not ToolCommitState.UNKNOWN_OUTCOME:
            raise ClarificationNotPending(
                "outcome reconciliation lost its UNKNOWN_OUTCOME execution"
            )
        reconciled_value = (
            "SUCCEEDED" if selected == "CONFIRM_SUCCEEDED" else "NOT_APPLIED"
        )
        reference = f"user:{request.request_id}:outcome-reconciliation"
        reconciled_execution = execution.reconcile(reconciled_value, reference)
        result = ToolResult(
            call_id=execution.call.call_id,
            ok=reconciled_value == "SUCCEEDED",
            data={
                "reconciled_outcome": reconciled_value,
                "execution_id": execution_id,
            },
            error_code=(
                None if reconciled_value == "SUCCEEDED" else "NOT_APPLIED"
            ),
            message=(
                "User confirmed the external action succeeded."
                if reconciled_value == "SUCCEEDED" else
                "User confirmed the external action did not take effect."
            ),
            retryable=reconciled_value == "NOT_APPLIED",
            meta={
                "request_id": request.request_id,
                "reconciled_by_user": True,
                "original_commit_state": ToolCommitState.UNKNOWN_OUTCOME.value,
            },
        )
        tool_message = Message(
            f"msg-tool-reconciled-{uuid4().hex}", MessageRole.TOOL,
            (ToolResultBlock(result),),
        )
        reconciled_messages = tuple(
            message for message in checkpoint.messages
            if not (
                message.role is MessageRole.TOOL
                and any(
                    isinstance(block, ToolResultBlock)
                    and block.result.call_id == execution.call.call_id
                    for block in message.content
                )
            )
        )
        resumed_checkpoint = replace(
            checkpoint, revision=checkpoint.revision + 1,
            messages=reconciled_messages + (tool_message,),
            pending_tool_calls=(), pending_user_action={},
            tool_batch=(
                checkpoint.tool_batch.with_pending(
                    (), status=ToolBatchStatus.CLOSED
                ) if checkpoint.tool_batch is not None else None
            ),
        )
        stored = await self._require_stored_task(task.task_id)
        live = TaskSnapshot.from_data(stored.data)
        resumed = live.with_tool_execution(
            reconciled_execution
        ).resolve_clarification().with_agent_checkpoint(
            resumed_checkpoint.to_data()
        )
        answer_hash = canonical_hash({
            "selected_choice": selected, "execution_id": execution_id,
        })
        events: tuple[RuntimeEvent, ...] = (
            RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + 1, "tool.outcome_reconciled", {
                    "request_id": request.request_id,
                    "execution_id": execution_id,
                    "reconciled_outcome": reconciled_value,
                    "answer_hash": answer_hash,
                    "outcome_ref": execution.call.outcome_ref,
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + 2, "clarification.resolved", {
                    "request_id": request.request_id,
                    "turn_id": request.turn_id,
                    "selected_choice": selected,
                    "input_mode": request.input_mode,
                    "kind": request.kind.value,
                    "answer_hash": answer_hash,
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + 3, "task.state_changed", {
                    "previous_state": live.state.value,
                    "next_state": resumed.state.value,
                    "reason": "unknown tool outcome reconciled",
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + 4, "checkpoint.saved", {
                    "turn_id": request.turn_id,
                    "revision": resumed_checkpoint.revision,
                    "checkpoint_hash": resumed_checkpoint.checkpoint_hash,
                    "reason": "unknown-outcome-reconciled",
                },
            ),
        )
        if (
            execution.call.outcome_ref is not None
            and execution.call.outcome_binding_mode
            is OutcomeBindingMode.FULFILLMENT
        ):
            events += (RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + len(events) + 1,
                "task_outcome.state_changed", {
                    "outcome_id": execution.call.outcome_ref,
                    "status": (
                        TaskOutcomeStatus.ALREADY_SATISFIED.value
                        if reconciled_value == "SUCCEEDED" else
                        TaskOutcomeStatus.PENDING.value
                    ),
                    "fulfillment_ref": (
                        reference if reconciled_value == "SUCCEEDED" else ""
                    ),
                    "reason": "unknown_tool_outcome_reconciled",
                    "tool_call_id": execution.call.call_id,
                },
            ),)
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task.task_id, stored.version, resumed.to_data(), events
        ))
        visible_tools = await self.list_tools()
        await self._validate_agent_checkpoint(
            await self.get_task(task.task_id), resumed_checkpoint, visible_tools
        )
        return await self._continue_agent_turn(
            resumed_checkpoint, visible_tools,
            on_text_delta=on_text_delta, on_progress=on_progress,
        )

    async def interrupt_agent_turn(
        self, task_id: str, reason: str = "model invocation interrupted by user"
    ) -> TaskSnapshot:
        """Persist a recoverable interruption without completing the active turn."""
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("interrupt reason must not be empty")
        stored = await self._require_stored_task(task_id)
        current = TaskSnapshot.from_data(stored.data)
        if current.state is TaskState.INTERRUPTED:
            return current
        if current.state not in {
            TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW,
            TaskState.AWAITING_APPROVAL, TaskState.AWAITING_USER,
        }:
            raise InvalidTurnState(
                f"task {task_id} cannot be interrupted from {current.state.value}"
            )
        interrupting = current.transition(TaskState.INTERRUPTING)
        interrupted = interrupting.transition(TaskState.INTERRUPTED)
        checkpoint = current.active_agent_checkpoint
        turn_id = checkpoint.get("turn_id") if checkpoint is not None else None
        events = (
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
                "task.state_changed",
                {"previous_state": current.state.value,
                 "next_state": interrupting.state.value,
                 "reason": normalized_reason,
                 "intent": "interrupt", "schema_version": 1},
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 2,
                "turn.interrupted",
                {"turn_id": turn_id, "reason": normalized_reason,
                 "checkpoint_preserved": checkpoint is not None},
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 3,
                "task.state_changed",
                {"previous_state": interrupting.state.value,
                 "next_state": interrupted.state.value,
                 "reason": "active Agent checkpoint preserved for resume"},
            ),
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, interrupted.to_data(), events
        ))
        return interrupted

    async def _interrupting_intent(self, task_id: str) -> str:
        """Read the intent recorded on the latest INTERRUPTING transition.

        The intent lives on the durable event, never in memory. A missing field
        is a historical record and defaults to ``"interrupt"`` (safe side: keep
        the work and allow resume).
        """
        for event in reversed(
            await self._dependencies.store.read_events(task_id)
        ):
            if (
                event.event_type == "task.state_changed"
                and str(event.payload.get("next_state", ""))
                == TaskState.INTERRUPTING.value
            ):
                return str(event.payload.get("intent") or "interrupt")
        return "interrupt"

    async def reconcile_interrupting_tasks(self) -> tuple[str, ...]:
        """Settle INTERRUPTING Tasks left behind by a crash (spec §6.2, plan A).

        Called once after adapters start and before any user input is accepted.
        Idempotent: a settled Task is no longer INTERRUPTING, and the settling
        transition is recorded with reason ``interrupting_settled_on_startup``.
        """
        settled: list[str] = []
        for session in await self.list_sessions(include_archived=True):
            for task in await self.list_session_tasks(session.session_id):
                if task.state is not TaskState.INTERRUPTING:
                    continue
                intent = await self._interrupting_intent(task.task_id)
                target = (
                    TaskState.CANCELLED if intent == "cancel"
                    else TaskState.INTERRUPTED
                )
                try:
                    await self.transition_task(
                        task.task_id, target,
                        "interrupting_settled_on_startup",
                        authority="runtime", intent=intent,
                    )
                except (InvalidTurnState, TaskNotFound):
                    continue
                settled.append(task.task_id)
                note_interrupting_settled(task.task_id, intent)
        return tuple(settled)

    #: How a restart settles each Task state that asserts in-process work.
    #: A state absent here is either terminal or legitimately durable across
    #: restarts: AWAITING_APPROVAL / AWAITING_USER carry a pending action a
    #: human still owns, and INTERRUPTING is settled by
    #: ``reconcile_interrupting_tasks``.
    _ORPHANED_SETTLEMENTS: Mapping[TaskState, TaskState] = {
        # The checkpoint survives, so a resume re-runs the work.
        TaskState.EXECUTING: TaskState.INTERRUPTED,
        TaskState.RUNNING_WORKFLOW: TaskState.INTERRUPTED,
        TaskState.RESUMING: TaskState.INTERRUPTED,
        TaskState.PLANNING: TaskState.INTERRUPTED,
        TaskState.ROUTING: TaskState.INTERRUPTED,
        # Verification is a pure function of the ledger, so it is re-derived on
        # the next run rather than reported as an outcome nobody produced.
        TaskState.VERIFYING: TaskState.INTERRUPTED,
        # Reaching FINALIZING means trusted verification already passed, and
        # transition_task re-checks that gate; the Task only lacked its last
        # two bookkeeping transitions.
        TaskState.FINALIZING: TaskState.SUCCEEDED,
        # Nothing started, so there is nothing to recover or to report as work.
        TaskState.CREATED: TaskState.CANCELLED,
        TaskState.INTAKE: TaskState.CANCELLED,
        TaskState.RESOLVING_PROJECT: TaskState.CANCELLED,
        TaskState.SELECTING_EXTENSIONS: TaskState.CANCELLED,
    }



    async def _continue_agent_turn(
        self,
        checkpoint: AgentTurnCheckpoint,
        visible_tools: tuple[ToolSpec, ...],
        *,
        initial_tool_message: Message | None = None,
        recover_interrupted: bool = False,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ) -> (
        AgentTurnResult | AgentTurnSuspended | AgentClarificationSuspended
        | AgentContinuationSuspended
    ):
        state = TurnState(
            task_id=checkpoint.task_id, turn_id=checkpoint.turn_id,
            visible_tools=visible_tools,
            initial_tool_message=initial_tool_message,
            recover_interrupted=recover_interrupted,
            on_text_delta=on_text_delta, on_progress=on_progress,
        )
        await self.plan_task_spec(state.task_id)
        state.live_spec = await self.get_task_spec(state.task_id)
        state.closed_outcomes_at_entry = frozenset(
            outcome.outcome_id for outcome in state.live_spec.outcomes
            if outcome.required and outcome.status.is_closed
        )
        state.checkpoint = replace(
            checkpoint, task_spec_revision=state.live_spec.revision,
            task_spec_hash=state.live_spec.content_hash,
        )
        state.checkpoint = await self._inject_agent_decides_completion_diagnostics(
            state.checkpoint, state.visible_tools
        )
        state.messages = list(state.checkpoint.messages)
        if initial_tool_message is not None:
            state.messages.append(initial_tool_message)
        state.pending_tool_calls = list(state.checkpoint.pending_tool_calls)
        state.seen_call_ids = set(state.checkpoint.seen_call_ids)
        state.model_call_count = state.checkpoint.model_calls
        state.tool_call_count = state.checkpoint.tool_calls
        state.input_tokens = state.checkpoint.input_tokens
        state.output_tokens = state.checkpoint.output_tokens
        state.last_tool_error = next((
            f"{block.result.error_code or 'TOOL_ERROR'}"
            for message in reversed(state.messages)
            for block in reversed(message.content)
            if isinstance(block, ToolResultBlock) and not block.result.ok
        ), None)
        state.plan_guard = PlanGuard()
        state.visible_tool_by_name = {tool.name: tool for tool in state.visible_tools}
        state.checkpoint = self._synchronize_tool_batch(state.checkpoint)
        state.pending_tool_calls = list(state.checkpoint.pending_tool_calls)

        while True:
            # Cooperative cancellation boundary (task-cancellation-spec §7): a
            # durable cancel may have landed since the last step (another
            # process, or a restart without this in-process runner). Stop before
            # starting new work; the SDK never writes SUCCEEDED for a cancelled
            # Task.
            _begin = await self._begin_iteration(state)
            if _begin.action is PhaseAction.RETURN:
                return _begin.result

            await self._prepare_model_call(state)
            _verdict = await self._invoke_model(state)
            if _verdict.action is PhaseAction.CONTINUE:
                continue
            if _verdict.action is PhaseAction.RETURN:
                return _verdict.result

            _final = await self._finish_iteration(state)
            if _final.action is PhaseAction.RETURN:
                return _final.result
            if _final.action is PhaseAction.CONTINUE:
                continue
    async def _begin_iteration(self, state: TurnState) -> PhaseVerdict:
        """Forward to the turn-phase sibling (late import: no cycle)."""

        from .turn_phases import _begin_iteration as _phase

        return await _phase(self, state)

    async def _finish_iteration(self, state: TurnState) -> PhaseVerdict:
        """Forward to the turn-phase sibling (late import: no cycle)."""

        from .turn_phases import _finish_iteration as _phase

        return await _phase(self, state)
    async def _invoke_model(self, state: TurnState) -> PhaseVerdict:
        """Forward to the turn-phase sibling (late import: no cycle)."""

        from .turn_phases import _invoke_model as _phase

        return await _phase(self, state)
    async def _prepare_model_call(self, state: TurnState) -> None:
        """Forward to the turn-phase sibling (late import: no cycle)."""

        from .turn_phases import _prepare_model_call as _phase

        await _phase(self, state)
    async def _run_tool_batch(
        self, state: TurnState,
    ) -> (
        AgentTurnSuspended | AgentClarificationSuspended
        | AgentContinuationSuspended | None
    ):
        """Forward to the tool-batch phase sibling (late import: no cycle)."""

        from .turn_tool_batch import run_tool_batch

        return await run_tool_batch(self, state)



    async def _maybe_suspend_after_completed_unit(
        self, checkpoint: AgentTurnCheckpoint,
        closed_outcomes_at_entry: frozenset[str],
    ) -> AgentContinuationSuspended | None:
        """Persist one policy-selected unit boundary from Outcome facts.

        No user phrase is inspected here.  Runtime pauses only when this loop
        newly closed a required Outcome and the Planner explicitly selected the
        AFTER_COMPLETED_UNIT policy while later required Outcomes remain.
        """
        if checkpoint.pending_tool_calls or (
            checkpoint.tool_batch is not None and checkpoint.tool_batch.is_open
        ):
            return None
        spec = await self.get_task_spec(checkpoint.task_id)
        if spec.continuation_mode is not TaskContinuationMode.AFTER_COMPLETED_UNIT:
            return None
        closed = tuple(
            outcome for outcome in spec.outcomes
            if outcome.required and outcome.status.is_closed
        )
        newly_closed = tuple(
            outcome for outcome in closed
            if outcome.outcome_id not in closed_outcomes_at_entry
        )
        remaining = tuple(
            outcome for outcome in spec.outcomes
            if outcome.required and not outcome.status.is_closed
        )
        if not newly_closed or not remaining:
            return None
        pending = {
            "kind": "CONTINUATION",
            "task_id": checkpoint.task_id,
            "turn_id": checkpoint.turn_id,
            "completed_outcome_ids": [
                outcome.outcome_id for outcome in newly_closed
            ],
            "remaining_outcome_ids": [
                outcome.outcome_id for outcome in remaining
            ],
        }
        suspended_checkpoint = replace(
            checkpoint, revision=checkpoint.revision + 1,
            pending_user_action=pending,
            task_spec_revision=spec.revision, task_spec_hash=spec.content_hash,
        )
        stored = await self._require_stored_task(checkpoint.task_id)
        task = TaskSnapshot.from_data(stored.data)
        if task.state is not TaskState.EXECUTING:
            return None
        awaiting = task.transition(TaskState.AWAITING_USER).with_agent_checkpoint(
            suspended_checkpoint.to_data()
        )
        message = Message(
            f"msg-continuation-{uuid4().hex}", MessageRole.ASSISTANT,
            (TextBlock(
                "Completed this unit: "
                + "; ".join(outcome.description for outcome in newly_closed)
                + ". Remaining required work: "
                + "; ".join(outcome.description for outcome in remaining)
                + "."
            ),),
        )
        events = (
            RuntimeEvent(
                f"evt-{uuid4().hex}", checkpoint.task_id,
                stored.last_event_sequence + 1, "continuation.requested",
                pending,
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", checkpoint.task_id,
                stored.last_event_sequence + 2, "checkpoint.saved", {
                    "turn_id": checkpoint.turn_id,
                    "revision": suspended_checkpoint.revision,
                    "checkpoint_hash": suspended_checkpoint.checkpoint_hash,
                    "reason": "completed-unit-continuation",
                    "pending_user_action": pending,
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", checkpoint.task_id,
                stored.last_event_sequence + 3, "task.state_changed", {
                    "previous_state": task.state.value,
                    "next_state": awaiting.state.value,
                    "reason": "completed unit awaits semantic continuation",
                },
            ),
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            checkpoint.task_id, stored.version, awaiting.to_data(), events
        ))
        return AgentContinuationSuspended(
            checkpoint.task_id, checkpoint.turn_id,
            suspended_checkpoint.revision,
            tuple(outcome.outcome_id for outcome in newly_closed),
            tuple(outcome.outcome_id for outcome in remaining), message,
        )



    async def _suspend_incomplete_recoverable(
        self, checkpoint: AgentTurnCheckpoint, message: Message,
        gaps: tuple[CompletionGap, ...], *, reason: str = "incomplete_recoverable",
    ) -> AgentContinuationSuspended:
        """Persist unmet required work as a resumable boundary.

        The default is exhausted recoverable work. Acceptance criteria that
        remain unmet after a required blocker disclosure use the same durable
        continuation boundary, but retain their distinct reason for callers.
        """
        spec = await self.get_task_spec(checkpoint.task_id)
        remaining_outcome_ids = tuple(
            outcome.outcome_id for outcome in spec.outcomes
            if outcome.required and not outcome.status.is_closed
        )
        required_gap_ids = tuple(sorted(
            gap.gap_id for gap in gaps if gap.required
        ))
        # A user continuation replenishes capacity. That must not turn one
        # unsatisfiable requirement into an endless series of identical
        # attempts, so count how many consecutive continuations ended with the
        # same required gaps and the same outstanding Outcomes, and carry that
        # count past the replenishment.
        readiness_state = CompletionReadinessState.from_data(
            checkpoint.completion_readiness_state
        )
        previous = await self._last_continuation_pending(checkpoint.task_id)
        stalled = (
            readiness_state.stalled_continuations + 1
            if _continuation_made_no_progress(
                previous, required_gap_ids,
                tuple(sorted(remaining_outcome_ids)),
            )
            else 0
        )
        if stalled >= self._dependencies.completion_max_stalled_continuations:
            # INV-4: the same required gaps survived several continuations with
            # no progress. Stop asking the user to retry an unclosable
            # requirement and hand the decision to a human instead.
            return await self._finalize_needs_review(
                checkpoint, gaps, reason="required_work_unverifiable_or_stalled",
                stalled=stalled, message=message,
            )
        pending = {
            "kind": "CONTINUATION",
            "reason": reason,
            "task_id": checkpoint.task_id,
            "turn_id": checkpoint.turn_id,
            "completed_outcome_ids": [],
            "remaining_outcome_ids": list(remaining_outcome_ids),
            "gap_ids": list(required_gap_ids),
            "zero_progress_continuations": stalled,
        }
        suspended_checkpoint = replace(
            checkpoint, revision=checkpoint.revision + 1,
            pending_user_action=pending,
            task_spec_revision=spec.revision, task_spec_hash=spec.content_hash,
            completion_readiness_state=replace(
                readiness_state, stalled_continuations=stalled
            ).to_data(),
        )
        stored = await self._require_stored_task(checkpoint.task_id)
        task = TaskSnapshot.from_data(stored.data)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                "recoverable completion boundary requires an executing Task"
            )
        awaiting = task.transition(TaskState.AWAITING_USER).with_agent_checkpoint(
            suspended_checkpoint.to_data()
        )
        events = (
            RuntimeEvent(
                f"evt-{uuid4().hex}", checkpoint.task_id,
                stored.last_event_sequence + 1, "continuation.requested", pending,
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", checkpoint.task_id,
                stored.last_event_sequence + 2, "checkpoint.saved", {
                    "turn_id": checkpoint.turn_id,
                    "revision": suspended_checkpoint.revision,
                    "checkpoint_hash": suspended_checkpoint.checkpoint_hash,
                    "reason": "incomplete-recoverable-continuation",
                    "pending_user_action": pending,
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", checkpoint.task_id,
                stored.last_event_sequence + 3, "task.state_changed", {
                    "previous_state": task.state.value,
                    "next_state": awaiting.state.value,
                    "reason": "recoverable work awaits a later Turn",
                },
            ),
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            checkpoint.task_id, stored.version, awaiting.to_data(), events
        ))
        return AgentContinuationSuspended(
            checkpoint.task_id, checkpoint.turn_id,
            suspended_checkpoint.revision, (), remaining_outcome_ids, message,
        )


    async def _evaluate_evidence_delta(
        self, task_id: str, turn_id: str, call: ToolCall, result: ToolResult,
        inventory_data: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], EvidenceDelta | None]:
        evaluator = self._dependencies.evidence_delta_evaluator
        if evaluator is None:
            return dict(inventory_data), None
        try:
            evaluation = await evaluator.evaluate(
                call, result, EvidenceInventory.from_data(inventory_data)
            )
        except Exception as error:
            await self._append_events(task_id, ((
                "evidence.delta_failed", {
                    "turn_id": turn_id,
                    "tool_call_id": call.call_id,
                    "tool_name": call.name,
                    "error_type": type(error).__name__,
                },
            ),))
            return dict(inventory_data), None
        delta = evaluation.delta
        await self._append_events(task_id, ((
            "evidence.delta_evaluated", {
                "turn_id": turn_id,
                "tool_call_id": call.call_id,
                "tool_name": call.name,
                **delta.to_data(),
            },
        ),))
        return evaluation.inventory.to_data(), delta

    async def _bind_evidence_question(
        self, task_id: str, turn_id: str, call: ToolCall,
    ) -> EvidenceQuestionProjection:
        """Bind once per Tool Call and return the durable lifecycle state."""
        question = call.evidence_question
        projection = await self.get_evidence_questions(task_id)
        if question is None:
            return projection
        current = projection.get(question.question_id)
        if current is not None and call.call_id in current.tool_call_ids:
            return projection
        projection.bind(question, turn_id, call.call_id)
        await self._append_events(task_id, ((
            "evidence.question_bound", {
                "turn_id": turn_id,
                "tool_call_id": call.call_id,
                "tool_name": call.name,
                "question_ref": canonical_hash(question.question_id)[:12],
                **question.to_data(),
            },
        ),))
        return await self.get_evidence_questions(task_id)

    async def _observe_evidence_question(
        self, task_id: str, turn_id: str, call: ToolCall, result: ToolResult,
        delta: EvidenceDelta | None,
    ) -> EvidenceQuestionProjection:
        """Advance lifecycle from a real Tool result, never model prose."""
        projection = await self.get_evidence_questions(task_id)
        question = call.evidence_question
        if question is None:
            return projection
        current = projection.get(question.question_id)
        if current is None:
            raise ValueError("evidence question must be bound before observation")
        events = await self._dependencies.store.read_events(task_id)
        # ``tool.action_disposed`` is appended first; the projected record is
        # authored by the following ``evidence.question_state_changed`` event.
        next_sequence = events[-1].sequence + 2 if events else 2
        projection, record = projection.observe(
            call, result, delta, event_sequence=next_sequence
        )
        await self._append_events(task_id, ((
            "evidence.question_state_changed", {
                "turn_id": turn_id,
                "tool_call_id": call.call_id,
                "tool_name": call.name,
                "question_ref": record.question_ref,
                "previous_status": current.status.value,
                "next_status": record.status.value,
                "observation_kind": (
                    record.observation_kind.value
                    if record.observation_kind else None
                ),
                "blocking_reason": record.blocking_reason,
                "evidence_count": len(record.evidence_references),
                "record": record.to_data(),
            },
        ),))
        return await self.get_evidence_questions(task_id)

    async def _dispose_evidence_question(
        self, task_id: str, turn_id: str, call: ToolCall,
        disposition: ToolActionDisposition, reason: str,
    ) -> EvidenceQuestionProjection:
        """Close a bound question when Runtime removes its Action pre-execution.

        The disposition is an audited lifecycle fact, never successful evidence.
        Keeping this transition in one Kernel boundary prevents policy adapters
        from leaving orphan OPEN questions when they redirect or stop an Action.
        """
        projection = await self.get_evidence_questions(task_id)
        if call.evidence_question is None:
            return projection
        current = projection.get(call.evidence_question.question_id)
        if current is None:
            raise ValueError("evidence question must be bound before disposition")
        events = await self._dependencies.store.read_events(task_id)
        next_sequence = events[-1].sequence + 1 if events else 1
        projection, record = projection.dispose(
            call, disposition, reason, event_sequence=next_sequence
        )
        if record is None:
            return projection
        await self._append_events(task_id, ((
            "tool.action_disposed", {
                "turn_id": turn_id, "tool_call_id": call.call_id,
                "tool_name": call.name, "disposition": disposition.value,
                "reason": reason, "question_ref": record.question_ref,
            },
        ), (
            "evidence.question_state_changed", {
                "turn_id": turn_id, "tool_call_id": call.call_id,
                "tool_name": call.name, "question_ref": record.question_ref,
                "previous_status": current.status.value,
                "next_status": record.status.value,
                "observation_kind": (
                    record.observation_kind.value
                    if record.observation_kind else None
                ),
                "blocking_reason": record.blocking_reason,
                "evidence_count": len(record.evidence_references),
                "record": record.to_data(),
            },
        )))
        return await self.get_evidence_questions(task_id)



























    async def _request_agent_clarification(
        self, task_id: str, turn_id: str, call: ToolCall,
        agent_checkpoint: Mapping[str, Any] | None,
    ) -> None:
        if agent_checkpoint is None:
            raise InvalidToolArguments(
                "core.request_input is only available inside an Agent turn"
            )
        _, spec = await self._find_tool(call.name)
        validate_tool_arguments(spec, call.arguments)
        question = str(call.arguments.get("question", "")).strip()
        reason = str(call.arguments.get("reason", "")).strip()
        if not question or len(question) > 500:
            raise InvalidToolArguments(
                "core.request_input question must contain 1..500 characters"
            )
        if not reason or len(reason) > 500:
            raise InvalidToolArguments(
                "core.request_input reason must contain 1..500 characters"
            )
        raw_choices = call.arguments.get("choices", ())
        if not isinstance(raw_choices, (list, tuple)) or len(raw_choices) > 3:
            raise InvalidToolArguments(
                "core.request_input choices must contain at most 3 items"
            )
        choices: list[ClarificationChoice] = []
        for raw in raw_choices:
            if not isinstance(raw, Mapping):
                raise InvalidToolArguments(
                    "core.request_input choice must be an object"
                )
            unknown = set(raw) - {"value", "label"}
            value = raw.get("value")
            label = raw.get("label")
            if unknown or not isinstance(value, str) or not isinstance(label, str):
                raise InvalidToolArguments(
                    "core.request_input choice requires string value and label only"
                )
            try:
                choices.append(ClarificationChoice(value.strip(), label.strip()))
            except ValueError as error:
                raise InvalidToolArguments(str(error)) from error
        if len({choice.value for choice in choices}) != len(choices):
            raise InvalidToolArguments(
                "core.request_input choice values must be unique"
            )
        required = call.arguments.get("required", True)
        if not isinstance(required, bool):
            raise InvalidToolArguments(
                "core.request_input required must be boolean"
            )
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        if task.pending_clarification is not None:
            raise ClarificationNotPending("task already has a pending clarification")
        token = secrets.token_urlsafe(32)
        created = datetime.now(timezone.utc)
        request = ClarificationRequest(
            request_id=f"clarification-{uuid4().hex}",
            task_id=task_id, turn_id=turn_id, call=call,
            question=question, choices=tuple(choices), reason=reason,
            required=required,
            resume_token_hash=ClarificationRequest.hash_resume_token(token),
            created_at=created, expires_at=created + timedelta(hours=24),
        )
        pending_user_action = {
            "kind": "CLARIFICATION",
            "request_id": request.request_id,
            "task_id": task_id, "turn_id": turn_id,
            "required": required,
            "outcome_ref": call.outcome_ref,
        }
        checkpoint_data = replace(
            AgentTurnCheckpoint.from_data(agent_checkpoint),
            pending_user_action=pending_user_action,
        ).to_data()
        awaiting = task.await_clarification(request)
        awaiting = awaiting.with_agent_checkpoint(checkpoint_data)
        events = (
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id,
                stored.last_event_sequence + 1, "clarification.requested",
                {
                    "request_id": request.request_id,
                    "turn_id": turn_id,
                    "outcome_ref": call.outcome_ref,
                    "question_hash": canonical_hash({"question": question}),
                    "choice_count": len(choices),
                    "input_mode": request.input_mode,
                    "required": required,
                    "expires_at": request.expires_at.isoformat(),
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id,
                stored.last_event_sequence + 2, "checkpoint.saved",
                {
                    "turn_id": turn_id,
                    "request_id": request.request_id,
                    "revision": agent_checkpoint.get("revision"),
                    "reason": "clarification-requested",
                    "pending_user_action": pending_user_action,
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id,
                stored.last_event_sequence + 3, "task.state_changed",
                {
                    "previous_state": task.state.value,
                    "next_state": awaiting.state.value,
                    "reason": "waiting for required user clarification",
                },
            ),
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, awaiting.to_data(), events
        ))
        raise ClarificationRequired(request, token)

    async def _append_events(
        self,
        task_id: str,
        event_data: tuple[tuple[str, Mapping[str, Any]], ...],
    ) -> None:
        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        events = tuple(
            RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=stored.last_event_sequence + index,
                event_type=event_type,
                payload=payload,
            )
            for index, (event_type, payload) in enumerate(event_data, start=1)
        )
        await self._dependencies.store.commit(
            RuntimeUnitOfWork(
                task_id=task_id,
                expected_version=stored.version,
                next_state=stored.data,
                events=events,
            )
        )




    @staticmethod
    def _outcome_binding_correction(detail: str) -> str:
        """State the one repair that can satisfy a rejected Outcome binding.

        Advising the model to "correct or omit the outcome_ref" is unsatisfiable
        for OUTCOME_SELECTION_AMBIGUOUS: every compatible Outcome is already
        selected and the reference is already omitted, so repeating the call
        reproduces the identical rejection until the recovery budget is spent
        and the turn is interrupted. That state has exactly one repair, so it is
        named here rather than left to be inferred from raw decision JSON.
        """
        try:
            decision = json.loads(detail) if detail else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            decision = {}
        if not isinstance(decision, Mapping):
            decision = {}
        candidates = tuple(
            str(item) for item in decision.get("compatible_outcome_ids", ())
            if str(item).strip()
        )
        reason = str(decision.get("reason") or "")
        listed = ", ".join(candidates)
        if (
            reason == OutcomeBindingReason.OUTCOME_SELECTION_AMBIGUOUS.value
            and len(candidates) > 1
        ):
            return (
                "Your previous tool call could bind to more than one open Task "
                f"outcome ({listed}). Name exactly one of them: repeat the same "
                f"tool call with outcome_ref set to one of {listed}. Omitting "
                "outcome_ref will be rejected again. Do not repeat the invalid "
                f"call unchanged. Runtime detail: {detail}"
            )
        return (
            "Your previous tool call could not bind to an eligible compatible "
            "Task outcome. Inspect Runtime detail below and correct or omit the "
            "outcome_ref. Do not repeat the invalid call unchanged. "
            f"Runtime detail: {detail}"
        )




    @staticmethod
    def _supporting_observation_is_available(
        target: TaskOutcomeSnapshot, eligible_outcome_ids: tuple[str, ...],
    ) -> bool:
        """Allow preparation only for a direct descendant of eligible work."""
        if not target.depends_on:
            return False
        return any(dependency in set(eligible_outcome_ids)
                   for dependency in target.depends_on)








    @staticmethod
    def _atomic_action_completed(
        outcome: TaskOutcomeSnapshot, call: ToolCall, result: ToolResult,
    ) -> bool:
        """Close only a TaskSpec-declared, exactly matching atomic action.

        Tool kind, effect, file count, or a successful result are intentionally
        insufficient.  The planner must declare the exact tool and arguments so
        Runtime can prove that one invocation is the whole Outcome.
        """
        contract = outcome.atomic_action
        return bool(
            result.ok
            and outcome.completion_policy
            is TaskOutcomeCompletionPolicy.ATOMIC_ACTION
            and isinstance(contract, Mapping)
            and call.name == str(contract.get("tool_name", ""))
            and isinstance(contract.get("arguments"), Mapping)
            and canonical_hash(dict(call.arguments))
            == canonical_hash(dict(contract["arguments"]))
        )







    async def _commit_recovery_result(
        self,
        task: TaskSnapshot,
        execution: ToolExecutionRecord,
        result: ToolResult,
        event_type: str,
    ) -> None:
        stored = await self._dependencies.store.load_task(task.task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task.task_id}")
        current = TaskSnapshot.from_data(stored.data)
        updated = current.with_tool_execution(execution)
        event = RuntimeEvent(
            event_id=f"evt-{uuid4().hex}", task_id=task.task_id,
            sequence=stored.last_event_sequence + 1, event_type=event_type,
            payload={
                "turn_id": execution.turn_id,
                "execution_id": execution.execution_id,
                "commit_state": execution.state.value,
                "result": result.to_data(),
            },
        )
        await self._dependencies.store.commit(
            RuntimeUnitOfWork(
                task.task_id, stored.version, updated.to_data(), (event,)
            )
        )










    async def _record_turn_failure(
        self, task_id: str, turn_id: str, error: Exception,
        prompt_receipt: PromptAssemblyReceipt | None = None,
        *, failure_kind: str = "model_response",
        failure_category: ModelFailureCategory = ModelFailureCategory.UNKNOWN,
        retry_safety: ModelRetrySafety = ModelRetrySafety.NEVER,
        recovery_action: ModelRecoveryAction = ModelRecoveryAction.FAIL_TERMINAL,
    ) -> None:
        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task disappeared during turn: {task_id}")
        current = TaskSnapshot.from_data(stored.data)
        failed = current.transition(TaskState.FAILED)
        next_sequence = stored.last_event_sequence + 1
        error_payload = {
            "turn_id": turn_id,
            "error_type": type(error).__name__,
            "message": str(error),
            "failure_kind": failure_kind,
            "failure_category": failure_category.value,
            "retry_safety": retry_safety.value,
            "recovery_action": recovery_action.value,
            **(prompt_receipt.event_data() if prompt_receipt is not None else {}),
        }
        events = (
            RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=next_sequence,
                event_type="llm.failed",
                payload=error_payload,
            ),
            RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=next_sequence + 1,
                event_type="turn.failed",
                payload=error_payload,
            ),
            RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=next_sequence + 2,
                event_type="task.state_changed",
                payload={
                    "previous_state": current.state.value,
                    "next_state": failed.state.value,
                    "reason": (
                        "model tool protocol failed"
                        if failure_kind == "tool_protocol"
                        else "model invocation failed"
                    ),
                },
            ),
        )
        await self._dependencies.store.commit(
            RuntimeUnitOfWork(
                task_id=task_id,
                expected_version=stored.version,
                next_state=failed.to_data(),
                events=events,
            )
        )

    async def _record_recoverable_turn_interruption(
        self, task_id: str, turn_id: str, error: Exception,
        prompt_receipt: PromptAssemblyReceipt | None = None,
        *, failure_kind: str = "provider",
        failure_category: ModelFailureCategory = ModelFailureCategory.UNKNOWN,
        retry_safety: ModelRetrySafety = ModelRetrySafety.NEVER,
        recovery_action: ModelRecoveryAction = ModelRecoveryAction.FAIL_TERMINAL,
    ) -> None:
        """Persist an unexpected stop while preserving its safe checkpoint."""
        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task disappeared during turn: {task_id}")
        current = TaskSnapshot.from_data(stored.data)
        if current.state is TaskState.INTERRUPTED:
            return
        if (
            current.state is not TaskState.EXECUTING
            or current.active_agent_checkpoint is None
        ):
            await self._record_turn_failure(
                task_id, turn_id, error, prompt_receipt,
                failure_kind=failure_kind,
                failure_category=failure_category,
                retry_safety=retry_safety,
                recovery_action=recovery_action,
            )
            return
        interrupting = current.transition(TaskState.INTERRUPTING)
        interrupted = interrupting.transition(TaskState.INTERRUPTED)
        error_payload = {
            "turn_id": turn_id,
            "error_type": type(error).__name__,
            "message": str(error),
            "failure_kind": failure_kind,
            "failure_category": failure_category.value,
            "retry_safety": retry_safety.value,
            "recovery_action": recovery_action.value,
            "recoverable": True,
            "checkpoint_preserved": True,
            **(prompt_receipt.event_data() if prompt_receipt is not None else {}),
        }
        sequence = stored.last_event_sequence + 1
        events = (
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, sequence, "llm.failed",
                error_payload,
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, sequence + 1,
                "task.state_changed", {
                    "previous_state": current.state.value,
                    "next_state": interrupting.state.value,
                    # Truthful and machine-readable: the stop was *classified*
                    # (recoverable, checkpoint preserved), so calling it
                    # "unexpected" contradicted the payload right next to it and
                    # made downstream triage guess.
                    "reason": (
                        f"recoverable stop: {failure_category.value}/"
                        f"{failure_kind}/{recovery_action.value}"
                    ),
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, sequence + 2,
                "turn.interrupted", error_payload,
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, sequence + 3,
                "task.state_changed", {
                    "previous_state": interrupting.state.value,
                    "next_state": interrupted.state.value,
                    "reason": "safe Agent checkpoint preserved for resume",
                },
            ),
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, interrupted.to_data(), events
        ))

    async def _record_agent_loop_failure(
        self, task_id: str, turn_id: str, error: AgentLoopLimitExceeded
    ) -> None:
        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task disappeared during agent loop: {task_id}")
        current = TaskSnapshot.from_data(stored.data)
        failed = current.transition(TaskState.FAILED)
        next_sequence = stored.last_event_sequence + 1
        error_payload = {
            "turn_id": turn_id,
            "error_type": type(error).__name__,
            "message": str(error),
            "limit": error.limit,
            "maximum": error.maximum,
            "model_calls": error.model_calls,
            "tool_calls": error.tool_calls,
            "last_tool_error": error.last_tool_error,
            "max_model_calls": error.max_model_calls,
            "max_tool_calls": error.max_tool_calls,
        }
        events = (
            RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=next_sequence,
                event_type="agent.limit_exceeded",
                payload=error_payload,
            ),
            RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=next_sequence + 1,
                event_type="turn.failed",
                payload=error_payload,
            ),
            RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=next_sequence + 2,
                event_type="task.state_changed",
                payload={
                    "previous_state": current.state.value,
                    "next_state": failed.state.value,
                    "reason": error.limit,
                },
            ),
        )
        await self._dependencies.store.commit(
            RuntimeUnitOfWork(
                task_id=task_id,
                expected_version=stored.version,
                next_state=failed.to_data(),
                events=events,
            )
        )
