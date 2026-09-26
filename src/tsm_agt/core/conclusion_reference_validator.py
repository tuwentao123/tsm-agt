"""Read-only validation of structured conclusion fact references."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tsm_agt.ports import (
    AssistantConclusion,
    ClaimReferenceValidation,
    ConclusionKind,
    ConclusionReferenceValidation,
    ConclusionValidationStatus,
    FactReference,
    RuntimeEvent,
)

from .execution import ToolCommitState, ToolExecutionRecord
from .task import TaskSnapshot


_STRONG_KINDS = frozenset({
    ConclusionKind.IMPLEMENTED,
    ConclusionKind.VERIFIED,
})
_UNAVAILABLE_LIFECYCLES = frozenset({"orphaned", "expired", "purged"})


@dataclass(frozen=True, slots=True)
class _ReferenceCheck:
    status: ConclusionValidationStatus
    reasons: tuple[str, ...] = ()


class ConclusionReferenceValidator:
    """Validate reference identity and recorded fact metadata without side effects.

    The validator deliberately does not interpret claim summaries, decide whether
    a change repaired a product behaviour, or modify Runtime state.  It only
    compares a structured reference against the current Task ledger and the
    immutable Runtime event stream supplied by its caller.
    """

    def validate(
        self,
        task: TaskSnapshot,
        events: Sequence[RuntimeEvent],
        conclusion: AssistantConclusion,
    ) -> ConclusionReferenceValidation:
        events_by_id = {event.event_id: event for event in events}
        claims: list[ClaimReferenceValidation] = []
        for claim in conclusion.claims:
            checks = tuple(
                self._validate_reference(task, events_by_id, claim.kind, reference)
                for reference in claim.fact_refs
            )
            reasons = tuple(reason for check in checks for reason in check.reasons)
            status = self._aggregate(check.status for check in checks)
            claims.append(ClaimReferenceValidation(claim.claim_id, status, reasons))
        return ConclusionReferenceValidation(
            self._aggregate(claim.status for claim in claims), tuple(claims)
        )

    @staticmethod
    def _aggregate(
        statuses: Sequence[ConclusionValidationStatus] | Any,
    ) -> ConclusionValidationStatus:
        values = tuple(statuses)
        if any(status is ConclusionValidationStatus.INVALID for status in values):
            return ConclusionValidationStatus.INVALID
        if any(status is ConclusionValidationStatus.INCOMPLETE for status in values):
            return ConclusionValidationStatus.INCOMPLETE
        return ConclusionValidationStatus.VALID

    def _validate_reference(
        self,
        task: TaskSnapshot,
        events_by_id: Mapping[str, RuntimeEvent],
        kind: ConclusionKind,
        reference: FactReference,
    ) -> _ReferenceCheck:
        if reference.task_id != task.task_id:
            return self._invalid("task_id_mismatch")
        event = events_by_id.get(reference.event_id)
        if event is None:
            return self._invalid("event_not_found")
        if event.sequence != reference.sequence:
            return self._invalid("event_sequence_mismatch")
        if kind in _STRONG_KINDS and reference.lifecycle.value in _UNAVAILABLE_LIFECYCLES:
            return _ReferenceCheck(
                ConclusionValidationStatus.INCOMPLETE,
                (f"reference_lifecycle_{reference.lifecycle.value}",),
            )
        if reference.source_type == "tool_result":
            return self._tool_result(task, event, kind, reference)
        if reference.source_type == "runtime_event":
            if reference.source_id != event.event_id:
                return self._invalid("runtime_event_source_id_mismatch")
            return self._without_tool_fact(reference)
        if reference.source_type == "mutation":
            return self._mutation(task, event, reference)
        if reference.source_type == "verification":
            return self._verification(event, reference)
        if reference.source_type == "artifact":
            return self._artifact(event, reference)
        return self._invalid("unsupported_source_type")

    @staticmethod
    def _invalid(reason: str) -> _ReferenceCheck:
        return _ReferenceCheck(ConclusionValidationStatus.INVALID, (reason,))

    def _tool_result(
        self,
        task: TaskSnapshot,
        event: RuntimeEvent,
        kind: ConclusionKind,
        reference: FactReference,
    ) -> _ReferenceCheck:
        if event.event_type not in {"tool.completed", "tool.failed"}:
            return self._invalid("tool_result_event_type_mismatch")
        execution_id = event.payload.get("execution_id")
        if not isinstance(execution_id, str) or execution_id != reference.source_id:
            return self._invalid("tool_result_execution_id_mismatch")
        if reference.execution_id is not None and reference.execution_id != execution_id:
            return self._invalid("execution_id_mismatch")
        execution = task.tool_executions.get(execution_id)
        if execution is None:
            return _ReferenceCheck(
                ConclusionValidationStatus.INCOMPLETE, ("execution_not_found",)
            )
        if reference.tool_call_id is not None and reference.tool_call_id != execution.call.call_id:
            return self._invalid("tool_call_id_mismatch")
        result = execution.result
        if result is None:
            return _ReferenceCheck(
                ConclusionValidationStatus.INCOMPLETE, ("tool_result_not_committed",)
            )
        event_result = event.payload.get("result")
        if not isinstance(event_result, Mapping) or event_result.get("call_id") != result.call_id:
            return self._invalid("tool_result_event_payload_mismatch")
        if kind in _STRONG_KINDS:
            if execution.state is not ToolCommitState.COMMITTED:
                return self._invalid("strong_claim_requires_committed_execution")
            if not result.ok:
                return self._invalid("strong_claim_requires_successful_result")
        if kind is ConclusionKind.VERIFIED and (
            event.event_type != "tool.completed" or not result.ok
        ):
            return self._invalid("verified_requires_successful_tool_result")
        descriptor = result.fact_descriptor
        if descriptor is None:
            return self._fact_metadata_missing(reference)
        if reference.expected_authority is not None and (
            reference.expected_authority != descriptor.authority
        ):
            return self._invalid("fact_authority_mismatch")
        expected_effect = reference.expected_effect
        recorded_tool = event.payload.get("tool")
        recorded_effect = (
            recorded_tool.get("effect")
            if isinstance(recorded_tool, Mapping) else None
        )
        if expected_effect is not None and expected_effect != recorded_effect:
            return self._invalid("tool_effect_mismatch")
        if reference.content_hash is not None and reference.content_hash != descriptor.content_hash:
            return self._invalid("fact_content_hash_mismatch")
        if kind in _STRONG_KINDS and event.event_type == "tool.failed":
            return self._invalid("failed_tool_result_cannot_support_strong_claim")
        return _ReferenceCheck(ConclusionValidationStatus.VALID)

    def _without_tool_fact(self, reference: FactReference) -> _ReferenceCheck:
        if (
            reference.expected_authority is not None
            or reference.expected_effect is not None
            or reference.content_hash is not None
        ):
            return self._invalid("tool_fact_metadata_not_available_for_source")
        return _ReferenceCheck(ConclusionValidationStatus.VALID)

    def _fact_metadata_missing(self, reference: FactReference) -> _ReferenceCheck:
        if (
            reference.expected_authority is not None
            or reference.expected_effect is not None
            or reference.content_hash is not None
        ):
            return _ReferenceCheck(
                ConclusionValidationStatus.INCOMPLETE, ("fact_descriptor_missing",)
            )
        return _ReferenceCheck(ConclusionValidationStatus.VALID)

    def _mutation(
        self, task: TaskSnapshot, event: RuntimeEvent, reference: FactReference,
    ) -> _ReferenceCheck:
        if not any(item.mutation_id == reference.source_id for item in task.mutation_journal):
            return self._invalid("mutation_not_found")
        if not self._payload_contains(event.payload, "mutation_id", reference.source_id):
            return self._invalid("mutation_event_mismatch")
        return self._without_tool_fact(reference)

    def _verification(
        self, event: RuntimeEvent, reference: FactReference,
    ) -> _ReferenceCheck:
        if not (event.event_type.startswith("verify.") or "verification" in event.event_type):
            return self._invalid("verification_event_type_mismatch")
        if not self._source_in_event(event, reference.source_id, (
            "criterion_id", "verification_id", "check_id",
        )):
            return self._invalid("verification_source_id_mismatch")
        return self._without_tool_fact(reference)

    def _artifact(
        self, event: RuntimeEvent, reference: FactReference,
    ) -> _ReferenceCheck:
        if not self._source_in_event(event, reference.source_id, (
            "artifact_id", "artifact_ref", "artifact_refs",
        )):
            return self._invalid("artifact_source_id_mismatch")
        return self._without_tool_fact(reference)

    @staticmethod
    def _payload_contains(
        payload: Mapping[str, Any], name: str, expected: str,
    ) -> bool:
        value = payload.get(name)
        return value == expected or (
            isinstance(value, list) and expected in value
        )

    def _source_in_event(
        self, event: RuntimeEvent, source_id: str, keys: tuple[str, ...],
    ) -> bool:
        return any(self._payload_contains(event.payload, key, source_id) for key in keys)
