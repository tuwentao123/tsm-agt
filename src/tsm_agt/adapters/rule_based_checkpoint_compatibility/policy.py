"""Conservative, project-neutral checkpoint compatibility decisions."""

from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, CheckpointCompatibilityAction,
    CheckpointCompatibilityDecision, CheckpointCompatibilityProbe,
    HealthState, HealthStatus,
)


class RuleBasedCheckpointCompatibilityPolicy:
    """Allow runtime upgrades only when no execution side effect exists.

    Kernel supplies persisted facts. This adapter does not inspect user text,
    understand a project type, modify a checkpoint, or execute a Tool. It can
    therefore be replaced without changing the recovery workflow.
    """

    descriptor = AdapterDescriptor(
        "builtin.rule-based-checkpoint-compatibility", "1.0.0",
        "CheckpointCompatibilityPolicyPort", "1.0",
        frozenset({"project-neutral", "side-effect-aware", "replaceable"}),
    )

    _HARD_IDENTITY_DIFFERENCES = frozenset({
        "task_id", "session_id", "session_missing", "session_state",
        "local_subject", "workspace_fingerprint",
        "working_memory_hash",
        "steering_inbound_sequence", "effective_configuration_missing",
    })
    _SAFE_REBASE_DIFFERENCES = frozenset({
        "session_context_hash", "effective_config_hash",
        "prompt_manifest_hash", "model_configuration",
        "provider_capabilities_hash", "policy_hash",
        "adapter_lock_hash", "toolset_hash",
    })

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "checkpoint compatibility policy ready"
            if self._started else "not started",
        )

    async def evaluate(
        self, probe: CheckpointCompatibilityProbe,
    ) -> CheckpointCompatibilityDecision:
        if not self._started:
            raise RuntimeError("checkpoint compatibility policy is not started")
        differences = tuple(dict.fromkeys(probe.differences))
        reconcilable = tuple(dict.fromkeys(probe.reconcilable_differences))
        hard = tuple(
            item for item in differences
            if item in self._HARD_IDENTITY_DIFFERENCES
        )
        unknown = tuple(
            item for item in differences
            if item not in self._HARD_IDENTITY_DIFFERENCES
            and item not in self._SAFE_REBASE_DIFFERENCES
        )
        if probe.unknown_outcome_count or probe.running_non_idempotent_count:
            reasons = differences + ((
                "unknown_tool_execution_outcome"
                if probe.unknown_outcome_count else
                "running_non_idempotent_tool"
            ),)
            return CheckpointCompatibilityDecision(
                CheckpointCompatibilityAction.BLOCKED,
                "unsafe_tool_execution_state", tuple(dict.fromkeys(reasons)),
            )
        if (
            differences
            and set(differences).issubset(
                set(reconcilable) | self._SAFE_REBASE_DIFFERENCES
            )
            and probe.committed_pending_tool_count > 0
            and probe.committed_pending_tool_count
            == probe.pending_tool_call_count
        ):
            return CheckpointCompatibilityDecision(
                CheckpointCompatibilityAction.RECONCILE_REQUIRED,
                "checkpoint_projection_refresh_required",
                conflict_reasons=differences,
            )
        if hard or unknown:
            return CheckpointCompatibilityDecision(
                CheckpointCompatibilityAction.BLOCKED,
                "checkpoint_identity_conflict",
                tuple(dict.fromkeys(hard + unknown)),
            )
        if not differences:
            return CheckpointCompatibilityDecision(
                CheckpointCompatibilityAction.EXACT_RESUME,
                "safe_checkpoint_available",
            )
        validation_reasons: list[str] = []
        if probe.pending_tool_call_count:
            validation_reasons.append("pending_tool_calls")
        if probe.tool_execution_count:
            validation_reasons.append("tool_executions")
        if probe.mutation_count:
            validation_reasons.append("workspace_mutations")
        if probe.background_process_count:
            validation_reasons.append("background_processes")
        if validation_reasons:
            return CheckpointCompatibilityDecision(
                CheckpointCompatibilityAction.REQUIRES_VALIDATION,
                "runtime_upgrade_has_execution_state",
                tuple(dict.fromkeys(differences + tuple(validation_reasons))),
            )
        return CheckpointCompatibilityDecision(
            CheckpointCompatibilityAction.REBASE_REQUIRED,
            "safe_runtime_upgrade_rebase", rebase_reasons=differences,
            discard_pending_tool_calls=True, reset_transient_state=True,
        )
