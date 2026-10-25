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
    CompletionReviewFailurePolicy, CompletionReviewerPort,
    CompletionReviewMode, TurnStopPolicyPort,
    CompletionGap,
    BackgroundProcessStartResult,
    ProcessCommandResult,
    ProcessExecutorPort,
    ProcessExitStatus,
    ProcessLogs,
    ProcessResult,
    ProcessStartRequest,
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
    ToolResultGovernorPort,
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
    TurnState,
    AgentClarificationSuspended,
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
from .task import (
    LEGAL_TRANSITIONS, SUCCESS_PATH, TaskKind, TaskNotFound, TaskSnapshot,
    TaskState,
)
from .task_spec import (
    TaskAcceptanceCriterion, TaskCriterionKind,
    OutcomeBindingAction, OutcomeBindingDecision, OutcomeBindingReason,
    TaskExecutionFocus, TaskExecutionFocusProjector,
    TaskOutcomeCompletionPolicy, TaskOutcomeEligibilityCalculator,
    TaskOutcomeKind, TaskOutcomeProposal, TaskOutcomeSnapshot,
    TaskOutcomeStatus,
    TaskSpecProjector, TaskSpecProposal, TaskSpecSnapshot,
    REQUIRED_EFFECT_CRITERION_ID, RUNTIME_AUTHORED_CRITERION_KINDS,
    validate_authored_criterion,
)
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
)
from .session_resources import (
    SessionQuestionReference, SessionResourceKind, SessionResourceReference,
)
from .conversation_orchestration import OrchestrationMode
from .skill_selector import SkillSelector
from .skills import SkillRegistry
from .tool_result_governor import DefaultToolResultGovernor
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
#: to a neighbouring branch (the project's dispatch rule). The lookup walks
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
    completion_reviewer: CompletionReviewerPort | None = None
    turn_stop_policies: tuple[TurnStopPolicyPort, ...] = ()
    completion_review_mode: CompletionReviewMode = CompletionReviewMode.OFF
    completion_review_max_continuations: int = 1
    completion_review_failure_policy: CompletionReviewFailurePolicy = (
        CompletionReviewFailurePolicy.VISIBLE_UNVERIFIED
    )
    model_recovery_policy: ModelRecoveryPolicyPort | None = None
    evidence_delta_evaluator: EvidenceDeltaEvaluatorPort | None = None
    semantic_action_classifier: SemanticActionClassifierPort | None = None
    read_hits_policy: ReadHitsPolicyPort | None = None
    artifact_read_policy: ArtifactReadPolicyPort | None = None
    progressive_scope_policy: ProgressiveScopePolicyPort | None = None
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
    checkpoint_compatibility_policy: (
        CheckpointCompatibilityPolicyPort | None
    ) = None
    process_executor: ProcessExecutorPort | None = None
    tools: tuple[ToolProviderPort, ...] = ()
    tool_result_governor: ToolResultGovernorPort = field(
        default_factory=DefaultToolResultGovernor
    )
    runtime_adapters: tuple[RuntimeAdapter, ...] = ()
    configuration_metadata: Mapping[str, Any] = field(default_factory=dict)
    orchestration_mode: OrchestrationMode = OrchestrationMode.CONVERSATION_LOOP
    #: The reference agent allows 90 iterations per parent turn (50 per subagent) and Codex
    #: has no ceiling at all, relying on compaction. 40 turned ordinary
    #: engineering work into ``max_model_calls`` failures, which the user could
    #: only answer by continuing -- and each continuation re-read context.
    default_max_model_calls: int = 90
    default_max_tool_calls: int = 120
    default_max_output_tokens: int = 1024
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
    skill_registry: SkillRegistry = field(default_factory=SkillRegistry)
    skill_selector: SkillSelector | None = None


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


#: Effects whose delivery is a performed side effect. ``observe`` and
#: ``interact`` are deliberately excluded: observation is governed by the
#: evidence criteria, and asking the user is closed by the interaction flow.
#: Neither is a claim that work was carried out, so neither can be faked by
#: prose and neither belongs in a delivery assertion.
_DELIVERABLE_EFFECTS: frozenset[ToolEffect] = frozenset({
    ToolEffect.MUTATE, ToolEffect.EXECUTE,
})


def _required_effect_deliveries(spec: TaskSpecSnapshot) -> frozenset[ToolEffect]:
    """Side effects the Task spec requires the Task to leave a record of.

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
    result is good enough is the Task spec's own command criteria to judge.
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
            f"the task spec requires a performed {effect.value} action",
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
        "about what is current. A question whose answer changes over time -- "
        "news, schedules, scores, prices, weather -- must be looked up with the "
        "available tools; recalling it from training data is a stale answer, and "
        "saying the data is unavailable is not an answer."
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
        #: Per-turn in-process cancellation resources (task-cancellation-spec
        #: §5.1). Not persisted; closed when a turn is replaced, suspended, or
        #: settled. Task-owned managed processes deliberately live outside it.
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

        return tuple(gaps)


    def _demote_unresolvable_criteria(
        self, task: TaskSnapshot, events: tuple[RuntimeEvent, ...],
        criteria: tuple[TaskAcceptanceCriterion, ...],
    ) -> tuple[tuple[TaskAcceptanceCriterion, ...], list[dict[str, str]]]:
        """Demote criteria whose verification channel cannot decide them.

        An ``evidence_reference`` criterion asserts a fact that must already be
        true; it is never work that execution can produce. A reference no
        event/tool_call/mutation can resolve is permanently unverifiable and
        must not survive as a required gate. Demoting it keeps the requirement
        visible to a judge instead of burning the turn budget on a debt nothing
        can pay.

        Demotion happens only on the authoring path, so historical snapshots
        keep replaying unchanged.
        """
        kept: list[TaskAcceptanceCriterion] = []
        demoted: list[dict[str, str]] = []
        for criterion in criteria:
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
        """Release transient resources of a settled/replaced turn generation.

        Task-owned managed processes are persisted separately and are not part
        of this scope: an approval pause must preserve them for the resumed
        checkpoint.
        """
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
        from .context_messages import GENERATED_CONTEXT_PREFIXES
        generated_prefixes = GENERATED_CONTEXT_PREFIXES + ("completion-readiness-",)
        return next((
            message for message in messages
            if message.role is MessageRole.USER
            and not message.message_id.startswith(generated_prefixes)
        ), None)

    async def _commit_final_agent_result(
        self, *, task_id: str, turn_id: str, user_message: Message,
        assistant_message: Message, llm_payload: Mapping[str, Any],
        assistant_replay_eligible: bool = False,
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
            # A conversation Task does not terminalise when one turn ends: it
            # returns to AWAITING_INPUT in this same atomic commit, so a crash
            # can never leave "checkpoint cleared but still EXECUTING". The
            # keep the retained checkpoint archive from being treated as a
            # finished turn.
            conversation_finalized = (
                clear_agent_checkpoint
                and updated_task.task_kind is TaskKind.CONVERSATION
                and updated_task.state is TaskState.EXECUTING
            )
            if conversation_finalized:
                updated_task = updated_task.transition(TaskState.AWAITING_INPUT)
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
                "llm.completed", dict(llm_payload), turn_id=turn_id,
            )
            task_events: list[RuntimeEvent] = [llm_event]
            if conversation_finalized:
                task_events.append(RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id,
                    stored.last_event_sequence + len(task_events) + 1,
                    "task.state_changed", {
                        "previous_state": TaskState.EXECUTING.value,
                        "next_state": TaskState.AWAITING_INPUT.value,
                        "reason": "conversation turn completed",
                    },
                    turn_id=turn_id,
                ))
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
                    turn_id=turn_id,
                ))
            for event_type, payload in additional_events:
                task_events.append(RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id,
                    stored.last_event_sequence + len(task_events) + 1,
                    event_type, payload, turn_id=turn_id,
                ))
            task_events.append(RuntimeEvent(
                f"evt-{uuid4().hex}", task_id,
                stored.last_event_sequence + len(task_events) + 1,
                "turn.completed", {
                    "turn_id": turn_id,
                    "model_calls": llm_payload.get("model_call"),
                    "tool_calls": completed_tool_calls,
                },
                turn_id=turn_id,
            ))
            session_unit = await self._session_task_result_unit(
                updated_task, turn_id, user_message, assistant_message,
                assistant_replay_eligible=assistant_replay_eligible,
                assistant_conclusion=conclusion,
                conclusion_validation=validation,
                answer_event_ref=llm_event.event_id,
                turn_counters={
                    "model_calls": llm_payload.get("model_call") or 0,
                    "tool_calls": completed_tool_calls,
                },
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

        This is the whole completion rule (reference agent semantics): the model
        stops calling tools, so the turn is over and the Task succeeded. No
        verdict, no evidence, no criterion is consulted.

        ``VERIFYING`` and ``FINALIZING`` are pure accounting hops here: the
        success path lives in ``core/task.py`` next to ``LEGAL_TRANSITIONS``.

        A conversation Task is the exception: one finished turn is not the end of
        the conversation. ``_commit_final_agent_result`` already returned it to
        ``AWAITING_INPUT`` in the same commit; this is a safety net for turn
        paths that do not run through that boundary.
        """
        task = await self.get_task(task_id)
        if task.task_kind is TaskKind.CONVERSATION:
            if task.state is TaskState.EXECUTING:
                return await self.transition_task(
                    task_id, TaskState.AWAITING_INPUT, reason
                )
            return task
        for target in SUCCESS_PATH:
            task = await self.transition_task(task_id, target, reason)
        return task

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

        # A conversation Task sits in AWAITING_INPUT between turns; starting a
        # turn moves it back to EXECUTING before any loop sees it.
        current = await self.get_task(task_id)
        if current.state is TaskState.AWAITING_INPUT:
            await self.transition_task(
                task_id, TaskState.EXECUTING, "conversation turn started"
            )

        from .conversation_loop.loop import ConversationAgentLoop

        return await ConversationAgentLoop(self).run(
            task_id, normalized_text, user_blocks,
            max_model_calls=max_model_calls,
            max_tool_calls=max_tool_calls,
            max_output_tokens=max_output_tokens,
            tool_timeout_seconds=tool_timeout_seconds,
            on_text_delta=on_text_delta, on_progress=on_progress,
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
            read_hits_state=read_hits_state,
            artifact_read_state=artifact_read_state,
            progressive_scope_state=checkpoint.progressive_scope_state,
            exploration_budget_state=exploration_budget_state,
            stop_or_pivot_state=checkpoint.stop_or_pivot_state,
            evidence_relation_state=checkpoint.evidence_relation_state,
            rejection_loop_state=checkpoint.rejection_loop_state,
            exploration_outcome_state=checkpoint.exploration_outcome_state,
            finalization_state=checkpoint.finalization_state,
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
        from .conversation_loop.loop import ConversationAgentLoop

        return await ConversationAgentLoop(self).continue_after_approval(
            resumed, visible_tools, tool_message,
            on_text_delta=on_text_delta,
            on_progress=on_progress,
        )

    async def resume_checkpointed_agent_turn(
        self, task_id: str,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ) -> AgentTurnResult | AgentTurnSuspended | AgentClarificationSuspended:
        from .conversation_loop.loop import ConversationAgentLoop

        return await ConversationAgentLoop(self).resume_checkpoint(
            task_id, on_text_delta=on_text_delta, on_progress=on_progress,
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
    ) -> AgentTurnResult | AgentTurnSuspended | AgentClarificationSuspended:
        """Migrate a persisted pre-conversation continuation into this loop.

        New turns never create this boundary. The compatibility entry point only
        keeps durable checkpoints readable for the unchanged CLI and SDK routes.
        """
        normalized = user_text.strip()
        if not normalized or not input_id.strip():
            raise ValueError("continuation input and input_id are required")
        await self._close_cancellation_scope(task_id)
        await self.recover_workspace_transactions(task_id)
        await self.reconcile_background_processes(task_id)
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
        visible_tools = await self.list_tools()
        compatibility = await self._evaluate_checkpoint_compatibility(
            task, checkpoint, visible_tools
        )
        if compatibility.action is CheckpointCompatibilityAction.RECONCILE_REQUIRED:
            checkpoint = await self._reconcile_agent_checkpoint(
                task, checkpoint, visible_tools, compatibility
            )
        elif compatibility.action is CheckpointCompatibilityAction.REBASE_REQUIRED:
            checkpoint = await self._rebase_agent_checkpoint(
                task, checkpoint, visible_tools, compatibility
            )
        elif compatibility.action is not CheckpointCompatibilityAction.EXACT_RESUME:
            raise AgentCheckpointConflict(
                "Agent continuation cannot resume safely: "
                + compatibility.reason_code
            )
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        if task.active_agent_checkpoint is None:
            raise LookupError("continuation checkpoint disappeared during validation")
        checkpoint = AgentTurnCheckpoint.from_data(task.active_agent_checkpoint)
        if checkpoint.pending_user_action.get("kind") != "CONTINUATION":
            raise ClarificationNotPending("continuation changed during validation")
        replenished = (
            checkpoint.model_calls >= checkpoint.max_model_calls
            or checkpoint.tool_calls >= checkpoint.max_tool_calls
        )
        continuation_message = Message(
            f"msg-continuation-input-{input_id}", MessageRole.USER,
            (TextBlock(normalized),),
        )
        resumed_checkpoint = replace(
            checkpoint,
            revision=checkpoint.revision + 1,
            messages=checkpoint.messages + (continuation_message,),
            pending_user_action={},
            max_model_calls=(
                checkpoint.model_calls + self._dependencies.default_max_model_calls
                if replenished else checkpoint.max_model_calls
            ),
            max_tool_calls=(
                checkpoint.tool_calls + self._dependencies.default_max_tool_calls
                if replenished else checkpoint.max_tool_calls
            ),
            exploration_budget_state=(
                {} if replenished else checkpoint.exploration_budget_state
            ),
        )
        resumed = task.transition(TaskState.EXECUTING).with_agent_checkpoint(
            resumed_checkpoint.to_data()
        )
        events = (
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id,
                stored.last_event_sequence + 1, "continuation.migrated", {
                    "turn_id": checkpoint.turn_id,
                    "input_id": input_id,
                    "input_hash": canonical_hash(normalized),
                    "capacity_replenished": replenished,
                    "orchestration_mode": "CONVERSATION_LOOP",
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id,
                stored.last_event_sequence + 2, "task.state_changed", {
                    "previous_state": task.state.value,
                    "next_state": resumed.state.value,
                    "reason": "historical continuation migrated to conversation loop",
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id,
                stored.last_event_sequence + 3, "checkpoint.saved", {
                    "turn_id": checkpoint.turn_id,
                    "revision": resumed_checkpoint.revision,
                    "checkpoint_hash": resumed_checkpoint.checkpoint_hash,
                    "reason": "conversation-continuation-migrated",
                    "pending_user_action": {},
                },
            ),
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, resumed.to_data(), events
        ))
        from .conversation_loop.loop import ConversationAgentLoop

        return await ConversationAgentLoop(self)._drive(
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
        from .conversation_loop.loop import ConversationAgentLoop

        return await ConversationAgentLoop(self)._drive(
            resumed_checkpoint, visible_tools,
            on_text_delta=on_text_delta, on_progress=on_progress,
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
        from .conversation_loop.loop import ConversationAgentLoop

        return await ConversationAgentLoop(self)._drive(
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



    async def _run_tool_batch(
        self, state: TurnState,
    ) -> AgentTurnSuspended | AgentClarificationSuspended | None:
        """Delegate shared tool execution to the conversation loop batch."""
        from .turn_tool_batch import run_tool_batch

        return await run_tool_batch(self, state)


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
        *,
        turn_id: str | None = None,
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
                turn_id=turn_id,
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
