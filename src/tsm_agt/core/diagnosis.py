"""Inspectable diagnosis state used to keep an investigation evidence-led.

The Runtime stores this compact record but never creates hypotheses, chooses a
probe, or decides that a repair is sufficient.  Those remain model decisions
grounded in the tools and instructions available for a particular project.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DiagnosisMode(StrEnum):
    """Whether the user asked to diagnose only or diagnose and repair."""

    DIAGNOSE = "diagnose"
    REPAIR = "repair"


class DiagnosisStatus(StrEnum):
    OPEN = "open"
    DIAGNOSED = "diagnosed"
    REPAIRED = "repaired"
    BLOCKED = "blocked"


class HypothesisStatus(StrEnum):
    OPEN = "open"
    SUPPORTED = "supported"
    REJECTED = "rejected"


class ReplayStatus(StrEnum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class DiagnosisHypothesis:
    """A short, falsifiable explanation and the evidence that bears on it."""

    hypothesis_id: str
    claim: str
    status: HypothesisStatus
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_text(self.hypothesis_id, "diagnosis hypothesis id", 120)
        _validate_text(self.claim, "diagnosis hypothesis claim", 1000)
        _validate_reference_list(self.evidence_refs, "diagnosis hypothesis evidence")
        if (
            self.status in {HypothesisStatus.SUPPORTED, HypothesisStatus.REJECTED}
            and not self.evidence_refs
        ):
            raise ValueError(
                "supported or rejected diagnosis hypotheses require evidence"
            )

    def to_data(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "claim": self.claim,
            "status": self.status.value,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> DiagnosisHypothesis:
        return cls(
            hypothesis_id=_required_string(data, "hypothesis_id"),
            claim=_required_string(data, "claim"),
            status=HypothesisStatus(_required_string(data, "status")),
            evidence_refs=_reference_tuple(data, "evidence_refs"),
        )


@dataclass(frozen=True, slots=True)
class DiagnosticFrame:
    """A bounded diagnosis record, deliberately separate from a user Task goal.

    It contains conclusions that a user can inspect: observed symptom, candidate
    causes, evidence references, the next discriminating probe, and (for a
    repair request) the original-scenario replay result.  It never stores
    private reasoning or raw logs.
    """

    mode: DiagnosisMode
    status: DiagnosisStatus
    symptom: str
    expected_behavior: str = ""
    reproduction: str = ""
    hypotheses: tuple[DiagnosisHypothesis, ...] = ()
    next_probe: str = ""
    repair_hypothesis_id: str | None = None
    replay_status: ReplayStatus = ReplayStatus.NOT_RUN
    replay_evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_text(self.symptom, "diagnosis symptom", 1000)
        _validate_optional_text(self.expected_behavior, "expected behavior", 1000)
        _validate_optional_text(self.reproduction, "diagnosis reproduction", 1000)
        _validate_optional_text(self.next_probe, "diagnosis next probe", 1000)
        if len(self.hypotheses) > 3:
            raise ValueError("diagnosis may contain at most three hypotheses")
        if len({item.hypothesis_id for item in self.hypotheses}) != len(self.hypotheses):
            raise ValueError("diagnosis hypothesis IDs must be unique")
        _validate_reference_list(
            self.replay_evidence_refs, "diagnosis replay evidence"
        )

        hypotheses = {item.hypothesis_id: item for item in self.hypotheses}
        supported = {
            item.hypothesis_id for item in self.hypotheses
            if item.status is HypothesisStatus.SUPPORTED
        }
        if self.status is DiagnosisStatus.OPEN and not (
            self.hypotheses or self.next_probe
        ):
            raise ValueError(
                "an open diagnosis requires a hypothesis or next discriminating probe"
            )
        if self.status is DiagnosisStatus.DIAGNOSED and not supported:
            raise ValueError("a diagnosed investigation requires supported evidence")
        if self.mode is DiagnosisMode.DIAGNOSE:
            if self.repair_hypothesis_id is not None:
                raise ValueError("diagnose-only frames cannot select a repair hypothesis")
            if self.replay_status is not ReplayStatus.NOT_RUN or self.replay_evidence_refs:
                raise ValueError("diagnose-only frames cannot record a repair replay")
            if self.status is DiagnosisStatus.REPAIRED:
                raise ValueError("diagnose-only frames cannot be marked repaired")
        elif self.repair_hypothesis_id is not None:
            chosen = hypotheses.get(self.repair_hypothesis_id)
            if chosen is None or chosen.status is not HypothesisStatus.SUPPORTED:
                raise ValueError(
                    "a repair hypothesis must identify a supported hypothesis"
                )
        if self.status is DiagnosisStatus.REPAIRED:
            if self.repair_hypothesis_id is None:
                raise ValueError("a repaired diagnosis requires its repair hypothesis")
            if self.replay_status is not ReplayStatus.PASSED:
                raise ValueError("a repaired diagnosis requires a passed replay")
            if not self.replay_evidence_refs:
                raise ValueError("a repaired diagnosis requires replay evidence")
        if (
            self.replay_status in {ReplayStatus.PASSED, ReplayStatus.FAILED}
            and not self.replay_evidence_refs
        ):
            raise ValueError("a completed replay requires evidence")

    def evidence_references(self) -> tuple[str, ...]:
        """All Runtime references which must exist before the frame is saved."""
        return tuple(
            reference
            for hypothesis in self.hypotheses
            for reference in hypothesis.evidence_refs
        ) + self.replay_evidence_refs

    def to_data(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "status": self.status.value,
            "symptom": self.symptom,
            "expected_behavior": self.expected_behavior,
            "reproduction": self.reproduction,
            "hypotheses": [item.to_data() for item in self.hypotheses],
            "next_probe": self.next_probe,
            "repair_hypothesis_id": self.repair_hypothesis_id,
            "replay_status": self.replay_status.value,
            "replay_evidence_refs": list(self.replay_evidence_refs),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> DiagnosticFrame:
        raw_hypotheses = data.get("hypotheses", [])
        if not isinstance(raw_hypotheses, list) or any(
            not isinstance(item, Mapping) for item in raw_hypotheses
        ):
            raise ValueError("diagnosis hypotheses must be a list of objects")
        raw_repair_hypothesis_id = data.get("repair_hypothesis_id")
        if raw_repair_hypothesis_id is not None and not isinstance(
            raw_repair_hypothesis_id, str
        ):
            raise ValueError("repair_hypothesis_id must be a string or null")
        return cls(
            mode=DiagnosisMode(_required_string(data, "mode")),
            status=DiagnosisStatus(_required_string(data, "status")),
            symptom=_required_string(data, "symptom"),
            expected_behavior=_optional_string(data, "expected_behavior"),
            reproduction=_optional_string(data, "reproduction"),
            hypotheses=tuple(
                DiagnosisHypothesis.from_data(item) for item in raw_hypotheses
            ),
            next_probe=_optional_string(data, "next_probe"),
            repair_hypothesis_id=(
                raw_repair_hypothesis_id.strip()
                if raw_repair_hypothesis_id is not None else None
            ),
            replay_status=ReplayStatus(str(data.get("replay_status", "not_run"))),
            replay_evidence_refs=_reference_tuple(data, "replay_evidence_refs"),
        )


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(
        r"\b(?:api[_-]?key|password|passwd|secret|access[_-]?token)"
        r"\s*[:=]\s*[^\s,;]{6,}", re.I,
    ),
)


def _validate_text(value: str, name: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise ValueError("credentials and private key material cannot enter diagnosis")


def _validate_optional_text(value: str, name: str, maximum: int) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if value:
        _validate_text(value, name, maximum)


def _validate_reference_list(values: tuple[str, ...], name: str) -> None:
    if len(values) > 20:
        raise ValueError(f"{name} exceeds 20 entries")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")
    for value in values:
        _validate_text(value, name, 1000)


def _required_string(data: Mapping[str, Any], key: str) -> str:
    if key not in data or not isinstance(data[key], str):
        raise ValueError(f"diagnosis {key} must be a string")
    return data[key].strip()


def _optional_string(data: Mapping[str, Any], key: str) -> str:
    raw = data.get(key, "")
    if not isinstance(raw, str):
        raise ValueError(f"diagnosis {key} must be a string")
    return raw.strip()


def _reference_tuple(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    raw = data.get(key, [])
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError(f"diagnosis {key} must be a list of strings")
    return tuple(item.strip() for item in raw)
