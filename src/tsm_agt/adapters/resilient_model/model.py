"""Coordinate bounded physical attempts for one logical model round."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable
from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthStatus, ModelAttemptFailed,
    ModelFailureCategory, ModelAttemptFailure, ModelProviderPort, ModelRequest,
    ModelResponse, ModelRecoveryAction, ModelRecoveryDecision,
    ModelRecoveryExhausted,
    ModelRecoveryPolicyPort, ModelRecoveryProbe, ModelRetrySafety,
    ModelStreamCompleted, ModelStreamEvent, ModelTextDelta, ModelReasoningDelta,
    ModelTransportProgress, StreamingModelProviderPort,
    RecoverableToolProtocolError,
)


class ResilientModelProvider:
    """Expose one logical model while coordinating its physical attempts."""

    def __init__(
        self, provider: ModelProviderPort, policy: ModelRecoveryPolicyPort, *,
        max_provider_attempts: int = 3,
        network_wait_limit_seconds: float = 0.0,
        network_probe: Callable[[], Awaitable[bool]] | None = None,
        network_probe_max_interval_seconds: float = 15.0,
        max_network_resumes: int = 3,
    ) -> None:
        if max_provider_attempts < 1:
            raise ValueError("max_provider_attempts must be positive")
        if network_wait_limit_seconds < 0:
            raise ValueError("network_wait_limit_seconds must not be negative")
        if network_probe_max_interval_seconds <= 0:
            raise ValueError("network_probe_max_interval_seconds must be positive")
        if max_network_resumes < 0:
            raise ValueError("max_network_resumes must not be negative")
        self._provider = provider
        self._policy = policy
        self._max_provider_attempts = max_provider_attempts
        #: How long one logical round may stay parked on an unreachable network,
        #: and how often a flapping network may restart that round.
        self._network_wait_limit_seconds = network_wait_limit_seconds
        self._network_probe = network_probe
        self._network_probe_max_interval_seconds = network_probe_max_interval_seconds
        self._max_network_resumes = max_network_resumes
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

    def capabilities_for(self, profile_id=None):
        lookup = getattr(self._provider, "capabilities_for", None)
        return lookup(profile_id) if lookup else self.capabilities

    def output_token_limit_for(self, profile_id=None):
        lookup = getattr(self._provider, "output_token_limit_for", None)
        return lookup(profile_id) if lookup else None

    def resolve_profile(self, profile_id=None):
        lookup = getattr(self._provider, "resolve_profile", None)
        if lookup:
            return lookup(profile_id)
        if profile_id is not None:
            raise ValueError("this model provider has no selectable profiles")
        return None

    def profile_catalog(self):
        lookup = getattr(self._provider, "profile_catalog", None)
        return lookup() if lookup else {"default_profile": None, "profiles": []}

    def summary_reasoning_effort_for(self, profile_id=None):
        lookup = getattr(self._provider, "summary_reasoning_effort_for", None)
        return lookup(profile_id) if lookup else None

    def profile_fingerprint(self, profile_id=None):
        lookup = getattr(self._provider, "profile_fingerprint", None)
        return lookup(profile_id) if lookup else None

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
        max_attempts: int | None = None, streaming: bool = False,
        network_waited: float = 0.0,
    ):
        return await self._policy.evaluate(ModelRecoveryProbe(
            failure, attempt, max_attempts or self._max_provider_attempts,
            fallback, streaming,
            network_wait_limit_seconds=self._network_wait_limit_seconds,
            network_waited_seconds=network_waited,
        ))

    async def complete(self, request: ModelRequest) -> ModelResponse:
        attempt = 1
        max_attempts = request.max_provider_attempts or self._max_provider_attempts
        network_waited = 0.0
        network_resumes = 0
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
                    failure, attempt, fallback=False, max_attempts=max_attempts,
                    network_waited=network_waited,
                )
                await self._report_failure(
                    request, failure, decision, attempt, max_attempts
                )
                if decision.action is ModelRecoveryAction.WAIT_FOR_NETWORK:
                    network_waited, network_resumes = await self._wait_for_network(
                        request, error, failure, decision, attempt, max_attempts,
                        network_waited, network_resumes,
                    )
                    # A recovered network reopens the request: attempts that
                    # never left this machine do not consume the budget.
                    attempt = 1
                    continue
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
        network_waited = 0.0
        network_resumes = 0
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
                        if isinstance(event, (ModelTextDelta, ModelReasoningDelta)):
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
                    max_attempts=max_attempts, streaming=not use_fallback,
                    network_waited=network_waited,
                )
                await self._report_failure(
                    request, failure, decision, attempt, max_attempts
                )
                if decision.action is ModelRecoveryAction.WAIT_FOR_NETWORK:
                    network_waited, network_resumes = await self._wait_for_network(
                        request, error, failure, decision, attempt, max_attempts,
                        network_waited, network_resumes,
                    )
                    # A recovered network reopens the request: attempts that
                    # never left this machine do not consume the budget, and a
                    # transport fallback chosen for a dead network is stale.
                    attempt = 1
                    use_fallback = False
                    continue
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

    async def _wait_for_network(
        self, request, error, failure, decision, attempt, max_attempts,
        waited_so_far: float, resumes: int,
    ) -> tuple[float, int]:
        """Park the interrupted round until the endpoint answers again.

        Two bounds keep this honest: one round may not wait longer than the
        configured budget, and a flapping network may not reopen the round
        forever. Either limit interrupts instead of silently spinning.
        """

        def exhausted(reason: str) -> ModelRecoveryExhausted:
            return ModelRecoveryExhausted(
                failure,
                ModelRecoveryDecision(
                    ModelRecoveryAction.PRESERVE_AND_INTERRUPT, reason,
                    decision.delay_seconds,
                ),
                attempt,
            )

        probe = self._network_probe
        remaining_budget = self._network_wait_limit_seconds - waited_so_far
        if probe is None or remaining_budget <= 0:
            raise exhausted("network_wait_limit_reached") from error
        if resumes >= self._max_network_resumes:
            raise exhausted("network_resume_limit_reached") from error
        loop = asyncio.get_running_loop()
        deadline = loop.time() + remaining_budget
        started = loop.time()
        interval = decision.delay_seconds or 1.0
        common = dict(
            category=failure.category.value,
            diagnostic_code=failure.diagnostic_code,
        )
        await self._notify(request, ModelTransportProgress(
            "network_waiting", attempt, max_attempts,
            reason="network_unavailable_wait_for_recovery",
            delay_seconds=interval, waited_seconds=waited_so_far, **common,
        ))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise exhausted("network_wait_limit_reached") from error
            await asyncio.sleep(min(interval, remaining))
            waited = waited_so_far + (loop.time() - started)
            try:
                available = bool(await probe())
            except Exception:
                # A probe that cannot answer is the same fact as "still down".
                available = False
            if available:
                await self._notify(request, ModelTransportProgress(
                    "network_recovered", attempt, max_attempts,
                    reason="network_recovered_retrying",
                    waited_seconds=waited, **common,
                ))
                return waited, resumes + 1
            interval = min(interval * 2, self._network_probe_max_interval_seconds)
            await self._notify(request, ModelTransportProgress(
                "network_waiting", attempt, max_attempts,
                reason="network_unavailable_wait_for_recovery",
                delay_seconds=interval, waited_seconds=waited, **common,
            ))

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
            diagnostics=failure.diagnostics,
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
