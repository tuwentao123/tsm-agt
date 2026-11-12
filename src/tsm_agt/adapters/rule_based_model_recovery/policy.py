"""Conservative recovery decisions from structured attempt facts."""

from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
    ModelFailureCategory, ModelRecoveryAction, ModelRecoveryDecision,
    ModelRecoveryProbe, ModelRetrySafety,
)


class RuleBasedModelRecoveryPolicy:
    """Choose safe recovery without inspecting user text or project type."""

    descriptor = AdapterDescriptor(
        "builtin.rule-based-model-recovery", "1.0.0",
        "ModelRecoveryPolicyPort", "1.0",
        frozenset({"project-neutral", "visibility-aware", "bounded"}),
    )

    def __init__(self, retry_backoff_seconds: float = 1.0) -> None:
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        self._retry_backoff_seconds = retry_backoff_seconds
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "model recovery policy ready" if self._started else "not started",
        )

    async def evaluate(
        self, probe: ModelRecoveryProbe,
    ) -> ModelRecoveryDecision:
        if not self._started:
            raise RuntimeError("model recovery policy is not started")
        failure = probe.failure
        if failure.response_committed or failure.visible_output_emitted:
            return ModelRecoveryDecision(
                ModelRecoveryAction.PRESERVE_AND_INTERRUPT,
                "attempt_has_user_visible_or_committed_output",
            )
        if failure.retry_safety is ModelRetrySafety.NEVER:
            return ModelRecoveryDecision(
                ModelRecoveryAction.FAIL_TERMINAL, "failure_is_not_retryable"
            )
        if failure.category is ModelFailureCategory.NETWORK_UNAVAILABLE:
            # The request never reached a provider that could answer it, so the
            # attempt budget says nothing about whether sending it again is
            # worth doing. Wait for the network instead of spending an attempt;
            # only a spent wait budget makes this an interruption.
            remaining = (
                probe.network_wait_limit_seconds - probe.network_waited_seconds
            )
            if probe.network_wait_limit_seconds <= 0 or remaining <= 0:
                return ModelRecoveryDecision(
                    ModelRecoveryAction.PRESERVE_AND_INTERRUPT,
                    "network_wait_limit_reached",
                )
            return ModelRecoveryDecision(
                ModelRecoveryAction.WAIT_FOR_NETWORK,
                "network_unavailable_wait_for_recovery",
                min(max(self._retry_backoff_seconds, 1.0), remaining),
            )
        if probe.provider_attempt >= probe.max_provider_attempts:
            return ModelRecoveryDecision(
                ModelRecoveryAction.PRESERVE_AND_INTERRUPT,
                "provider_attempt_limit_reached",
            )
        delay = failure.retry_after_seconds
        if delay is None:
            delay = self._retry_backoff_seconds * (2 ** (probe.provider_attempt - 1))
        if (
            probe.streaming
            and probe.fallback_available
            and probe.provider_attempt > 1
            and failure.category is ModelFailureCategory.TRANSIENT_PROVIDER
            and failure.retry_safety is ModelRetrySafety.SAFE_SAME_REQUEST
        ):
            # One identical retry already failed. A long-lived stream that dies
            # at the transport level does not become likelier to survive by
            # repeating it, while the one-shot transport is a genuinely
            # different way to send the same request and remains safe to try.
            return ModelRecoveryDecision(
                ModelRecoveryAction.FALLBACK_TRANSPORT,
                "stream_transport_unstable_fallback_available",
                delay,
            )
        mapping = {
            ModelRetrySafety.SAFE_SAME_REQUEST: (
                ModelRecoveryAction.RETRY_SAME_REQUEST, "safe_same_request"
            ),
            ModelRetrySafety.SAFE_RESAMPLE: (
                ModelRecoveryAction.RESAMPLE, "safe_uncommitted_resample"
            ),
            ModelRetrySafety.SAFE_WITH_CORRECTION: (
                ModelRecoveryAction.RESAMPLE_WITH_CORRECTION,
                "semantic_correction_required",
            ),
            ModelRetrySafety.SAFE_FALLBACK_TRANSPORT: (
                ModelRecoveryAction.FALLBACK_TRANSPORT,
                "safe_transport_fallback",
            ),
        }
        action_reason = mapping.get(failure.retry_safety)
        if action_reason is None:
            return ModelRecoveryDecision(
                ModelRecoveryAction.PRESERVE_AND_INTERRUPT,
                "retry_safety_is_ambiguous",
            )
        action, reason = action_reason
        if action is ModelRecoveryAction.FALLBACK_TRANSPORT and not probe.fallback_available:
            action = ModelRecoveryAction.RETRY_SAME_REQUEST
            reason = "fallback_unavailable_retry_same_request"
        return ModelRecoveryDecision(action, reason, delay)
