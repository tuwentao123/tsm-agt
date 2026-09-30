"""Semantic review: one structured packet in, one bounded verdict out.

Design constraints (see 《任务证明闭环实施SPEC.md》 §6):

* **Pure** -- no IO, no ``await``, no mutation. Independently unit-testable.
* **Reuses the existing judge port.** No ``SemanticReviewerPort``, no second
  verdict enum, no second fail-closed rule (R1-9).
* **Structured input only.** The packet is built from the contract, the artifact
  subgraph, evidence records and the deterministic result. Worker working
  memory, private prompts, hidden reasoning, write-tool arguments, approval
  tokens and credentials have no field to travel in -- the guarantee is the
  type, not a prompt instruction.
* **Affirmation must cite evidence.** A judge that says "satisfied" without
  naming the evidence it read is treated as ``UNDECIDABLE`` (fail-closed),
  because an uncited affirmation is exactly the "declared, not earned" shape the
  whole scheme exists to remove.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tsm_agt.ports import JudgeVerdict, RubricEvidence

from .artifact_graph import ArtifactGraphProjection
from .evidence_coverage import EvidenceRecord
from .proof_obligation import ProofObligation


@dataclass(frozen=True, slots=True)
class SemanticReviewPacket:
    """Everything a judge may read, and nothing else."""

    task_id: str
    obligation_ids: tuple[str, ...]
    task_contract: Mapping[str, Any] = field(default_factory=dict)
    artifact_subgraph: Mapping[str, Any] = field(default_factory=dict)
    evidence_records: tuple[EvidenceRecord, ...] = ()
    deterministic_acceptance: Mapping[str, Any] = field(default_factory=dict)

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(item.evidence_id for item in self.evidence_records)

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "obligation_ids": list(self.obligation_ids),
            "task_contract": dict(self.task_contract),
            "artifact_subgraph": dict(self.artifact_subgraph),
            "evidence_records": [item.to_data() for item in self.evidence_records],
            "deterministic_acceptance": dict(self.deterministic_acceptance),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> SemanticReviewPacket:
        return cls(
            task_id=str(data["task_id"]),
            obligation_ids=tuple(data.get("obligation_ids") or ()),
            task_contract=dict(data.get("task_contract") or {}),
            artifact_subgraph=dict(data.get("artifact_subgraph") or {}),
            evidence_records=tuple(
                EvidenceRecord.from_data(item)
                for item in data.get("evidence_records") or ()
            ),
            deterministic_acceptance=dict(
                data.get("deterministic_acceptance") or {}
            ),
        )


def build_semantic_review_packet(
    *,
    task_id: str,
    obligations: Sequence[ProofObligation],
    graph: ArtifactGraphProjection,
    evidence: Sequence[EvidenceRecord],
    deterministic_acceptance: Mapping[str, Any],
) -> SemanticReviewPacket:
    """Assemble the packet for the *semantic* obligations only.

    Non-semantic obligations are deliberately excluded: the judge is never asked
    about facts a deterministic stage already settled, which is what keeps it
    from becoming a general-purpose veto.
    """

    semantic = tuple(
        item.obligation_id for item in obligations if item.is_semantic
    )
    wanted = set(semantic)
    relevant = tuple(
        record for record in evidence
        if wanted.intersection(record.obligation_ids)
    )
    referenced = {
        reference.artifact_id
        for record in relevant for reference in record.artifact_refs
    }
    subgraph = graph.to_data()
    subgraph["nodes"] = [
        node for node in subgraph.get("nodes", [])
        if node.get("artifact_id") in referenced
    ]
    subgraph["edges"] = [
        edge for edge in subgraph.get("edges", [])
        if edge.get("source_artifact_id") in referenced
        and edge.get("target_artifact_id") in referenced
    ]
    return SemanticReviewPacket(
        task_id=task_id,
        obligation_ids=semantic,
        task_contract={
            obligation.obligation_id: {
                "contract_ref": obligation.contract_ref,
                "predicate": obligation.predicate.value,
                "expected": dict(obligation.expected),
            }
            for obligation in obligations
            if obligation.obligation_id in wanted
        },
        artifact_subgraph=subgraph,
        evidence_records=relevant,
        deterministic_acceptance=dict(deterministic_acceptance),
    )


def review_evidence(packet: SemanticReviewPacket) -> tuple[RubricEvidence, ...]:
    """The bounded excerpts a judge is shown, already scoped to the packet."""

    excerpts: list[RubricEvidence] = []
    for record in packet.evidence_records:
        excerpts.append(RubricEvidence(
            record.evidence_id,
            _bounded_excerpt(record),
        ))
    return tuple(excerpts)


def _bounded_excerpt(record: EvidenceRecord, limit: int = 800) -> str:
    """Structured facts only, bounded: never a raw tool dump."""

    import json

    rendered = json.dumps(
        {"capability": record.capability.value, "observation": dict(record.observation)},
        ensure_ascii=False, sort_keys=True,
    )
    return rendered if len(rendered) <= limit else rendered[:limit]


@dataclass(frozen=True, slots=True)
class SemanticReviewOutcome:
    """A judge verdict after the citation rule, expressed as a Runtime action."""

    obligation_ids: tuple[str, ...]
    verdict: JudgeVerdict
    #: ``SATISFIED`` | ``NEEDS_REWORK`` | ``NEEDS_REVIEW``
    disposition: str
    reason: str = ""
    cited_evidence_ids: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        return self.disposition == "SATISFIED"

    def to_data(self) -> dict[str, Any]:
        return {
            "obligation_ids": list(self.obligation_ids),
            "verdict": self.verdict.value,
            "disposition": self.disposition,
            "reason": self.reason,
            "cited_evidence_ids": list(self.cited_evidence_ids),
        }


def normalize_judgement(
    *,
    obligation_ids: Sequence[str],
    verdict: JudgeVerdict,
    reason: str = "",
    cited_evidence_ids: Sequence[str] = (),
    packet: SemanticReviewPacket | None = None,
    known_evidence_ids: frozenset[str] | None = None,
) -> SemanticReviewOutcome:
    """Apply the citation rule and map the verdict onto a Runtime disposition.

    Mapping (SPEC §6.2):

    * ``SATISFIED`` **with** valid citations -> ``SATISFIED``;
    * ``SATISFIED`` without them (or citing evidence outside the packet) ->
      ``UNDECIDABLE`` -> ``NEEDS_REVIEW``: fail-closed, because an uncited
      affirmation cannot be checked and must not become a pass;
    * ``NEEDS_REVISION`` -> ``NEEDS_REWORK`` (a structured gap, bounded rework);
    * ``UNDECIDABLE`` / ``JUDGE_ERROR`` -> ``NEEDS_REVIEW``.
    """

    known = (
        packet.evidence_ids if packet is not None
        else frozenset(known_evidence_ids or ())
    )
    cited = tuple(dict.fromkeys(
        item for item in cited_evidence_ids if item in known
    ))
    if verdict is JudgeVerdict.SATISFIED and not cited:
        return SemanticReviewOutcome(
            tuple(obligation_ids), JudgeVerdict.UNDECIDABLE, "NEEDS_REVIEW",
            (
                f"judge affirmed without citing evidence: {reason}"
                if reason else "judge affirmed without citing evidence"
            ),
            (),
        )
    if verdict is JudgeVerdict.SATISFIED:
        return SemanticReviewOutcome(
            tuple(obligation_ids), JudgeVerdict.SATISFIED, "SATISFIED",
            reason, cited,
        )
    if verdict is JudgeVerdict.NEEDS_REVISION:
        return SemanticReviewOutcome(
            tuple(obligation_ids), verdict, "NEEDS_REWORK", reason, cited,
        )
    return SemanticReviewOutcome(
        tuple(obligation_ids), verdict, "NEEDS_REVIEW", reason, cited,
    )
