"""Durable completion contracts for the ``SINGLE_LOOP`` execution path.

This module derives completion facts from the task ledger rather than from the
model's final prose.  It deliberately has no dependency on ``Kernel`` so its
rules remain independently testable and cannot alter legacy completion.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from tsm_agt.ports import Message, RuntimeEvent, ToolEffect, ToolResultAuthority, ToolSpec
from tsm_agt.ports.evidence import (
    EvidenceFact,
    EvidenceGatePort,
    GateDecision,
    GateResolution,
    TaskContract,
)

from ..execution import ToolCommitState, ToolExecutionRecord
from ..task_contract import TaskContractProjector
from ..task import TaskSnapshot
from .types import (
    CompletionClaim,
    IntentDependency,
    IntentDependencyKind,
    TaskOrchestrationMetadata,
    TurnIntent,
    TurnIntentKind,
)

_RUNTIME_INSPECTION_TOOLS = frozenset({
    "runtime.inspect_task",
    "runtime.list_task_events",
    "runtime.inspect_session",
    "runtime.search_history",
})
_ANCHOR_PATTERN = re.compile(
    r"\b(?P<kind>task|session|trace)[ _-]?id\s*[:=]\s*(?P<value>[A-Za-z0-9._:-]+)",
    re.IGNORECASE,
)

@dataclass(frozen=True, slots=True)
class CompletionGap:
    """One durable fact missing before a single-loop turn can complete."""

    code: str
    detail: str
    recoverable: bool = True
    required_effects: tuple[ToolEffect, ...] = ()
    required_capability_ids: tuple[str, ...] = ()
    reference: str = ""

    def to_data(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "recoverable": self.recoverable,
            "required_effects": [effect.value for effect in self.required_effects],
            "required_capability_ids": list(self.required_capability_ids),
            "reference": self.reference,
        }


@dataclass(frozen=True, slots=True)
class CompletionValidation:
    """A runtime-owned claim paired with any durable completion gaps."""

    claim: CompletionClaim
    gaps: tuple[CompletionGap, ...]
    gate_decision: GateDecision = GateDecision(
        GateResolution.PASS, reason_code="NO_MANDATORY_CONTRACT"
    )

    @property
    def complete(self) -> bool:
        return self.gate_decision.resolution is GateResolution.PASS and not self.gaps

    def to_data(self) -> dict[str, Any]:
        return {
            "claim": self.claim.to_data(),
            "gaps": [gap.to_data() for gap in self.gaps],
            "gate_decision": self.gate_decision.to_data(),
        }


def validate_completion(
    *,
    task: TaskSnapshot,
    metadata: TaskOrchestrationMetadata,
    events: Sequence[RuntimeEvent],
    turn_id: str,
    assistant_message: Message,
    visible_tools: Sequence[ToolSpec] = (),
    evidence_gate: EvidenceGatePort | None = None,
) -> CompletionValidation:
    """Derive a completion claim and reject final prose lacking ledger evidence."""
    intent = _latest_intent(events, turn_id)
    executions = tuple(
        record for record in task.tool_executions.values() if record.turn_id == turn_id
    )
    successful = tuple(record for record in executions if _successful(record))
    net_mutations = _net_mutations(task)
    evidence_refs = tuple(
        [f"tool_execution:{record.execution_id}" for record in successful]
        + [f"mutation:{mutation_id}" for mutation_id in net_mutations]
    )
    claimed_effects: list[ToolEffect] = []
    if net_mutations:
        claimed_effects.append(ToolEffect.MUTATE)
    if any(record.effect is ToolEffect.EXECUTE for record in successful):
        claimed_effects.append(ToolEffect.EXECUTE)
    if any(record.effect is ToolEffect.OBSERVE for record in successful):
        claimed_effects.append(ToolEffect.OBSERVE)
    claim = CompletionClaim(
        schema_version=1,
        turn_id=turn_id,
        assistant_message_id=assistant_message.message_id,
        evidence_refs=evidence_refs,
        claimed_effects=tuple(claimed_effects),
    )
    anchors = _explicit_anchors(metadata.root_goal)
    contract = _contract_from_events(events)
    facts = _evidence_facts(task)
    if evidence_gate is None:
        raise ValueError("validate_completion requires an EvidenceGatePort")
    decision = evidence_gate.evaluate(
        contract,
        facts,
        frozenset(
            capability
            for tool in visible_tools
            for capability in tool.evidence_capability_ids
        ),
    )
    if (
        (intent is None or intent.kind is TurnIntentKind.DIRECT_RESPONSE)
        and not anchors
        and decision.resolution is GateResolution.PASS
    ):
        return CompletionValidation(claim, (), decision)

    gaps: list[CompletionGap] = []
    if decision.resolution is not GateResolution.PASS:
        gaps.append(CompletionGap(
            code=decision.reason_code,
            detail=decision.detail,
            recoverable=decision.resolution is GateResolution.CONTINUE,
            required_capability_ids=decision.required_capability_ids,
            reference=",".join(decision.missing_requirement_ids),
        ))
    observed = tuple(record for record in successful if record.effect is ToolEffect.OBSERVE)
    for dependency in (intent.dependencies if intent is not None else ()):
        if dependency.kind is IntentDependencyKind.WORKSPACE_SOURCE and not observed:
            gaps.append(CompletionGap(
                "SOURCE_OBSERVATION_MISSING",
                "A source dependency requires a successful observe execution.",
                required_effects=(ToolEffect.OBSERVE,),
                reference=dependency.reference,
            ))
        if dependency.kind in {
            IntentDependencyKind.RUNTIME_TASK,
            IntentDependencyKind.RUNTIME_SESSION,
        } and not _has_matching_runtime_evidence(observed, dependency):
            gaps.append(CompletionGap(
                "RUNTIME_INSPECTION_MISSING",
                "A runtime dependency requires matching runtime inspection evidence.",
                required_effects=(ToolEffect.OBSERVE,),
                reference=dependency.reference,
            ))
    for kind, value in anchors:
        dependency = IntentDependency(
            {
                "task": IntentDependencyKind.RUNTIME_TASK,
                "session": IntentDependencyKind.RUNTIME_SESSION,
                "trace": IntentDependencyKind.RUNTIME_TASK,
            }[kind],
            value,
        )
        if not _has_matching_runtime_evidence(observed, dependency):
            gaps.append(CompletionGap(
                "RUNTIME_ANCHOR_UNVERIFIED",
                f"Explicit {kind}-id anchor requires matching runtime inspection evidence.",
                required_effects=(ToolEffect.OBSERVE,),
                reference=value,
            ))
    if intent is not None and intent.kind is TurnIntentKind.MODIFY and not net_mutations:
        gaps.append(CompletionGap(
            "NET_MUTATION_MISSING",
            "MODIFY intent requires a net workspace mutation.",
            required_effects=(ToolEffect.MUTATE,),
        ))
    if intent is not None and intent.kind is TurnIntentKind.VERIFY and not any(
        record.effect is ToolEffect.EXECUTE for record in successful
    ):
        gaps.append(CompletionGap(
            "SUCCESSFUL_EXECUTION_MISSING",
            "VERIFY intent requires a committed successful execute result.",
            required_effects=(ToolEffect.EXECUTE,),
        ))
    return CompletionValidation(claim, tuple(gaps), decision)


def required_effects(gaps: Iterable[CompletionGap]) -> frozenset[ToolEffect]:
    """Return effects a continuation must still be able to produce."""
    return frozenset(effect for gap in gaps for effect in gap.required_effects)


def required_capability_ids(gaps: Iterable[CompletionGap]) -> frozenset[str]:
    """Return exact typed-evidence capabilities a continuation may satisfy."""
    return frozenset(
        capability for gap in gaps for capability in gap.required_capability_ids
    )


def _contract_from_events(events: Sequence[RuntimeEvent]) -> TaskContract | None:
    return TaskContractProjector.project(events)


def _evidence_facts(task: TaskSnapshot) -> tuple[EvidenceFact, ...]:
    facts: list[EvidenceFact] = []
    for record in task.tool_executions.values():
        if not _successful(record) or record.result is None:
            continue
        execution_ref = f"tool_execution:{record.execution_id}"
        facts.extend(
            replace(fact, execution_ref=execution_ref, complete=False)
            if record.result.truncated and fact.complete
            else replace(fact, execution_ref=execution_ref)
            for fact in record.result.evidence_facts
        )
    return tuple(facts)


def _latest_intent(events: Sequence[RuntimeEvent], turn_id: str) -> TurnIntent | None:
    for event in reversed(events):
        if event.event_type not in {"turn.intent_declared", "turn.intent_updated"}:
            continue
        intent = TurnIntent.from_data(event.payload)
        if intent.turn_id == turn_id:
            return intent
    return None


def _successful(record: ToolExecutionRecord) -> bool:
    return (
        record.state is ToolCommitState.COMMITTED
        and record.result is not None
        and record.result.ok
    )


def _net_mutations(task: TaskSnapshot) -> tuple[str, ...]:
    by_path: dict[str, tuple[Any, Any]] = {}
    for mutation in sorted(task.mutation_journal, key=lambda item: item.created_at):
        first, _ = by_path.get(mutation.path, (mutation, mutation))
        by_path[mutation.path] = (first, mutation)
    return tuple(
        last.mutation_id for first, last in by_path.values()
        if first.before_hash != last.after_hash
    )


def _explicit_anchors(root_goal: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (match.group("kind").casefold(), match.group("value"))
        for match in _ANCHOR_PATTERN.finditer(root_goal)
    )


def _has_matching_runtime_evidence(
    executions: Sequence[ToolExecutionRecord], dependency: IntentDependency,
) -> bool:
    return any(
        record.call.name in _RUNTIME_INSPECTION_TOOLS
        and record.result_authority is ToolResultAuthority.RUNTIME_FACT
        and _contains_reference(record.call.arguments, dependency.reference)
        or (
            record.call.name in _RUNTIME_INSPECTION_TOOLS
            and record.result_authority is ToolResultAuthority.RUNTIME_FACT
            and record.result is not None
            and _contains_reference(record.result.data, dependency.reference)
        )
        for record in executions
    )


def _contains_reference(value: Any, reference: str) -> bool:
    if isinstance(value, str):
        return value == reference
    if isinstance(value, Mapping):
        return any(_contains_reference(item, reference) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_reference(item, reference) for item in value)
    return False
