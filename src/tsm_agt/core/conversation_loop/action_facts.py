"""Project model-visible tool traffic into compact Runtime action facts.

The conversation loop must distinguish a tool invocation from the observable
outcome of the action it attempted. This module deliberately does *not* read
user prose or choose a recovery: it reads only a ToolCall, its ToolResult, and
the immutable metadata of the ToolSpec exposed for the turn.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tsm_agt.ports import (
    Message, ToolCallBlock, ToolEffect, ToolResultAuthority, ToolResultBlock,
    ToolRecoveryKind, ToolSpec,
)

from ..execution import ToolCommitState
from ..execution_facts import EffectStatus, classify_effect_status
from ..verification_command import is_verification_request


_DELIVERY_EFFECTS = frozenset({ToolEffect.MUTATE, ToolEffect.EXECUTE})


def _invocation_state_for(result: Any) -> ToolCommitState:
    """Recover the durable invocation state representable in a ToolResult.

    Conversation messages retain the tool result rather than the complete
    execution ledger. ``UNKNOWN_OUTCOME`` is therefore the one terminal state
    which must be recovered from the producer's declared result semantics
    instead of treating it as an ordinary failed delivery.
    """

    if (
        result.error_code == "UNKNOWN_OUTCOME"
        or result.effective_recovery_kind is ToolRecoveryKind.UNKNOWN_OUTCOME
    ):
        return ToolCommitState.UNKNOWN_OUTCOME
    if result.error_code == "CANCELLED":
        return ToolCommitState.CANCELLED
    return ToolCommitState.COMMITTED


@dataclass(frozen=True, slots=True)
class RuntimeActionFact:
    """One Runtime-observed action result, never a statement of task success."""

    ordinal: int
    call_id: str
    tool_name: str
    effect: ToolEffect
    result_authority: ToolResultAuthority
    invocation_succeeded: bool
    effect_status: EffectStatus | None
    failure_code: str = ""
    recovery_kind: str = ""
    execution_state: str = ""
    exit_code: int | None = None
    is_recognized_verification: bool = False

    @property
    def is_delivery_action(self) -> bool:
        return self.effect in _DELIVERY_EFFECTS

    @property
    def failed(self) -> bool:
        return self.effect_status is EffectStatus.FAILED

    @property
    def succeeded(self) -> bool:
        return self.effect_status is EffectStatus.SUCCEEDED

    @property
    def can_request_recovery(self) -> bool:
        """Whether one recovery nudge is meaningful from Runtime facts alone.

        A denied/terminal invocation is already an observable blocker. In
        contrast, a process that ran and exited non-zero has a real failed
        action, while an explicitly retryable invocation has a declared path
        to another attempt.
        """

        return self.failed and (
            self.invocation_succeeded
            or self.recovery_kind in {
                ToolRecoveryKind.RETRY_SAME.value,
                ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS.value,
                ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE.value,
                ToolRecoveryKind.UNKNOWN_OUTCOME.value,
            }
        )

    def prompt_data(self) -> dict[str, object]:
        """Return a small, non-sensitive fact block for the next model call."""

        data: dict[str, object] = {
            "tool": self.tool_name,
            "call_id": self.call_id,
            "invocation_status": (
                "succeeded" if self.invocation_succeeded else "failed"
            ),
            "effect": self.effect.value,
            "operation_outcome": (
                self.effect_status.value if self.effect_status is not None
                else "not_applicable"
            ),
        }
        if self.execution_state:
            data["execution_state"] = self.execution_state
        if self.failure_code:
            data["failure_code"] = self.failure_code
        if self.exit_code is not None:
            data["exit_code"] = self.exit_code
        if self.is_recognized_verification:
            data["verification_attempt"] = True
        return data


@dataclass(frozen=True, slots=True)
class PostMutationVerificationFact:
    """What the ledger observed after the latest successful workspace mutation.

    ``missing`` does not claim that every mutation needs a test. It only says
    that no recognized test/build observation has been recorded after it, which
    lets the model decide whether to verify or to give an evidence-based reason
    why a verifier is not applicable.
    """

    status: str
    mutation: RuntimeActionFact | None = None
    verification: RuntimeActionFact | None = None

    def prompt_data(self) -> dict[str, object] | None:
        if self.status == "not_applicable" or self.mutation is None:
            return None
        data: dict[str, object] = {
            "status": self.status,
            "mutation_call_id": self.mutation.call_id,
            "mutation_tool": self.mutation.tool_name,
        }
        if self.verification is not None:
            data["verification"] = self.verification.prompt_data()
        return data


def project_runtime_action_facts(
    messages: tuple[Message, ...] | list[Message],
    tool_specs: Mapping[str, ToolSpec],
) -> tuple[RuntimeActionFact, ...]:
    """Project durable ToolCall/ToolResult pairs in chronological order."""

    calls: dict[str, Any] = {}
    facts: list[RuntimeActionFact] = []
    for message in messages:
        for block in message.content:
            if isinstance(block, ToolCallBlock):
                calls[block.call.call_id] = block.call
                continue
            if not isinstance(block, ToolResultBlock):
                continue
            result = block.result
            call = calls.get(result.call_id)
            if call is None:
                # A malformed/legacy result cannot be safely bound to a tool
                # effect. Its own tool message remains visible, but it must not
                # become a delivery fact.
                continue
            spec = tool_specs.get(call.name)
            effect = spec.effect if spec is not None else ToolEffect.UNSPECIFIED
            authority = (
                spec.result_authority
                if spec is not None else ToolResultAuthority.UNSPECIFIED
            )
            data = result.data if isinstance(result.data, Mapping) else {}
            classified = classify_effect_status(
                effect,
                authority,
                invocation_state=_invocation_state_for(result),
                ok=result.ok,
                data=data,
                error_code=result.error_code or "",
            )
            status, code = classified if classified is not None else (None, "")
            raw_exit_code = data.get("exit_code")
            facts.append(RuntimeActionFact(
                ordinal=len(facts),
                call_id=call.call_id,
                tool_name=call.name,
                effect=effect,
                result_authority=authority,
                invocation_succeeded=result.ok,
                effect_status=status,
                failure_code=code,
                recovery_kind=(
                    result.effective_recovery_kind.value
                    if not result.ok else ""
                ),
                execution_state=str(data.get("execution_state", "")).strip(),
                exit_code=(
                    raw_exit_code if isinstance(raw_exit_code, int)
                    and not isinstance(raw_exit_code, bool) else None
                ),
                is_recognized_verification=(
                    call.name == "core.run_command"
                    and is_verification_request(call.arguments)
                ),
            ))
    return tuple(facts)


def latest_failed_delivery_action(
    facts: tuple[RuntimeActionFact, ...],
) -> RuntimeActionFact | None:
    """Return the latest failed mutate/execute action, if it is still latest."""

    latest = next(
        (fact for fact in reversed(facts) if fact.is_delivery_action), None
    )
    return latest if latest is not None and latest.can_request_recovery else None


def post_mutation_verification_fact(
    facts: tuple[RuntimeActionFact, ...],
) -> PostMutationVerificationFact:
    """Describe the latest recognized verification attempt after a mutation."""

    mutation = next(
        (
            fact for fact in reversed(facts)
            if fact.effect is ToolEffect.MUTATE and fact.succeeded
        ),
        None,
    )
    if mutation is None:
        return PostMutationVerificationFact("not_applicable")
    attempts = tuple(
        fact for fact in facts
        if fact.ordinal > mutation.ordinal and fact.is_recognized_verification
    )
    if not attempts:
        return PostMutationVerificationFact("missing", mutation)
    latest = attempts[-1]
    if latest.succeeded:
        return PostMutationVerificationFact("passed", mutation, latest)
    if latest.failed:
        return PostMutationVerificationFact("failed", mutation, latest)
    return PostMutationVerificationFact("unknown", mutation, latest)
