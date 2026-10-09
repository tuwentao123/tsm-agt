"""Diagnostic-only completion projection for the SINGLE_LOOP migration.

This module intentionally does not participate in ``validate_completion`` or
turn control flow. It compares durable intent attempts and execution facts with
the existing completion claim so rollout can measure differences before the new
contract becomes authoritative.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tsm_agt.core.execution_facts import (
    EffectStatus,
    ToolExecutionFact,
    project_execution_fact,
    project_tool_execution_fact,
)
from tsm_agt.ports import RuntimeEvent, ToolEffect

from ..task import TaskSnapshot
from .contracts import CompletionValidation, _net_mutations
from .types import (
    IntentAttempt,
    IntentAttemptStatus,
    OrchestrationMode,
    TaskOrchestrationMetadata,
    ToolIntentRequest,
    TurnIntentKind,
)

_MAX_ATTEMPTS = 20
_MAX_ITEMS = 50
_MAX_DIAGNOSTICS = 20


@dataclass(frozen=True, slots=True)
class CompletionShadowGap:
    """A diagnostic-only obligation that the existing validator did not gate."""

    code: str
    effect: ToolEffect | None = None
    intent_attempt_ref: str = ""
    detail: str = ""

    def to_data(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "effect": self.effect.value if self.effect is not None else None,
            "intent_attempt_ref": self.intent_attempt_ref,
            "detail": self.detail[:500],
            "diagnostic_only": True,
        }


@dataclass(frozen=True, slots=True)
class CompletionShadowDivergence:
    """A durable fact that differs from the legacy completion projection."""

    code: str
    execution_id: str = ""
    mutation_id: str = ""
    detail: str = ""

    def to_data(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "execution_id": self.execution_id,
            "mutation_id": self.mutation_id,
            "detail": self.detail[:500],
            "diagnostic_only": True,
        }


@dataclass(frozen=True, slots=True)
class CompletionShadowValidation:
    """Bounded rollout telemetry; never changes the legacy completion result."""

    turn_id: str
    orchestration_mode: OrchestrationMode
    claim: Mapping[str, Any]
    gaps: tuple[CompletionShadowGap, ...] = ()
    divergences: tuple[CompletionShadowDivergence, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "turn_id": self.turn_id,
            "orchestration_mode": self.orchestration_mode.value,
            "claim": dict(self.claim),
            "gaps": [gap.to_data() for gap in self.gaps],
            "divergences": [item.to_data() for item in self.divergences],
            "diagnostic_only": True,
        }


def validate_completion_shadow(
    *,
    task: TaskSnapshot,
    metadata: TaskOrchestrationMetadata,
    events: Sequence[RuntimeEvent],
    turn_id: str,
    completion: CompletionValidation,
) -> CompletionShadowValidation:
    """Compare intent attempts and execution facts without changing completion.

    The root goal and binding are provenance only: this migration stage does not
    infer obligations from arbitrary prose. Terminal ``IntentAttempt`` records
    contribute their Runtime-derived implied effects; a matching durable tool
    fact satisfies an effect, while mutation also requires a surviving net delta.
    """
    attempts, unresolved_attempts = _intent_attempts(events, turn_id)
    executions = tuple(
        record for record in task.tool_executions.values() if record.turn_id == turn_id
    )
    mutation_ids = frozenset(
        mutation.mutation_id for mutation in task.mutation_journal
    )
    execution_facts = tuple(
        fact
        for record in executions
        if (fact := project_tool_execution_fact(
            record,
            known_mutation_ids=mutation_ids,
        )) is not None
    )
    delivery_facts = tuple(
        fact
        for record in executions
        if (fact := project_execution_fact(
            record,
            known_mutation_ids=mutation_ids,
        )) is not None
    )
    net_mutation_ids = _net_mutations(task)
    declared_effects = tuple(sorted({
        effect
        for attempt in attempts
        for effect in attempt.implied_effects
    }, key=lambda effect: effect.value))
    succeeded_effects = tuple(sorted({
        fact.effect
        for fact in execution_facts
        if fact.effect_status is EffectStatus.SUCCEEDED
    }, key=lambda effect: effect.value))

    gaps: list[CompletionShadowGap] = []
    for attempt in unresolved_attempts:
        for effect in _implied_effects_from_attempted_payload(attempt):
            gaps.append(CompletionShadowGap(
                code="SHADOW_INTENT_ATTEMPT_UNRESOLVED",
                effect=effect,
                intent_attempt_ref=str(attempt.get("attempt_id", "")),
                detail="Intent attempt has no terminal accepted, normalized, or rejected receipt.",
            ))
    for attempt in attempts:
        for effect in attempt.implied_effects:
            if not _effect_is_satisfied(effect, execution_facts, net_mutation_ids):
                gaps.append(CompletionShadowGap(
                    code="SHADOW_INTENT_EFFECT_UNSATISFIED",
                    effect=effect,
                    intent_attempt_ref=attempt.attempt_id,
                    detail=(
                        "Runtime-derived intent effect lacks matching durable "
                        "execution evidence."
                    ),
                ))

    divergences: list[CompletionShadowDivergence] = []
    for fact in delivery_facts:
        if (
            fact.effect is ToolEffect.MUTATE
            and fact.effect_status is EffectStatus.UNKNOWN
            and fact.failure_code == "MUTATION_NOT_RECORDED"
        ):
            divergences.append(CompletionShadowDivergence(
                code="MUTATION_EFFECT_NOT_RECORDED",
                execution_id=fact.execution_id,
                detail="Mutation tool result did not reference a committed journal mutation.",
            ))
        if (
            fact.effect is ToolEffect.MUTATE
            and fact.effect_status is EffectStatus.SUCCEEDED
            and not net_mutation_ids
        ):
            divergences.append(CompletionShadowDivergence(
                code="DELIVERED_MUTATION_ROLLED_BACK",
                execution_id=fact.execution_id,
                detail="A mutation execution was delivered, but no net workspace delta remains.",
            ))
    for attempt in attempts:
        if (
            attempt.continuation_task_id is not None
            and attempt.continuation_task_id != task.task_id
        ):
            divergences.append(CompletionShadowDivergence(
                code="CONTINUATION_TASK_MISMATCH",
                detail=(
                    "Intent continuation reference does not match the current Task."
                ),
            ))
    legacy_claimed = set(completion.claim.claimed_effects)
    for effect in legacy_claimed - set(succeeded_effects):
        divergences.append(CompletionShadowDivergence(
            code="LEGACY_CLAIM_EFFECT_UNOBSERVED",
            detail=f"Legacy completion claimed {effect.value} without shadow success.",
        ))

    claim = {
        "root_input_event_ref": metadata.root_input_event_ref,
        "latest_input_event_ref": metadata.latest_input_event_ref,
        "binding_kind": metadata.binding_kind.value,
        "legacy_completion_claim": completion.claim.to_data(),
        "intent_attempt_refs": [
            attempt.attempt_id for attempt in attempts[:_MAX_ATTEMPTS]
        ],
        "declared_effects": [effect.value for effect in declared_effects],
        "succeeded_effects": [effect.value for effect in succeeded_effects],
        "net_mutation_ids": list(net_mutation_ids[:_MAX_ITEMS]),
        "execution_fact_refs": [
            fact.execution_id for fact in execution_facts[:_MAX_ITEMS]
        ],
    }
    return CompletionShadowValidation(
        turn_id=turn_id,
        orchestration_mode=metadata.orchestration_mode,
        claim=claim,
        gaps=tuple(gaps[:_MAX_DIAGNOSTICS]),
        divergences=tuple(divergences[:_MAX_DIAGNOSTICS]),
    )


def _intent_attempts(
    events: Sequence[RuntimeEvent],
    turn_id: str,
) -> tuple[tuple[IntentAttempt, ...], tuple[Mapping[str, Any], ...]]:
    terminal_types = {
        "tool.intent_accepted",
        "tool.intent_normalized",
        "tool.intent_rejected",
    }
    terminals: list[IntentAttempt] = []
    terminal_call_ids: set[str] = set()
    attempted: list[Mapping[str, Any]] = []
    for event in reversed(events):
        if event.payload.get("turn_id") != turn_id:
            continue
        if event.event_type in terminal_types:
            try:
                attempt = IntentAttempt.from_data(event.payload)
            except (KeyError, TypeError, ValueError):
                continue
            if attempt.tool_call_id in terminal_call_ids:
                continue
            terminal_call_ids.add(attempt.tool_call_id)
            terminals.append(attempt)
        elif event.event_type == "tool.intent_attempted":
            attempted.append(event.payload)
    terminals.reverse()
    unresolved = tuple(
        payload
        for payload in reversed(attempted)
        if str(payload.get("tool_call_id", "")) not in terminal_call_ids
    )
    return tuple(terminals[:_MAX_ATTEMPTS]), unresolved[:_MAX_ATTEMPTS]


def _implied_effects_from_attempted_payload(
    payload: Mapping[str, Any],
) -> tuple[ToolEffect, ...]:
    raw_intent = payload.get("requested_intent")
    try:
        intent = TurnIntentKind(raw_intent) if isinstance(raw_intent, str) else None
    except ValueError:
        return ()
    return ToolIntentRequest.implied_effects_for(intent)


def _effect_is_satisfied(
    effect: ToolEffect,
    execution_facts: Sequence[ToolExecutionFact],
    net_mutation_ids: Sequence[str],
) -> bool:
    if effect is ToolEffect.MUTATE:
        return bool(net_mutation_ids) and any(
            fact.effect is ToolEffect.MUTATE
            and fact.effect_status is EffectStatus.SUCCEEDED
            for fact in execution_facts
        )
    return any(
        fact.effect is effect and fact.effect_status is EffectStatus.SUCCEEDED
        for fact in execution_facts
    )
