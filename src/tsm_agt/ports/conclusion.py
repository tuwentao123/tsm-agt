"""Structured, evidence-referenced assistant conclusions.

These contracts describe model claims and their reference-validation metadata.  They
must not be used to infer task success from prose or to make Runtime state changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class ConclusionKind(StrEnum):
    IMPLEMENTED = "implemented"
    CHECKED = "checked"
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_LIMITED = "verification_limited"
    HUMAN_CONFIRMATION_REQUIRED = "human_confirmation_required"


class FactLifecycle(StrEnum):
    ACTIVE = "active"
    REPLAYABLE = "replayable"
    ORPHANED = "orphaned"
    EXPIRED = "expired"
    PURGED = "purged"


class ConclusionValidationStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"


def _required_string(data: Mapping[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return tuple(value)


def _optional_string(data: Mapping[str, Any], name: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string when present")
    return value


@dataclass(frozen=True, slots=True)
class FactReference:
    task_id: str
    event_id: str
    sequence: int
    source_type: str
    source_id: str
    tool_call_id: str | None = None
    execution_id: str | None = None
    expected_authority: str | None = None
    expected_effect: str | None = None
    content_hash: str | None = None
    lifecycle: FactLifecycle = FactLifecycle.ACTIVE

    def __post_init__(self) -> None:
        for name in ("task_id", "event_id", "source_type", "source_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"fact reference {name} must be a non-empty string")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 0:
            raise ValueError("fact reference sequence must be a non-negative integer")
        for name in (
            "tool_call_id", "execution_id", "expected_authority",
            "expected_effect", "content_hash",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"fact reference {name} must be a non-empty string when present")

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "task_id": self.task_id,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "lifecycle": self.lifecycle.value,
        }
        for name in (
            "tool_call_id", "execution_id", "expected_authority",
            "expected_effect", "content_hash",
        ):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        return data

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> FactReference:
        sequence = data.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ValueError("sequence must be an integer")
        return cls(
            task_id=_required_string(data, "task_id"),
            event_id=_required_string(data, "event_id"),
            sequence=sequence,
            source_type=_required_string(data, "source_type"),
            source_id=_required_string(data, "source_id"),
            tool_call_id=_optional_string(data, "tool_call_id"),
            execution_id=_optional_string(data, "execution_id"),
            expected_authority=_optional_string(data, "expected_authority"),
            expected_effect=_optional_string(data, "expected_effect"),
            content_hash=_optional_string(data, "content_hash"),
            lifecycle=FactLifecycle(str(data.get("lifecycle", FactLifecycle.ACTIVE.value))),
        )


@dataclass(frozen=True, slots=True)
class ConclusionClaim:
    claim_id: str
    kind: ConclusionKind
    summary: str
    scope: tuple[str, ...] = ()
    fact_refs: tuple[FactReference, ...] = ()
    unverified_scope: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.claim_id, str) or not self.claim_id.strip():
            raise ValueError("claim_id must be a non-empty string")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("claim summary must be a non-empty string")
        if any(not isinstance(item, str) or not item.strip() for item in self.scope):
            raise ValueError("claim scope must contain non-empty strings")
        if any(not isinstance(item, FactReference) for item in self.fact_refs):
            raise ValueError("claim fact_refs must contain FactReference values")
        if any(not isinstance(item, str) or not item.strip() for item in self.unverified_scope):
            raise ValueError("claim unverified_scope must contain non-empty strings")
        if not isinstance(self.reason, str):
            raise ValueError("claim reason must be a string")
        if self.kind is ConclusionKind.IMPLEMENTED and not any(
            reference.source_type in {"mutation", "tool_result"}
            for reference in self.fact_refs
        ):
            raise ValueError("implemented claim requires mutation or tool_result fact")
        if self.kind is ConclusionKind.CHECKED and not any(
            reference.source_type in {"tool_result", "verification"}
            for reference in self.fact_refs
        ):
            raise ValueError("checked claim requires tool_result or verification fact")
        if self.kind is ConclusionKind.VERIFIED and (
            not self.scope or not self.fact_refs
        ):
            raise ValueError("verified claim requires scope and fact_refs")
        if self.kind is ConclusionKind.VERIFICATION_FAILED and not self.fact_refs:
            raise ValueError("verification_failed claim requires fact_refs")
        if self.kind is ConclusionKind.VERIFICATION_LIMITED and (
            not self.unverified_scope or not self.reason.strip()
        ):
            raise ValueError(
                "verification_limited claim requires unverified_scope and reason"
            )
        if self.kind is ConclusionKind.HUMAN_CONFIRMATION_REQUIRED and not self.reason.strip():
            raise ValueError("human_confirmation_required claim requires reason")

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "claim_id": self.claim_id,
            "kind": self.kind.value,
            "summary": self.summary,
            "scope": list(self.scope),
            "fact_refs": [reference.to_data() for reference in self.fact_refs],
            "unverified_scope": list(self.unverified_scope),
            "reason": self.reason,
        }
        return data

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ConclusionClaim:
        raw_refs = data.get("fact_refs", [])
        if not isinstance(raw_refs, list) or any(
            not isinstance(reference, Mapping) for reference in raw_refs
        ):
            raise ValueError("fact_refs must be a list of objects")
        reason = data.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError("reason must be a string")
        return cls(
            claim_id=_required_string(data, "claim_id"),
            kind=ConclusionKind(_required_string(data, "kind")),
            summary=_required_string(data, "summary"),
            scope=_strings(data.get("scope", []), "scope"),
            fact_refs=tuple(FactReference.from_data(reference) for reference in raw_refs),
            unverified_scope=_strings(
                data.get("unverified_scope", []), "unverified_scope"
            ),
            reason=reason,
        )


@dataclass(frozen=True, slots=True)
class AssistantConclusion:
    schema_version: int
    claims: tuple[ConclusionClaim, ...]
    overall_scope: tuple[str, ...] = ()
    origin: str = "native_structured"

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported conclusion schema_version")
        if not self.claims or any(not isinstance(claim, ConclusionClaim) for claim in self.claims):
            raise ValueError("conclusion claims must be a non-empty sequence")
        if len({claim.claim_id for claim in self.claims}) != len(self.claims):
            raise ValueError("conclusion claim_id values must be unique")
        if any(not isinstance(item, str) or not item.strip() for item in self.overall_scope):
            raise ValueError("conclusion overall_scope must contain non-empty strings")
        if not isinstance(self.origin, str) or not self.origin.strip():
            raise ValueError("conclusion origin must be a non-empty string")

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "claims": [claim.to_data() for claim in self.claims],
            "overall_scope": list(self.overall_scope),
            "origin": self.origin,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AssistantConclusion:
        schema_version = data.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError("schema_version must be an integer")
        raw_claims = data.get("claims")
        if not isinstance(raw_claims, list) or any(
            not isinstance(claim, Mapping) for claim in raw_claims
        ):
            raise ValueError("claims must be a list of objects")
        origin = data.get("origin", "native_structured")
        if not isinstance(origin, str):
            raise ValueError("origin must be a string")
        return cls(
            schema_version=schema_version,
            claims=tuple(ConclusionClaim.from_data(claim) for claim in raw_claims),
            overall_scope=_strings(data.get("overall_scope", []), "overall_scope"),
            origin=origin,
        )


@dataclass(frozen=True, slots=True)
class ClaimReferenceValidation:
    claim_id: str
    status: ConclusionValidationStatus
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.claim_id, str) or not self.claim_id.strip():
            raise ValueError("claim_id must be a non-empty string")
        if any(not isinstance(reason, str) or not reason.strip() for reason in self.reasons):
            raise ValueError("validation reasons must contain non-empty strings")

    def to_data(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "status": self.status.value,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ClaimReferenceValidation:
        return cls(
            claim_id=_required_string(data, "claim_id"),
            status=ConclusionValidationStatus(_required_string(data, "status")),
            reasons=_strings(data.get("reasons", []), "reasons"),
        )


@dataclass(frozen=True, slots=True)
class ConclusionReferenceValidation:
    status: ConclusionValidationStatus
    claims: tuple[ClaimReferenceValidation, ...]

    def __post_init__(self) -> None:
        if any(not isinstance(claim, ClaimReferenceValidation) for claim in self.claims):
            raise ValueError("validation claims must contain ClaimReferenceValidation values")
        if len({claim.claim_id for claim in self.claims}) != len(self.claims):
            raise ValueError("validation claim_id values must be unique")

    def to_data(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "claims": [claim.to_data() for claim in self.claims],
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ConclusionReferenceValidation:
        raw_claims = data.get("claims", [])
        if not isinstance(raw_claims, list) or any(
            not isinstance(claim, Mapping) for claim in raw_claims
        ):
            raise ValueError("validation claims must be a list of objects")
        return cls(
            status=ConclusionValidationStatus(_required_string(data, "status")),
            claims=tuple(ClaimReferenceValidation.from_data(claim) for claim in raw_claims),
        )
