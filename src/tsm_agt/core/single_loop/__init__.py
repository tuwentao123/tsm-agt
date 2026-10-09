"""Types and projections for the single Agent loop orchestration mode."""

from .binder import TaskBindingRequest, TaskBindingTask, bind_task_input
from .context_projection import ActiveContextProjector, ActiveContextShadow
from .completion_shadow import CompletionShadowValidation, validate_completion_shadow
from .contracts import CompletionGap, CompletionValidation, validate_completion
from .evidence_gate import evaluate_evidence_contract
from .loop import SingleAgentLoop
from .task_projection import TaskOrchestrationProjector
from .types import (
    ActiveContextProjection,
    CompletionClaim,
    IntentAttempt,
    IntentAttemptStatus,
    IntentDependency,
    IntentDependencyKind,
    IntentRequestValidationError,
    IntentSource,
    IntentValidationErrorData,
    OrchestrationMode,
    TaskBindingDecision,
    TaskBindingKind,
    TaskOrchestrationMetadata,
    ToolIntentRequest,
    TurnIntent,
    TurnIntentKind,
)

__all__ = [
    "ActiveContextProjection",
    "ActiveContextProjector",
    "ActiveContextShadow",
    "CompletionClaim",
    "CompletionGap",
    "CompletionShadowValidation",
    "CompletionValidation",
    "IntentAttempt",
    "IntentAttemptStatus",
    "IntentDependency",
    "IntentDependencyKind",
    "IntentRequestValidationError",
    "IntentSource",
    "IntentValidationErrorData",
    "OrchestrationMode",
    "SingleAgentLoop",
    "TaskBindingDecision",
    "TaskBindingKind",
    "TaskBindingRequest",
    "TaskBindingTask",
    "bind_task_input",
    "validate_completion",
    "validate_completion_shadow",
    "evaluate_evidence_contract",
    "TaskOrchestrationMetadata",
    "TaskOrchestrationProjector",
    "ToolIntentRequest",
    "TurnIntent",
    "TurnIntentKind",
]
