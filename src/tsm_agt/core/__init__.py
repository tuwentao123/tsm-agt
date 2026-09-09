"""Microkernel package. Concrete adapters are forbidden here."""

from .agent_loop import (
    AgentProgress, AgentProgressKind,
    AgentCheckpointConflict,
    AgentLoopLimitExceeded,
    AgentTurnCheckpoint,
    AgentTurnResult,
    AgentTurnSuspended,
    AgentClarificationSuspended,
    ProviderCapabilityMismatch,
)
from .configuration import (
    EffectiveConfigurationSnapshot, canonical_hash, effective_toolset_hash,
)
from .context import (
    ContextBudget, ContextBudgetAllocation, ContextCompactionReceipt, ContextWindowExceeded,
    ContextWindowManager, PreparedContext,
)
from .prompt import (
    PromptAssembly, PromptAssemblyReceipt, PromptManifest, PromptTemplate,
    PromptTemplateSegment,
)
from .flow import (
    FlowDiagnosticCategory, FlowDiagnosticFact, FlowEdge, FlowEdgeRelation,
    FlowFailureAttribution, FlowLane, FlowNode, FlowNodeDiagnostic,
    FlowNodeKind, FlowNodeStatus,
    FlowProjection, FlowProjectionError, FlowProjectionView, FlowProjector,
    FlowTimeline, FlowTimelineItem,
    build_flow_export_document,
)
from .approval import (
    ApprovalKind,
    ApprovalDecision,
    ApprovalNotPending,
    ApprovalPayloadMismatch,
    ApprovalRequest,
    ApprovalRequired,
)
from .workspace_access import WorkspaceAccessCapability, WorkspaceAccessGrant
from .clarification import (
    ClarificationChoice, ClarificationNotPending, ClarificationRequest,
    ClarificationRequired, ClarificationTokenMismatch,
)
from .kernel import Kernel, KernelDependencies
from .replay import (
    FlowReplay, FlowReplayBoundary, FlowReplayFrame, FlowReplayIndex,
    FlowReplayPlayback, FlowReplayPlaybackFrame, FlowReplaySnapshot,
    FlowReplaySpeed, FlowReplayTarget,
)
from .execution import (
    IdempotencyConflict,
    ToolCommitState,
    ToolExecutionInProgress,
    ToolExecutionRecord,
)
from .policy import CoreToolPolicy, PolicyAction, PolicyDecision
from .process import (
    BackgroundProcessRecord,
    BackgroundProcessState,
    ProcessSandboxDenied,
    SupervisedProcessResult,
)
from .task import InvalidTaskTransition, TaskNotFound, TaskSnapshot, TaskState
from .task_spec import (
    TaskAcceptanceCriterion, TaskCriterionKind, TaskSpecProjector,
    TaskSpecSnapshot,
)
from .session import SessionSnapshot, SessionState, standalone_session_id
from .session_context import (
    SessionContextProjector, SessionConversationMessage,
    SessionActiveCheckpoint, SessionConversationProjection,
    SessionPromptProjection, SessionTaskSummary,
    SessionWorkingState,
)
from .session_resources import (
    SessionQuestionReference, SessionResourceKind, SessionResourceReference,
)
from .working_memory import (
    EffectiveWorkingMemory, EffectiveWorkingMemoryProjector,
    WorkingEvidenceReference, WorkingMemoryProjector, WorkingMemorySnapshot,
    WorkingPlanStep, WorkingPlanStepStatus,
)
from .steering import (
    SteeringInput, SteeringKind, SteeringProjection, SteeringProjector,
)
from .runtime_input import (
    FollowUpMode, QueuedFollowUp,
    RuntimeInputContext, RuntimeInputIntent, RuntimeInputRoute, RuntimeInputRouter,
    SessionContinuationDecision, SessionContinuationMode,
    SessionResumeCandidate, SessionResumeSafety,
    SessionInputAction, SessionInputDecision,
)
from .plan_guard import (
    ActionProgressState, GoalSlice, PlanGuard, PlanGuardDecision,
)
from .trust import ProjectTrustBinding, ProjectTrustLevel
from .memory import MemoryRecord, MemoryScope, MemorySourceKind, MemoryView
from .onboarding import (
    ONBOARDING_PHASES, OnboardingCheckpoint, OnboardingFact, OnboardingSource,
    ProjectOnboardingScanner, ProjectOnboardingSnapshot,
)
from .project_instructions import (
    MAX_PROJECT_INSTRUCTIONS_BYTES, PROJECT_INSTRUCTIONS_PATH,
    ProjectInstructionsSnapshot, load_project_instructions,
)
from .workspace import (
    MutationOperation, MutationRecord, WorkspaceBaseline, WorkspaceChangeSet,
    WorkspaceFileSnapshot, WorkspaceMutationConflict,
)
from .verification import (
    AcceptanceResult, AcceptanceStatus, Evidence, TaskVerificationResult,
)
from .evidence_question import (
    EvidenceObservationKind, EvidenceQuestionProjector,
    EvidenceQuestionProjection, EvidenceQuestionRecord, EvidenceQuestionStatus,
    ToolActionDisposition,
)
from .turn import InvalidModelResponse, InvalidTurnState, ModelInvocationFailed, TurnResult
from .tool import (
    DuplicateToolName,
    InvalidToolArguments,
    InvalidToolResult,
    ToolNotFound,
)

__all__ = [
    "AgentLoopLimitExceeded",
    "AgentProgress",
    "AgentProgressKind",
    "AgentCheckpointConflict",
    "EffectiveConfigurationSnapshot",
    "canonical_hash",
    "effective_toolset_hash",
    "ContextBudget",
    "ContextBudgetAllocation",
    "ContextCompactionReceipt",
    "ContextWindowExceeded",
    "ContextWindowManager",
    "PreparedContext",
    "PromptAssembly",
    "PromptAssemblyReceipt",
    "PromptManifest",
    "PromptTemplate",
    "PromptTemplateSegment",
    "AgentTurnCheckpoint",
    "FlowDiagnosticCategory", "FlowDiagnosticFact", "FlowEdge",
    "FlowEdgeRelation", "FlowFailureAttribution", "FlowLane", "FlowNode",
    "FlowNodeDiagnostic", "FlowNodeKind",
    "FlowNodeStatus", "FlowProjection", "FlowProjectionError",
    "FlowProjectionView",
    "FlowProjector",
    "FlowTimeline",
    "FlowTimelineItem",
    "build_flow_export_document",
    "FlowReplay",
    "FlowReplayBoundary",
    "FlowReplayFrame",
    "FlowReplayIndex",
    "FlowReplayPlayback",
    "FlowReplayPlaybackFrame",
    "FlowReplaySnapshot",
    "FlowReplaySpeed",
    "FlowReplayTarget",
    "AgentTurnResult",
    "AgentTurnSuspended",
    "AgentClarificationSuspended",
    "ApprovalDecision",
    "ApprovalKind",
    "ApprovalNotPending",
    "ApprovalPayloadMismatch",
    "ApprovalRequest",
    "ApprovalRequired",
    "WorkspaceAccessCapability",
    "WorkspaceAccessGrant",
    "ClarificationChoice",
    "ClarificationNotPending",
    "ClarificationRequest",
    "ClarificationRequired",
    "ClarificationTokenMismatch",
    "ProviderCapabilityMismatch",
    "InvalidModelResponse",
    "InvalidTaskTransition",
    "InvalidTurnState",
    "Kernel",
    "KernelDependencies",
    "RuntimeInputContext",
    "FollowUpMode",
    "QueuedFollowUp",
    "RuntimeInputIntent",
    "RuntimeInputRoute",
    "RuntimeInputRouter",
    "SessionContinuationDecision",
    "SessionContinuationMode",
    "SessionResumeCandidate",
    "SessionResumeSafety",
    "SessionInputAction",
    "SessionInputDecision",
    "IdempotencyConflict",
    "ToolCommitState",
    "ToolExecutionInProgress",
    "ToolExecutionRecord",
    "CoreToolPolicy",
    "PolicyAction",
    "PolicyDecision",
    "ProcessSandboxDenied",
    "BackgroundProcessRecord",
    "BackgroundProcessState",
    "SupervisedProcessResult",
    "ModelInvocationFailed",
    "TaskNotFound",
    "TaskSnapshot",
    "TaskState",
    "TaskAcceptanceCriterion",
    "TaskCriterionKind",
    "TaskSpecProjector",
    "TaskSpecSnapshot",
    "SessionSnapshot",
    "SessionState",
    "standalone_session_id",
    "SessionContextProjector",
    "SessionConversationMessage",
    "SessionConversationProjection",
    "SessionActiveCheckpoint",
    "SessionPromptProjection",
    "SessionTaskSummary",
    "SessionWorkingState",
    "SessionQuestionReference",
    "SessionResourceKind",
    "SessionResourceReference",
    "WorkingEvidenceReference",
    "EffectiveWorkingMemory",
    "EffectiveWorkingMemoryProjector",
    "WorkingMemoryProjector",
    "WorkingMemorySnapshot",
    "WorkingPlanStep",
    "WorkingPlanStepStatus",
    "SteeringInput",
    "SteeringKind",
    "SteeringProjection",
    "SteeringProjector",
    "ActionProgressState",
    "GoalSlice",
    "PlanGuard",
    "PlanGuardDecision",
    "ProjectTrustBinding",
    "ProjectTrustLevel",
    "MemoryRecord",
    "MemoryScope",
    "MemorySourceKind",
    "MemoryView",
    "ONBOARDING_PHASES",
    "OnboardingCheckpoint",
    "OnboardingFact",
    "OnboardingSource",
    "ProjectOnboardingScanner",
    "MAX_PROJECT_INSTRUCTIONS_BYTES",
    "PROJECT_INSTRUCTIONS_PATH",
    "ProjectInstructionsSnapshot",
    "load_project_instructions",
    "ProjectOnboardingSnapshot",
    "WorkspaceBaseline",
    "WorkspaceChangeSet",
    "WorkspaceFileSnapshot",
    "MutationOperation",
    "MutationRecord",
    "WorkspaceMutationConflict",
    "AcceptanceResult",
    "AcceptanceStatus",
    "Evidence",
    "TaskVerificationResult",
    "EvidenceObservationKind",
    "EvidenceQuestionProjector",
    "EvidenceQuestionProjection",
    "EvidenceQuestionRecord",
    "EvidenceQuestionStatus",
    "TurnResult",
    "DuplicateToolName",
    "InvalidToolArguments",
    "InvalidToolResult",
    "ToolNotFound",
]
