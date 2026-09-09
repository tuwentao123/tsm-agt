"""Authority-free resource and question references shared across Tasks.

These records preserve where prior evidence was obtained without carrying the
old Tool result body, Task-local resource_ref, approval, process ownership, or
filesystem capability into a later Task.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .configuration import canonical_hash
from .evidence_question import EvidenceQuestionStatus


class SessionResourceKind(StrEnum):
    ROOT = "ROOT"
    ARTIFACT = "ARTIFACT"


@dataclass(frozen=True, slots=True)
class SessionResourceReference:
    catalog_ref: str
    canonical_path: str
    resolved_root: str
    root_kind: str
    resource_kind: SessionResourceKind
    source_task_id: str
    source_turn_id: str
    source_tool: str
    question_ref: str | None = None
    question_status: EvidenceQuestionStatus | None = None
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all((
            self.catalog_ref, self.canonical_path, self.resolved_root,
            self.root_kind, self.source_task_id, self.source_turn_id,
            self.source_tool,
        )):
            raise ValueError("session resource reference fields are required")
        if len(set(self.evidence_references)) != len(self.evidence_references):
            raise ValueError("session resource evidence references must be unique")

    @classmethod
    def create(
        cls, *, canonical_path: str, resolved_root: str, root_kind: str,
        resource_kind: SessionResourceKind, source_task_id: str,
        source_turn_id: str, source_tool: str, question_ref: str | None = None,
        question_status: EvidenceQuestionStatus | None = None,
        evidence_references: tuple[str, ...] = (),
    ) -> SessionResourceReference:
        identity = canonical_hash({
            "canonical_path": canonical_path,
            "resolved_root": resolved_root,
            "source_task_id": source_task_id,
            "resource_kind": resource_kind.value,
        })[:20]
        return cls(
            "catalog-" + identity, canonical_path, resolved_root, root_kind,
            resource_kind, source_task_id, source_turn_id, source_tool,
            question_ref, question_status, evidence_references[:50],
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "catalog_ref": self.catalog_ref,
            "canonical_path": self.canonical_path,
            "resolved_root": self.resolved_root,
            "root_kind": self.root_kind,
            "resource_kind": self.resource_kind.value,
            "source_task_id": self.source_task_id,
            "source_turn_id": self.source_turn_id,
            "source_tool": self.source_tool,
            "question_ref": self.question_ref,
            "question_status": (
                self.question_status.value if self.question_status else None
            ),
            "evidence_references": list(self.evidence_references),
            "authority_inherited": False,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> SessionResourceReference:
        raw_refs = data.get("evidence_references", [])
        if not isinstance(raw_refs, list):
            raise ValueError("session resource evidence references must be a list")
        raw_status = data.get("question_status")
        return cls(
            str(data["catalog_ref"]), str(data["canonical_path"]),
            str(data["resolved_root"]), str(data["root_kind"]),
            SessionResourceKind(str(data["resource_kind"])),
            str(data["source_task_id"]), str(data["source_turn_id"]),
            str(data["source_tool"]),
            (str(data["question_ref"])
             if data.get("question_ref") is not None else None),
            (EvidenceQuestionStatus(str(raw_status))
             if raw_status is not None else None),
            tuple(str(item) for item in raw_refs),
        )


@dataclass(frozen=True, slots=True)
class SessionQuestionReference:
    question_ref: str
    question: str
    status: EvidenceQuestionStatus
    source_task_id: str
    source_turn_id: str
    catalog_refs: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()
    blocking_reason: str | None = None

    def __post_init__(self) -> None:
        if not all((
            self.question_ref, self.question, self.source_task_id,
            self.source_turn_id,
        )):
            raise ValueError("session question reference fields are required")
        if len(set(self.catalog_refs)) != len(self.catalog_refs):
            raise ValueError("session question catalog refs must be unique")

    def to_data(self) -> dict[str, Any]:
        return {
            "question_ref": self.question_ref, "question": self.question,
            "status": self.status.value,
            "source_task_id": self.source_task_id,
            "source_turn_id": self.source_turn_id,
            "catalog_refs": list(self.catalog_refs),
            "evidence_references": list(self.evidence_references),
            "blocking_reason": self.blocking_reason,
            "authority_inherited": False,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> SessionQuestionReference:
        raw_catalog = data.get("catalog_refs", [])
        raw_evidence = data.get("evidence_references", [])
        if not isinstance(raw_catalog, list) or not isinstance(raw_evidence, list):
            raise ValueError("session question references must be lists")
        return cls(
            str(data["question_ref"]), str(data["question"]),
            EvidenceQuestionStatus(str(data["status"])),
            str(data["source_task_id"]), str(data["source_turn_id"]),
            tuple(str(item) for item in raw_catalog),
            tuple(str(item) for item in raw_evidence),
            (str(data["blocking_reason"])
             if data.get("blocking_reason") is not None else None),
        )
