"""Provider-neutral model attempt failure and recovery contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .adapter import RuntimeAdapter


class ModelFailureCategory(StrEnum):
    TRANSIENT_PROVIDER = "TRANSIENT_PROVIDER"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CORRECTABLE_PROTOCOL = "CORRECTABLE_PROTOCOL"
    AUTHENTICATION = "AUTHENTICATION"
    CONFIGURATION = "CONFIGURATION"
    CONTEXT_LIMIT = "CONTEXT_LIMIT"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class ModelRetrySafety(StrEnum):
    SAFE_SAME_REQUEST = "SAFE_SAME_REQUEST"
    SAFE_RESAMPLE = "SAFE_RESAMPLE"
    SAFE_WITH_CORRECTION = "SAFE_WITH_CORRECTION"
    SAFE_FALLBACK_TRANSPORT = "SAFE_FALLBACK_TRANSPORT"
    UNSAFE_AFTER_VISIBLE_OUTPUT = "UNSAFE_AFTER_VISIBLE_OUTPUT"
    NEVER = "NEVER"


class ModelRecoveryAction(StrEnum):
    RETRY_SAME_REQUEST = "RETRY_SAME_REQUEST"
    RESAMPLE = "RESAMPLE"
    RESAMPLE_WITH_CORRECTION = "RESAMPLE_WITH_CORRECTION"
    FALLBACK_TRANSPORT = "FALLBACK_TRANSPORT"
    PRESERVE_AND_INTERRUPT = "PRESERVE_AND_INTERRUPT"
    FAIL_TERMINAL = "FAIL_TERMINAL"


@dataclass(frozen=True, slots=True)
class ModelAttemptFailure:
    """Facts about one unusable physical model attempt.

    ``diagnostic_code`` is for logs and diagnostics only. Recovery control must
    use the category, retry safety and visibility/commit facts.
    """

    category: ModelFailureCategory
    retry_safety: ModelRetrySafety
    diagnostic_code: str
    message: str
    response_started: bool = False
    visible_output_emitted: bool = False
    response_committed: bool = False
    provider_request_id: str | None = None
    retry_after_seconds: float | None = None

    def with_visibility(self, visible: bool) -> "ModelAttemptFailure":
        if not visible or self.visible_output_emitted:
            return self
        return ModelAttemptFailure(
            self.category, ModelRetrySafety.UNSAFE_AFTER_VISIBLE_OUTPUT,
            self.diagnostic_code, self.message, True, True,
            self.response_committed, self.provider_request_id,
            self.retry_after_seconds,
        )

    def to_data(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "retry_safety": self.retry_safety.value,
            "diagnostic_code": self.diagnostic_code,
            "message": self.message,
            "response_started": self.response_started,
            "visible_output_emitted": self.visible_output_emitted,
            "response_committed": self.response_committed,
            "provider_request_id": self.provider_request_id,
            "retry_after_seconds": self.retry_after_seconds,
        }


class ModelAttemptFailed(RuntimeError):
    """A physical model attempt did not produce a committable result."""

    def __init__(self, failure: ModelAttemptFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


@dataclass(frozen=True, slots=True)
class ModelRecoveryProbe:
    failure: ModelAttemptFailure
    provider_attempt: int
    max_provider_attempts: int
    fallback_available: bool = False


@dataclass(frozen=True, slots=True)
class ModelRecoveryDecision:
    action: ModelRecoveryAction
    reason_code: str
    delay_seconds: float = 0.0


class ModelRecoveryExhausted(RuntimeError):
    """The unified recovery policy cannot safely produce another attempt."""

    def __init__(
        self, failure: ModelAttemptFailure, decision: ModelRecoveryDecision,
        attempts: int,
    ) -> None:
        self.failure = failure
        self.decision = decision
        self.attempts = attempts
        super().__init__(failure.message)


class ModelRecoveryPolicyPort(RuntimeAdapter, Protocol):
    async def evaluate(
        self, probe: ModelRecoveryProbe,
    ) -> ModelRecoveryDecision: ...
