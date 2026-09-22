"""Coordinate bounded physical attempts for one logical model round."""

from __future__ import annotations

import asyncio
import inspect
import re
from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthStatus, ModelAttemptFailed,
    ModelFailureCategory, ModelAttemptFailure, ModelProviderPort, ModelRequest,
    ModelResponse, ModelRecoveryAction, ModelRecoveryExhausted,
    ModelRecoveryPolicyPort, ModelRecoveryProbe, ModelRetrySafety,
    ModelStreamCompleted, ModelStreamEvent, ModelTextDelta,
    ModelTransportProgress, StreamingModelProviderPort,
    RecoverableToolProtocolError,
)


class ResilientModelProvider:
    """Expose one logical model while coordinating its physical attempts."""

    def __init__(
        self, provider: ModelProviderPort, policy: ModelRecoveryPolicyPort, *,
        max_provider_attempts: int = 3,
    ) -> None:
        if max_provider_attempts < 1:
            raise ValueError("max_provider_attempts must be positive")
        self._provider = provider
        self._policy = policy
        self._max_provider_attempts = max_provider_attempts
        self._max_retries = max_provider_attempts - 1
        self._retry_backoff_seconds = getattr(
            policy, "_retry_backoff_seconds", 0.0
        )
        self.capabilities = provider.capabilities
        self.descriptor = AdapterDescriptor(
            "builtin.resilient-model", "1.0.0", "ModelProviderPort", "1.0",
            frozenset(set(provider.descriptor.capabilities) | {
                "bounded-recovery", "physical-attempt-accounting",
            }),
        )
        # Compatibility/introspection fields remain read-only views of the
        # physical Provider configuration.
        for name in (
            "_timeout_seconds", "_output_token_parameter",
            "_strict_tool_schema", "_streaming",
        ):
            if hasattr(provider, name):
                setattr(self, name, getattr(provider, name))

    async def start(self, context: AdapterContext) -> None:
        await self._provider.start(context)

    async def health(self) -> HealthStatus:
        return await self._provider.health()

    async def stop(self, deadline: datetime) -> None:
        await self._provider.stop(deadline)

    @staticmethod
    async def _notify(
        request: ModelRequest, update: ModelTransportProgress,
    ) -> None:
        if request.on_transport_progress is None:
            return
        result = request.on_transport_progress(update)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _safe_diagnostic_detail(message: str) -> str:
        """Return a bounded diagnostic safe for local logs and Web UI."""
        detail = message.strip()
        substitutions = (
            (r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***"),
            (r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-***"),
            (
                r"(?i)([\"']?(?:api[_-]?key|token|secret|authorization)"
                r"[\"']?\s*[:=]\s*)"
                r"(?:\"[^\"]*\"|'[^']*'|[^,}\s]+)",
                r'\1"***"',
            ),
        )
        for pattern, replacement in substitutions:
            detail = re.sub(pattern, replacement, detail)
        return detail[:1000]

    @staticmethod
    def _normalize_failure(error: Exception) -> ModelAttemptFailure:
        if isinstance(error, ModelAttemptFailed):
            return error.failure
        return ModelAttemptFailure(
            ModelFailureCategory.UNKNOWN, ModelRetrySafety.NEVER,
            type(error).__name__, str(error) or type(error).__name__,
        )

    async def _decision(
        self, failure: ModelAttemptFailure, attempt: int, *, fallback: bool,
        max_attempts: int | None = None,
    ):
        return await self._policy.evaluate(ModelRecoveryProbe(
            failure, attempt, max_attempts or self._max_provider_attempts, fallback,
        ))

    async def complete(self, request: ModelRequest) -> ModelResponse:
        attempt = 1
        max_attempts = request.max_provider_attempts or self._max_provider_attempts
        while True:
            await self._notify(request, ModelTransportProgress(
                "attempt_started", attempt, max_attempts,
            ))
            try:
                response = await self._provider.complete(request)
                await self._notify(request, ModelTransportProgress(
                    "attempt_completed", attempt, max_attempts,
                ))
                return response
            except RecoverableToolProtocolError:
                # Task/tool semantic correction belongs to the Agent layer.
                raise
            except Exception as error:
                failure = self._normalize_failure(error)
                decision = await self._decision(
                    failure, attempt, fallback=False, max_attempts=max_attempts
                )
                await self._report_failure(
                    request, failure, decision, attempt, max_attempts
                )
                if decision.action not in {
                    ModelRecoveryAction.RETRY_SAME_REQUEST,
                    ModelRecoveryAction.RESAMPLE,
                }:
                    raise ModelRecoveryExhausted(
                        failure, decision, attempt
                    ) from error
                if decision.delay_seconds:
                    await asyncio.sleep(decision.delay_seconds)
                attempt += 1

    async def stream_complete(self, request: ModelRequest):
        attempt = 1
        max_attempts = request.max_provider_attempts or self._max_provider_attempts
        use_fallback = False
        while True:
            await self._notify(request, ModelTransportProgress(
                "attempt_started", attempt, max_attempts,
                transport_mode=("non_streaming" if use_fallback else "streaming"),
            ))
            visible = False
            try:
                if use_fallback or not isinstance(
                    self._provider, StreamingModelProviderPort
                ):
                    response = await self._provider.complete(request)
                    yield ModelStreamCompleted(response)
                else:
                    async for event in self._provider.stream_complete(request):
                        if isinstance(event, ModelTextDelta):
                            visible = True
                        yield event
                await self._notify(request, ModelTransportProgress(
                    "attempt_completed", attempt, max_attempts,
                    transport_mode=("non_streaming" if use_fallback else "streaming"),
                ))
                return
            except RecoverableToolProtocolError:
                # Do not turn a correctable Tool contract response into a
                # transport retry; the Agent must supply the correction.
                raise
            except Exception as error:
                failure = self._normalize_failure(error).with_visibility(visible)
                decision = await self._decision(
                    failure, attempt, fallback=(not use_fallback),
                    max_attempts=max_attempts,
                )
                await self._report_failure(
                    request, failure, decision, attempt, max_attempts
                )
                if decision.action not in {
                    ModelRecoveryAction.RETRY_SAME_REQUEST,
                    ModelRecoveryAction.RESAMPLE,
                    ModelRecoveryAction.FALLBACK_TRANSPORT,
                }:
                    raise ModelRecoveryExhausted(
                        failure, decision, attempt
                    ) from error
                if decision.action is ModelRecoveryAction.FALLBACK_TRANSPORT:
                    use_fallback = True
                if decision.delay_seconds:
                    await asyncio.sleep(decision.delay_seconds)
                attempt += 1

    async def _report_failure(
        self, request, failure, decision, attempt, max_attempts: int,
    ) -> None:
        common = dict(
            category=failure.category.value,
            retry_safety=failure.retry_safety.value,
            diagnostic_code=failure.diagnostic_code,
            diagnostic_detail=self._safe_diagnostic_detail(failure.message),
            visible_output_emitted=failure.visible_output_emitted,
            response_committed=failure.response_committed,
        )
        await self._notify(request, ModelTransportProgress(
            "attempt_failed", attempt, max_attempts,
            reason=decision.reason_code, **common,
        ))
        await self._notify(request, ModelTransportProgress(
            "recovery_decided", attempt, max_attempts,
            reason=decision.reason_code, delay_seconds=decision.delay_seconds,
            recovery_action=decision.action.value, **common,
        ))
