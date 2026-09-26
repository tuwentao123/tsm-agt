"""Pure projection of tool execution records into effect-level facts.

The completion layer must judge from the Runtime's own records, not from model
prose. ``ProcessResult.succeeded`` is already recorded, but a ``ToolResult`` with
``ok=True`` means "the call executed", not "the effect succeeded". This module
turns the immutable execution ledger into an explicit, inspectable verdict.

Design constraints:

* Pure: no IO, no ``await``, no mutation. Independently unit-testable.
* No business dispatch: classification depends only on ``ToolEffect`` and
  ``ToolResultAuthority``, so new tools/skills/MCP tools are covered by
  declaring metadata, not by adding rules here.
* No new persisted entity: facts are derived on demand from existing records.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tsm_agt.ports import ToolEffect, ToolResultAuthority, ToolSpec

from .execution import ToolCommitState, ToolExecutionRecord


class EffectStatus(StrEnum):
    """Whether an effectful action actually achieved its effect."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class FactResolution(StrEnum):
    """Whether a later action resolved an earlier failure."""

    UNRESOLVED = "unresolved"
    RETRIED = "retried"
    SUPERSEDED = "superseded"


#: Effects that never produce an effect-level fact: reads and internal state
#: have no delivery outcome to verify.
_NON_EFFECTFUL = frozenset({
    ToolEffect.OBSERVE, ToolEffect.INTERNAL, ToolEffect.UNSPECIFIED,
})

#: Authorities that prove a real side effect even when the tool forgot to
#: declare ``effect``.
_EFFECTFUL_AUTHORITIES = frozenset({
    ToolResultAuthority.PROCESS_FACT, ToolResultAuthority.MUTATION_FACT,
})

_CANCELLED_CODES = frozenset({"PROCESS_CANCELLED", "CANCELLED"})


@dataclass(frozen=True, slots=True)
class ExecutionFact:
    """One effectful execution and whether its effect landed."""

    execution_id: str
    tool_name: str
    effect: ToolEffect
    result_authority: ToolResultAuthority
    invocation_status: ToolCommitState
    effect_status: EffectStatus
    resolution: FactResolution = FactResolution.UNRESOLVED
    failure_code: str = ""
    detail: str = ""

    @property
    def required_effect(self) -> ToolEffect:
        """The capability a recovery attempt must exercise to close this fact.

        ``CompletionGap`` rejects ``UNSPECIFIED``, so an execution whose tool
        declared no effect falls back to the authority it can establish.
        """
        if self.effect is not ToolEffect.UNSPECIFIED:
            return self.effect
        if self.result_authority is ToolResultAuthority.PROCESS_FACT:
            return ToolEffect.EXECUTE
        if self.result_authority is ToolResultAuthority.MUTATION_FACT:
            return ToolEffect.MUTATE
        return ToolEffect.UNSPECIFIED

    @property
    def is_blocking_failure(self) -> bool:
        """True when this fact must stop a Task from reporting success.

        A cancelled action is ``UNKNOWN`` but is not a failure: the user asked
        for it to stop, so the Runtime must not present it as a completion
        blocker. An ``UNKNOWN_OUTCOME`` (timeout/crash with the side effect
        possibly applied) still is one.
        """
        if self.effect_status is EffectStatus.FAILED:
            return True
        return (
            self.effect_status is EffectStatus.UNKNOWN
            and self.failure_code not in _CANCELLED_CODES
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "tool_name": self.tool_name,
            "effect": self.effect.value,
            "result_authority": self.result_authority.value,
            "invocation_status": self.invocation_status.value,
            "effect_status": self.effect_status.value,
            "resolution": self.resolution.value,
            "failure_code": self.failure_code,
            "detail": self.detail,
        }


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _has_known_mutation(
    data: Mapping[str, Any], known_mutation_ids: frozenset[str] | None,
) -> bool:
    mutation_ids = [
        str(item).strip()
        for item in (
            data.get("mutation_ids")
            if isinstance(data.get("mutation_ids"), (list, tuple))
            else [data.get("mutation_id")]
        )
        if isinstance(item, str) and item.strip()
    ]
    if not mutation_ids:
        return False
    if known_mutation_ids is None:
        return True
    return any(item in known_mutation_ids for item in mutation_ids)


def classify_effect_status(
    effect: ToolEffect,
    authority: ToolResultAuthority,
    *,
    invocation_state: ToolCommitState,
    ok: bool,
    data: Mapping[str, Any] | None = None,
    error_code: str = "",
    known_mutation_ids: frozenset[str] | None = None,
) -> tuple[EffectStatus, str] | None:
    """Classify one execution outcome without needing a persisted record.

    Returns ``(status, failure_code)`` for an effectful action, or ``None`` when
    the action has no verifiable effect. Shared by the ledger projection and by
    the live call path so both judge identically.
    """
    if effect in _NON_EFFECTFUL and authority not in _EFFECTFUL_AUTHORITIES:
        return None
    payload = _mapping(data)
    if invocation_state is ToolCommitState.CANCELLED:
        return (EffectStatus.UNKNOWN, "CANCELLED")
    if invocation_state is ToolCommitState.UNKNOWN_OUTCOME:
        return (EffectStatus.UNKNOWN, "UNKNOWN_OUTCOME")
    if authority is ToolResultAuthority.PROCESS_FACT and (
        str(payload.get("status", "")).strip().lower() == "cancelled"
    ):
        return (EffectStatus.UNKNOWN, "PROCESS_CANCELLED")
    if not ok:
        return (EffectStatus.FAILED, error_code or "TOOL_ERROR")
    if authority is ToolResultAuthority.PROCESS_FACT and (
        payload.get("succeeded") is False
    ):
        return (
            EffectStatus.FAILED,
            str(payload.get("failure_code") or "PROCESS_EXIT_NON_ZERO"),
        )
    if (
        authority is ToolResultAuthority.MUTATION_FACT
        and not _has_known_mutation(payload, known_mutation_ids)
    ):
        return (EffectStatus.UNKNOWN, "MUTATION_NOT_RECORDED")
    return (EffectStatus.SUCCEEDED, "")


def project_execution_fact(
    record: ToolExecutionRecord,
    spec: ToolSpec | None = None,
    *,
    known_mutation_ids: frozenset[str] | None = None,
) -> ExecutionFact | None:
    """Derive one effect-level fact, or ``None`` when there is nothing to judge."""
    # Prefer the semantics frozen on the record at execution time; fall back to
    # the live ToolSpec only for executions recorded before 6.1 existed.
    effect = record.effect
    if effect is ToolEffect.UNSPECIFIED and spec is not None:
        effect = spec.effect
    authority = record.result_authority
    if authority is ToolResultAuthority.UNSPECIFIED and spec is not None:
        authority = spec.result_authority
    if effect in _NON_EFFECTFUL and authority not in _EFFECTFUL_AUTHORITIES:
        return None

    def fact(
        status: EffectStatus, *, failure_code: str = "", detail: str = "",
    ) -> ExecutionFact:
        return ExecutionFact(
            execution_id=record.execution_id,
            tool_name=record.call.name,
            effect=effect,
            result_authority=authority,
            invocation_status=record.state,
            effect_status=status,
            failure_code=failure_code,
            detail=detail,
        )

    # Cancellation and unknown outcome are terminal states that may carry no
    # result at all. They are checked before ``result is None`` so a cancelled
    # side effect is still visible to the caller.
    if record.state is ToolCommitState.CANCELLED:
        return fact(EffectStatus.UNKNOWN, failure_code="CANCELLED")
    if record.state is ToolCommitState.UNKNOWN_OUTCOME:
        return fact(EffectStatus.UNKNOWN, failure_code="UNKNOWN_OUTCOME")

    result = record.result
    if result is None:
        return None
    classified = classify_effect_status(
        effect, authority,
        invocation_state=record.state, ok=result.ok,
        data=result.data, error_code=result.error_code or "",
        known_mutation_ids=known_mutation_ids,
    )
    if classified is None:
        return None
    status, failure_code = classified
    detail = ""
    if status is not EffectStatus.SUCCEEDED:
        detail = (result.message or "")[:500] or str(
            _mapping(result.data).get("stderr_excerpt") or ""
        )[:500]
    return ExecutionFact(
        execution_id=record.execution_id,
        tool_name=record.call.name,
        effect=effect,
        result_authority=authority,
        invocation_status=record.state,
        effect_status=status,
        failure_code=failure_code,
        detail=detail,
    )


def _resolution_key(record: ToolExecutionRecord) -> tuple[Any, str]:
    return (record.updated_at, record.execution_id)


def project_execution_facts(
    executions: Iterable[ToolExecutionRecord],
    specs_by_name: Mapping[str, ToolSpec],
    *,
    known_mutation_ids: frozenset[str] | None = None,
    include_successful: bool = False,
) -> tuple[ExecutionFact, ...]:
    """Project many records, resolving earlier failures against later actions.

    Ordering is deterministic (``updated_at`` then ``execution_id``) so the
    result is stable for replay and for a future parallel batch. The result is
    sorted deterministically; pass ``include_successful=True`` to keep
    ``SUCCEEDED`` facts (needed to compute resolution for the failures).
    """
    ordered = sorted(executions, key=_resolution_key)
    projected: list[tuple[ToolExecutionRecord, ExecutionFact | None]] = [
        (
            record,
            project_execution_fact(
                record, specs_by_name.get(record.call.name),
                known_mutation_ids=known_mutation_ids,
            ),
        )
        for record in ordered
    ]

    resolved: list[ExecutionFact] = []
    signatures = [
        (record.call.name, record.payload_hash)
        for record, _ in projected
    ]
    for index, (record, fact_value) in enumerate(projected):
        if fact_value is None:
            continue
        fact = fact_value
        if fact.effect_status is not EffectStatus.SUCCEEDED:
            signature = signatures[index]
            later = [
                other for other, _ in projected[index + 1:]
            ]
            later_facts = [
                project_execution_fact(
                    other, specs_by_name.get(other.call.name),
                    known_mutation_ids=known_mutation_ids,
                )
                for other in later
            ]
            later_successes = [
                (other, other_fact)
                for other, other_fact in zip(later, later_facts)
                if other_fact is not None
                and other_fact.effect_status is EffectStatus.SUCCEEDED
            ]
            retried = any(
                (other.call.name, other.payload_hash) == signature
                for other, _ in later_successes
            )
            superseded = any(
                other_fact.effect is fact.effect
                for _, other_fact in later_successes
            )
            if retried:
                fact = _with_resolution(fact, FactResolution.RETRIED)
            elif superseded:
                fact = _with_resolution(fact, FactResolution.SUPERSEDED)
        resolved.append(fact)

    if include_successful:
        return tuple(resolved)
    return tuple(
        fact for fact in resolved
        if fact.effect_status is not EffectStatus.SUCCEEDED
    )


def project_unresolved_failures(
    executions: Iterable[ToolExecutionRecord],
    specs_by_name: Mapping[str, ToolSpec],
    *,
    known_mutation_ids: frozenset[str] | None = None,
) -> tuple[ExecutionFact, ...]:
    """Effect failures that no later action resolved, in stable order."""
    facts = project_execution_facts(
        executions, specs_by_name,
        known_mutation_ids=known_mutation_ids,
        include_successful=False,
    )
    return tuple(
        fact for fact in facts if fact.resolution is FactResolution.UNRESOLVED
    )


def _with_resolution(
    fact: ExecutionFact, resolution: FactResolution,
) -> ExecutionFact:
    return ExecutionFact(
        execution_id=fact.execution_id,
        tool_name=fact.tool_name,
        effect=fact.effect,
        result_authority=fact.result_authority,
        invocation_status=fact.invocation_status,
        effect_status=fact.effect_status,
        resolution=resolution,
        failure_code=fact.failure_code,
        detail=fact.detail,
    )


__all__ = [
    "EffectStatus",
    "ExecutionFact",
    "FactResolution",
    "classify_effect_status",
    "project_execution_fact",
    "project_execution_facts",
    "project_unresolved_failures",
]
