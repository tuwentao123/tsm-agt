"""Provider-neutral evidence contracts persisted with Task events and results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class ContractAuthority(StrEnum):
    """Authority that determines whether a contract can block completion."""

    USER_EXPLICIT = "USER_EXPLICIT"
    MODEL_DRAFT = "MODEL_DRAFT"
    # Compatibility aliases for persisted pre-lifecycle contracts.
    USER = USER_EXPLICIT
    DRAFT = MODEL_DRAFT


class ContractPhase(StrEnum):
    """Lifecycle phase of a task-scoped, event-projected contract."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    CLEARED = "CLEARED"


class GateResolution(StrEnum):
    """A deterministic evidence-gate outcome consumed by orchestration."""

    PASS = "PASS"
    CONTINUE = "CONTINUE"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    """One user-visible action that can unblock a deterministic contract gate."""

    action_id: str
    label: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.action_id.strip() or not self.label.strip():
            raise ValueError("recovery action id and label must not be empty")
        if not isinstance(self.payload, Mapping):
            raise ValueError("recovery action payload must be an object")

    def to_data(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "label": self.label,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class EvidenceCapability:
    """One provider-declared evidence kind a visible tool can produce."""

    capability_id: str

    def __post_init__(self) -> None:
        if not self.capability_id.strip():
            raise ValueError("evidence capability_id must not be empty")

    def to_data(self) -> dict[str, str]:
        return {"capability_id": self.capability_id}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> EvidenceCapability:
        return cls(str(data.get("capability_id", "")))


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    """A user-accepted evidence requirement evaluated by an EvidenceGate."""

    requirement_id: str
    kind: str
    minimum_count: int = 1
    distinct_by: str = ""
    predicates: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.requirement_id.strip() or not self.kind.strip():
            raise ValueError("evidence requirement_id and kind must not be empty")
        if self.minimum_count < 1:
            raise ValueError("evidence requirement minimum_count must be positive")
        if not isinstance(self.predicates, Mapping):
            raise ValueError("evidence requirement predicates must be an object")

    def to_data(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "kind": self.kind,
            "minimum_count": self.minimum_count,
            "distinct_by": self.distinct_by,
            "predicates": dict(self.predicates),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> EvidenceRequirement:
        predicates = data.get("predicates", {})
        if not isinstance(predicates, Mapping):
            raise ValueError("evidence requirement predicates must be an object")
        return cls(
            requirement_id=str(data.get("requirement_id", "")),
            kind=str(data.get("kind", "")),
            minimum_count=int(data.get("minimum_count", 1)),
            distinct_by=str(data.get("distinct_by", "")),
            predicates=dict(predicates),
        )


@dataclass(frozen=True, slots=True)
class TaskContract:
    """A versioned task contract; only explicit ACTIVE revisions are mandatory."""

    objective: str
    authority: ContractAuthority
    evidence_requirements: tuple[EvidenceRequirement, ...] = ()
    guidance: Mapping[str, Any] = field(default_factory=dict)
    contract_id: str = ""
    revision: int = 0
    phase: ContractPhase | None = None
    deterministic_gates: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ValueError("task contract objective must not be empty")
        ids = [item.requirement_id for item in self.evidence_requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("task contract requirement ids must be unique")
        if not isinstance(self.guidance, Mapping):
            raise ValueError("task contract guidance must be an object")
        if self.revision < 0:
            raise ValueError("task contract revision must not be negative")
        if not all(isinstance(item, Mapping) for item in self.deterministic_gates):
            raise ValueError("task contract deterministic_gates must be an object list")
        phase = self.phase or (
            ContractPhase.ACTIVE
            if self.authority is ContractAuthority.USER_EXPLICIT
            else ContractPhase.DRAFT
        )
        object.__setattr__(self, "phase", phase)

    @property
    def is_mandatory(self) -> bool:
        return (
            self.authority is ContractAuthority.USER_EXPLICIT
            and self.phase is ContractPhase.ACTIVE
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "revision": self.revision,
            "objective": self.objective,
            "authority": self.authority.value,
            "phase": self.phase.value,
            "guidance": dict(self.guidance),
            "evidence_requirements": [item.to_data() for item in self.evidence_requirements],
            "deterministic_gates": [dict(item) for item in self.deterministic_gates],
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> TaskContract:
        requirements = data.get("evidence_requirements", [])
        guidance = data.get("guidance", {})
        if "gates" in data:
            raise ValueError("task contract gates are not supported; use deterministic_gates")
        gates = data.get("deterministic_gates", ())
        if not isinstance(requirements, list) or not all(isinstance(item, Mapping) for item in requirements):
            raise ValueError("task contract evidence_requirements must be an object list")
        if not isinstance(guidance, Mapping):
            raise ValueError("task contract guidance must be an object")
        if not isinstance(gates, (list, tuple)) or not all(isinstance(item, Mapping) for item in gates):
            raise ValueError("task contract deterministic_gates must be an object list")
        raw_authority = str(data.get("authority", ContractAuthority.MODEL_DRAFT.value))
        authority = ContractAuthority({
            "USER": ContractAuthority.USER_EXPLICIT.value,
            "DRAFT": ContractAuthority.MODEL_DRAFT.value,
        }.get(raw_authority, raw_authority))
        raw_phase = data.get("phase")
        return cls(
            contract_id=str(data.get("contract_id", "")),
            revision=int(data.get("revision", 0)),
            objective=str(data.get("objective", "")),
            authority=authority,
            phase=ContractPhase(str(raw_phase)) if raw_phase is not None else None,
            guidance=dict(guidance),
            evidence_requirements=tuple(EvidenceRequirement.from_data(item) for item in requirements),
            deterministic_gates=tuple(dict(item) for item in gates),
        )


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    """A provider-normalized, durable observation from one tool execution."""

    fact_id: str
    kind: str
    execution_ref: str
    attributes: Mapping[str, Any]
    provenance: Mapping[str, Any]
    complete: bool

    def __post_init__(self) -> None:
        if not self.fact_id.strip() or not self.kind.strip() or not self.execution_ref.strip():
            raise ValueError("evidence fact id, kind, and execution_ref must not be empty")
        if not isinstance(self.attributes, Mapping) or not isinstance(self.provenance, Mapping):
            raise ValueError("evidence fact attributes and provenance must be objects")

    def to_data(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "kind": self.kind,
            "execution_ref": self.execution_ref,
            "attributes": dict(self.attributes),
            "provenance": dict(self.provenance),
            "complete": self.complete,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> EvidenceFact:
        attributes = data.get("attributes", {})
        provenance = data.get("provenance", {})
        if not isinstance(attributes, Mapping) or not isinstance(provenance, Mapping):
            raise ValueError("evidence fact attributes and provenance must be objects")
        return cls(
            fact_id=str(data.get("fact_id", "")),
            kind=str(data.get("kind", "")),
            execution_ref=str(data.get("execution_ref", "")),
            attributes=dict(attributes),
            provenance=dict(provenance),
            complete=bool(data.get("complete", False)),
        )


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Serializable decision from an EvidenceGate evaluation."""

    resolution: GateResolution
    missing_requirement_ids: tuple[str, ...] = ()
    required_capability_ids: tuple[str, ...] = ()
    reason_code: str = ""
    detail: str = ""
    recovery_actions: tuple[RecoveryAction, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution.value,
            "missing_requirement_ids": list(self.missing_requirement_ids),
            "required_capability_ids": list(self.required_capability_ids),
            "reason_code": self.reason_code,
            "detail": self.detail,
            "recovery_actions": [item.to_data() for item in self.recovery_actions],
        }


class EvidenceGatePort(Protocol):
    """Evaluates a persisted contract against facts and visible capabilities."""

    def evaluate(
        self,
        contract: TaskContract | None,
        facts: Sequence[EvidenceFact],
        capabilities: frozenset[str],
    ) -> GateDecision: ...
