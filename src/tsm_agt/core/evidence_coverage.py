"""Evidence records and deterministic Coverage over Proof Obligations.

Design constraints (see 《任务证明闭环实施SPEC.md》 §3.3 / §3.4 / §3.7 / §4.4):

* **Pure** -- no IO, no ``await``, no mutation. Coverage is a deterministic
  function of (obligations, evidence, artifact graph).
* **Attribution may be many, sufficiency must be strict** (AG-14). One
  observation may attach to every compatible obligation; whether an obligation
  is ``COVERED`` is then decided per obligation, by capability, scope, artifact
  version and completeness -- never by "a tool succeeded".
* **Narrow evidence may not close a wide obligation.** A single file read cannot
  satisfy an obligation whose scope demands a complete enumeration.
* **No business dispatch.** Nothing here branches on tool name, directory name,
  framework, language or natural-language text (AG-15, Neutrality).
* **Absence is not refutation** (AG-12). A missing obligation is ``MISSING`` (or
  ``BLOCKED`` when absence is being asserted without a defensible scope); it is
  never turned into a failure here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from tsm_agt.ports import CompletionGap, EvidenceCapability, ToolEffect, ToolSpec

from .artifact_graph import ArtifactGraphProjection, ArtifactRef
from .configuration import canonical_hash
from .proof_obligation import ObligationPredicate, ProofObligation


class CoverageStatus(StrEnum):
    COVERED = "COVERED"
    MISSING = "MISSING"
    STALE = "STALE"
    CONFLICTING = "CONFLICTING"
    BLOCKED = "BLOCKED"


class ProofEnforcementMode(StrEnum):
    """How strongly Coverage participates in the Task decision.

    ``OBSERVE`` records without intercepting, which is what P0/P1 run in. Only
    ``ENFORCE_*`` lets Coverage withhold ``SUCCEEDED``.
    """

    OBSERVE = "OBSERVE"
    ENFORCE_NEW_TASKS = "ENFORCE_NEW_TASKS"
    ENFORCE_ALL = "ENFORCE_ALL"


#: Capabilities able to settle each predicate. A predicate is only eligible for
#: ``COVERED`` when a record carries one of these.
_PREDICATE_CAPABILITIES: Mapping[ObligationPredicate, frozenset[EvidenceCapability]] = {
    ObligationPredicate.ARTIFACT_EXISTS: frozenset({
        EvidenceCapability.READ_ARTIFACT,
        EvidenceCapability.MUTATE_ARTIFACT,
        EvidenceCapability.OBSERVE_EXTERNAL,
    }),
    ObligationPredicate.ARTIFACT_CONTENT: frozenset({
        EvidenceCapability.READ_ARTIFACT,
    }),
    ObligationPredicate.RELATION_EXISTS: frozenset({
        EvidenceCapability.OBSERVE_RELATION,
    }),
    ObligationPredicate.EFFECT_OBSERVED: frozenset({
        EvidenceCapability.MUTATE_ARTIFACT,
        EvidenceCapability.OBSERVE_EFFECT,
    }),
    ObligationPredicate.COMMAND_VERIFIED: frozenset({
        EvidenceCapability.EXECUTE_VERIFICATION,
    }),
    ObligationPredicate.QUERY_CARDINALITY: frozenset({
        EvidenceCapability.ENUMERATE_SCOPE,
    }),
    ObligationPredicate.SEMANTIC_REVIEW: frozenset({
        EvidenceCapability.READ_ARTIFACT,
    }),
}

#: Relations may only be claimed from a durable producing event, never from
#: model prose (AG-4).
_DURABLE_RELATION_SOURCES: frozenset[str] = frozenset({
    "parse", "parse_result", "execution", "mutation", "tool_call",
})


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """An observation plus the obligations it was attributed to.

    ``observation`` / ``coverage`` are producer-declared structured facts. The
    evaluator reads a small, documented set of keys; it never parses prose.
    """

    evidence_id: str
    obligation_ids: tuple[str, ...]
    capability: EvidenceCapability
    artifact_refs: tuple[ArtifactRef, ...] = ()
    scope: Mapping[str, Any] = field(default_factory=dict)
    coverage: Mapping[str, Any] = field(default_factory=dict)
    observation: Mapping[str, Any] = field(default_factory=dict)
    #: ``auto_attributed`` (default) or ``explicit`` when the model supplied an
    #: ``evidence_intent``. Diagnostic only; it never changes sufficiency.
    binding_source: str = "auto_attributed"

    def to_data(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "obligation_ids": list(self.obligation_ids),
            "capability": self.capability.value,
            "artifact_refs": [item.to_data() for item in self.artifact_refs],
            "scope": dict(self.scope),
            "coverage": dict(self.coverage),
            "observation": dict(self.observation),
            "binding_source": self.binding_source,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> EvidenceRecord:
        return cls(
            evidence_id=str(data["evidence_id"]),
            obligation_ids=tuple(data.get("obligation_ids") or ()),
            capability=EvidenceCapability(data["capability"]),
            artifact_refs=tuple(
                ArtifactRef.from_data(item)
                for item in data.get("artifact_refs") or ()
            ),
            scope=dict(data.get("scope") or {}),
            coverage=dict(data.get("coverage") or {}),
            observation=dict(data.get("observation") or {}),
            binding_source=str(data.get("binding_source", "auto_attributed")),
        )


@dataclass(frozen=True, slots=True)
class ObligationCoverage:
    obligation_id: str
    status: CoverageStatus
    evidence_ids: tuple[str, ...] = ()
    missing_capabilities: frozenset[EvidenceCapability] = frozenset()
    reason_code: str = ""

    @property
    def covered(self) -> bool:
        return self.status is CoverageStatus.COVERED

    def to_data(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "status": self.status.value,
            "evidence_ids": list(self.evidence_ids),
            "missing_capabilities": sorted(
                item.value for item in self.missing_capabilities
            ),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ProofPlanSnapshot:
    """The compiled plan for one Task at one TaskSpec revision."""

    task_id: str
    task_spec_revision: int
    obligations: tuple[ProofObligation, ...]
    plan_hash: str = ""

    @classmethod
    def build(
        cls, *, task_id: str, task_spec_revision: int,
        obligations: Sequence[ProofObligation],
    ) -> ProofPlanSnapshot:
        ordered = tuple(sorted(obligations, key=lambda item: item.obligation_id))
        plan_hash = canonical_hash({
            "task_id": task_id,
            "task_spec_revision": task_spec_revision,
            "obligations": [item.to_data() for item in ordered],
        })
        return cls(task_id, task_spec_revision, ordered, plan_hash)

    @property
    def required(self) -> tuple[ProofObligation, ...]:
        return tuple(
            item for item in self.obligations if item.required and not item.retired
        )

    @property
    def advisory(self) -> tuple[ProofObligation, ...]:
        return tuple(
            item for item in self.obligations if not item.required and not item.retired
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_spec_revision": self.task_spec_revision,
            "plan_hash": self.plan_hash,
            "obligations": [item.to_data() for item in self.obligations],
        }


def can_succeed(
    *,
    coverages: Iterable[ObligationCoverage],
    deterministic_passed: bool,
    semantic_review_passed: bool = True,
) -> bool:
    """The one mechanical definition of "this Task may succeed" (SPEC §5.1).

    Every required obligation must be ``COVERED`` **and** the existing
    deterministic acceptance must have passed. Under ``OBSERVE`` the caller must
    not act on this value -- it is recorded for diagnosis only.
    """

    return (
        all(item.covered for item in coverages)
        and deterministic_passed
        and semantic_review_passed
    )


@dataclass(frozen=True, slots=True)
class EvidenceIntent:
    """A model's *optional* claim that an action proves specific obligations.

    Optional by design: the default path attributes observations automatically,
    so a model never has to fill this in (SPEC §5.2/§4.6). It exists to let a
    caller say "I ran this to prove precisely these", and it is always validated
    before it can influence anything.
    """

    obligation_ids: tuple[str, ...] = ()
    expected_capability: str = ""
    target_selectors: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_data(cls, data: Any) -> EvidenceIntent | None:
        """Parse tolerantly; ``None`` means "no usable intent"."""

        if not isinstance(data, Mapping):
            return None
        raw_ids = data.get("obligation_ids")
        obligation_ids = (
            tuple(str(item) for item in raw_ids if isinstance(item, str))
            if isinstance(raw_ids, (list, tuple)) else ()
        )
        selectors = data.get("target_selectors")
        parsed_selectors = (
            tuple(dict(item) for item in selectors if isinstance(item, Mapping))
            if isinstance(selectors, (list, tuple)) else ()
        )
        capability = data.get("expected_capability")
        intent = cls(
            obligation_ids=obligation_ids,
            expected_capability=capability if isinstance(capability, str) else "",
            target_selectors=parsed_selectors,
        )
        return None if intent.is_empty else intent

    @property
    def is_empty(self) -> bool:
        return not (self.obligation_ids or self.expected_capability or self.target_selectors)

    def to_data(self) -> dict[str, Any]:
        return {
            "obligation_ids": list(self.obligation_ids),
            "expected_capability": self.expected_capability,
            "target_selectors": [dict(item) for item in self.target_selectors],
        }


@dataclass(frozen=True, slots=True)
class EvidenceIntentValidation:
    """Outcome of validating one intent. Never blocks the action itself."""

    accepted_obligation_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return bool(self.accepted_obligation_ids)


def validate_evidence_intent(
    *,
    intent: EvidenceIntent | None,
    plan: ProofPlanSnapshot,
    tool_capabilities: frozenset[EvidenceCapability],
    graph: ArtifactGraphProjection,
) -> EvidenceIntentValidation:
    """Validate an explicit intent against four checks (SPEC §5.2).

    1. every ``obligation_ids`` entry exists in the current plan;
    2. the tool really declares ``expected_capability``;
    3. every target selector is inside the allowed scope;
    4. ``None`` (no intent at all) changes nothing -- auto attribution runs.

    A violation rejects the **intent**, never the action: the observation still
    runs and still auto-attributes. Rejecting wholesale rather than honouring a
    subset is deliberate -- a claim inconsistent with the plan is a mistake worth
    surfacing, and silently accepting half of it would hide that.
    """

    if intent is None or intent.is_empty:
        return EvidenceIntentValidation()

    reasons: list[str] = []
    known = {item.obligation_id for item in plan.obligations if not item.retired}
    unknown = [item for item in intent.obligation_ids if item not in known]
    if unknown:
        reasons.append("UNKNOWN_OBLIGATION")

    if intent.expected_capability:
        try:
            declared = EvidenceCapability(intent.expected_capability)
        except ValueError:
            reasons.append("UNKNOWN_CAPABILITY")
        else:
            if declared not in tool_capabilities:
                reasons.append("CAPABILITY_NOT_SUPPORTED")
    elif intent.obligation_ids:
        reasons.append("MISSING_EXPECTED_CAPABILITY")

    if intent.target_selectors and not _selectors_are_in_scope(
        intent=intent, plan=plan, graph=graph,
    ):
        reasons.append("SELECTOR_OUT_OF_SCOPE")

    if reasons:
        return EvidenceIntentValidation(
            accepted_obligation_ids=(), reason_codes=tuple(sorted(reasons)),
        )
    return EvidenceIntentValidation(
        accepted_obligation_ids=tuple(dict.fromkeys(intent.obligation_ids)),
    )


def _selectors_are_in_scope(
    *,
    intent: EvidenceIntent, plan: ProofPlanSnapshot,
    graph: ArtifactGraphProjection,
) -> bool:
    """A selector may only name something already observed or contract-declared."""

    observed_ids = {node.artifact_id for node in graph.nodes}
    observed_locators = {node.locator for node in graph.nodes}
    selected = [
        obligation for obligation in plan.obligations
        if obligation.obligation_id in set(intent.obligation_ids)
    ]
    declared_ids: set[str] = set()
    declared_locators: set[str] = set()
    for obligation in selected:
        for container in (obligation.subject_selector, obligation.scope):
            for key in ("artifact_id", "root_artifact_id"):
                value = container.get(key)
                if isinstance(value, str) and value:
                    declared_ids.add(value)
            for key in ("locator", "root_locator"):
                value = container.get(key)
                if isinstance(value, str) and value:
                    declared_locators.add(value)

    for selector in intent.target_selectors:
        identifier = selector.get("artifact_id")
        locator = selector.get("locator")
        if isinstance(identifier, str) and identifier:
            if identifier not in observed_ids and identifier not in declared_ids:
                return False
        if isinstance(locator, str) and locator:
            if locator not in observed_locators and locator not in declared_locators:
                return False
    return True


def _matching_ref(
    obligation: ProofObligation, record: EvidenceRecord,
) -> ArtifactRef | None:
    """A record ref that points at exactly what the obligation names."""

    declared_id = str(obligation.subject_selector.get("artifact_id", ""))
    declared_locator = str(obligation.subject_selector.get("locator", ""))
    for reference in record.artifact_refs:
        if declared_id and reference.artifact_id == declared_id:
            return reference
        if declared_locator and reference.locator == declared_locator:
            return reference
    return None


def _subject_matches(
    obligation: ProofObligation, record: EvidenceRecord,
) -> bool:
    """AG-3: a record may only settle the subject the obligation actually names.

    An obligation with no declared subject is effect- or command-level by
    construction ("some required effect was recorded"), so any matching record
    qualifies. One that *does* name an artifact must be hit by a ref.
    """

    declared = bool(
        obligation.subject_selector.get("locator")
        or obligation.subject_selector.get("artifact_id")
    )
    return True if not declared else _matching_ref(obligation, record) is not None


def _scope_is_fully_observed(
    obligation: ProofObligation, record: EvidenceRecord,
) -> bool:
    """AG-14 guardrail: narrow evidence must not close a wide obligation.

    The requirement comes from the *contract* (``scope.require_complete`` /
    ``scope.member_count``), never from prose.
    """

    require_complete = bool(
        obligation.scope.get("require_complete")
        or obligation.expected.get("require_complete")
    )
    if require_complete and record.coverage.get("complete") is not True:
        return False
    declared = obligation.scope.get("member_count")
    observed = record.coverage.get("observed_count")
    if isinstance(declared, int) and isinstance(observed, int):
        return observed >= declared
    return True


def _scope_root_observed(
    obligation: ProofObligation, graph: ArtifactGraphProjection,
) -> bool:
    """AG-13 condition ①/②: we may only enumerate something already observed."""

    root_id = str(obligation.scope.get("root_artifact_id", ""))
    if root_id:
        return graph.node(root_id) is not None
    root_locator = str(obligation.scope.get("root_locator", ""))
    if root_locator:
        return any(node.locator == root_locator for node in graph.nodes)
    return False


def _evaluate_predicate(
    obligation: ProofObligation,
    records: Sequence[EvidenceRecord],
    graph: ArtifactGraphProjection,
) -> ObligationCoverage:
    """Apply the per-predicate sufficiency rules (SPEC §3.7)."""

    predicate = obligation.predicate
    eligible = _PREDICATE_CAPABILITIES[predicate]
    capable = tuple(item for item in records if item.capability in eligible)
    evidence_ids = tuple(item.evidence_id for item in capable)

    if any(item.observation.get("conflicting") is True for item in records):
        return ObligationCoverage(
            obligation.obligation_id, CoverageStatus.CONFLICTING,
            tuple(item.evidence_id for item in records),
            frozenset(), "EVIDENCE_CONFLICT",
        )

    if not capable:
        return ObligationCoverage(
            obligation.obligation_id, CoverageStatus.MISSING, evidence_ids,
            frozenset(obligation.required_capabilities) - {
                item.capability for item in records
            },
            "NO_ELIGIBLE_CAPABILITY",
        )

    if predicate in {
        ObligationPredicate.ARTIFACT_EXISTS, ObligationPredicate.ARTIFACT_CONTENT,
    }:
        saw_ref = False
        saw_stale = False
        for record in capable:
            reference = _matching_ref(obligation, record)
            if reference is None:
                continue
            saw_ref = True
            if not _scope_is_fully_observed(obligation, record):
                continue
            if predicate is ObligationPredicate.ARTIFACT_CONTENT:
                if graph.is_current(reference):
                    return ObligationCoverage(
                        obligation.obligation_id, CoverageStatus.COVERED,
                        (record.evidence_id,), frozenset(), "CURRENT_VERSION_READ",
                    )
                saw_stale = True
                continue
            return ObligationCoverage(
                obligation.obligation_id, CoverageStatus.COVERED,
                (record.evidence_id,), frozenset(), "ARTIFACT_OBSERVED",
            )
        if saw_ref and saw_stale:
            return ObligationCoverage(
                obligation.obligation_id, CoverageStatus.STALE, evidence_ids,
                frozenset(), "ARTIFACT_VERSION_CHANGED",
            )
        if saw_ref:
            return ObligationCoverage(
                obligation.obligation_id, CoverageStatus.MISSING, evidence_ids,
                frozenset(), "NARROW_EVIDENCE_CANNOT_CLOSE_WIDE_OBLIGATION",
            )
        return ObligationCoverage(
            obligation.obligation_id, CoverageStatus.MISSING, evidence_ids,
            frozenset(), "OBSERVED_A_DIFFERENT_SUBJECT",
        )

    if predicate is ObligationPredicate.RELATION_EXISTS:
        for record in capable:
            if str(record.observation.get("relation_source", "")) in (
                _DURABLE_RELATION_SOURCES
            ):
                return ObligationCoverage(
                    obligation.obligation_id, CoverageStatus.COVERED,
                    (record.evidence_id,), frozenset(), "RELATION_FROM_DURABLE_SOURCE",
                )
        return ObligationCoverage(
            obligation.obligation_id, CoverageStatus.BLOCKED, evidence_ids,
            frozenset(), "RELATION_SOURCE_NOT_DURABLE",
        )

    if predicate is ObligationPredicate.EFFECT_OBSERVED:
        for record in capable:
            if not _subject_matches(obligation, record):
                continue
            if record.observation.get("effect_status") == "succeeded":
                return ObligationCoverage(
                    obligation.obligation_id, CoverageStatus.COVERED,
                    (record.evidence_id,), frozenset(), "EFFECT_RECORDED",
                )
        return ObligationCoverage(
            obligation.obligation_id, CoverageStatus.MISSING, evidence_ids,
            frozenset(), "EFFECT_NOT_RECORDED",
        )

    if predicate is ObligationPredicate.COMMAND_VERIFIED:
        for record in capable:
            if not _subject_matches(obligation, record):
                continue
            if record.observation.get("exit_code") == 0:
                return ObligationCoverage(
                    obligation.obligation_id, CoverageStatus.COVERED,
                    (record.evidence_id,), frozenset(), "COMMAND_SUCCEEDED",
                )
        return ObligationCoverage(
            obligation.obligation_id, CoverageStatus.MISSING, evidence_ids,
            frozenset(), "COMMAND_NOT_VERIFIED",
        )

    if predicate is ObligationPredicate.QUERY_CARDINALITY:
        # AG-13: absence may only be certified from a defensible scope.
        if not _scope_root_observed(obligation, graph):
            return ObligationCoverage(
                obligation.obligation_id, CoverageStatus.BLOCKED, evidence_ids,
                frozenset(), "SCOPE_ROOT_NOT_OBSERVED",
            )
        for record in capable:
            if record.coverage.get("complete") is not True:
                continue
            expected = obligation.expected.get("count")
            observed = record.coverage.get("count")
            if expected is None or observed == expected:
                return ObligationCoverage(
                    obligation.obligation_id, CoverageStatus.COVERED,
                    (record.evidence_id,), frozenset(), "ENUMERATION_COMPLETE",
                )
            return ObligationCoverage(
                obligation.obligation_id, CoverageStatus.BLOCKED, evidence_ids,
                frozenset(), "CARDINALITY_MISMATCH",
            )
        return ObligationCoverage(
            obligation.obligation_id, CoverageStatus.BLOCKED, evidence_ids,
            frozenset(), "ENUMERATION_INCOMPLETE",
        )

    # SEMANTIC_REVIEW: only a judge verdict that cites its evidence may affirm.
    for record in capable:
        observation = record.observation
        if observation.get("verdict") == "SATISFIED":
            if observation.get("cited_evidence_ids"):
                return ObligationCoverage(
                    obligation.obligation_id, CoverageStatus.COVERED,
                    (record.evidence_id,), frozenset(), "JUDGE_SATISFIED",
                )
            return ObligationCoverage(
                obligation.obligation_id, CoverageStatus.BLOCKED, evidence_ids,
                frozenset(), "JUDGE_AFFIRMED_WITHOUT_EVIDENCE",
            )
        if observation.get("verdict") in {"UNDECIDABLE", "JUDGE_ERROR"}:
            return ObligationCoverage(
                obligation.obligation_id, CoverageStatus.BLOCKED, evidence_ids,
                frozenset(), "JUDGE_COULD_NOT_DECIDE",
            )
    return ObligationCoverage(
        obligation.obligation_id, CoverageStatus.MISSING, evidence_ids,
        frozenset(), "REVIEW_NOT_PERFORMED",
    )


def evaluate_coverage(
    *,
    obligations: Sequence[ProofObligation],
    evidence: Sequence[EvidenceRecord],
    graph: ArtifactGraphProjection,
) -> tuple[ObligationCoverage, ...]:
    """Deterministically evaluate every active obligation.

    Retired obligations are skipped: a TaskSpec revision may drop a requirement,
    and a dropped requirement must not block the Task forever.
    """

    by_obligation: dict[str, list[EvidenceRecord]] = {}
    for record in evidence:
        for obligation_id in record.obligation_ids:
            by_obligation.setdefault(obligation_id, []).append(record)
    return tuple(
        _evaluate_predicate(
            obligation, tuple(by_obligation.get(obligation.obligation_id, ())),
            graph,
        )
        for obligation in obligations
        if not obligation.retired
    )


#: Coverage kinds that must reach the readiness gate as **required** gaps.
#: Kept separate from ``_JUDGED_GAP_KINDS`` in the Kernel: those are exempt from
#: the INV-3 demotion because a judge or a declared-unfixable effect owns them,
#: while these are exempt because the coverage layer owes a bounded escalation to
#: human review. Merging the two would let either reason's evolution silently
#: change the other's behaviour (SPEC §12.6 D3).
_COVERAGE_GAP_KIND_BY_STATUS: Mapping[CoverageStatus, str] = {
    CoverageStatus.MISSING: "PROOF_COVERAGE_MISSING",
    CoverageStatus.STALE: "PROOF_COVERAGE_STALE",
    CoverageStatus.CONFLICTING: "PROOF_COVERAGE_CONFLICTING",
    CoverageStatus.BLOCKED: "PROOF_COVERAGE_BLOCKED",
}

#: The parallel exemption set (SPEC §12.6 D3).
ESCALATED_GAP_KINDS: frozenset[str] = frozenset(
    _COVERAGE_GAP_KIND_BY_STATUS.values()
)

#: Statuses that can never be closed by acting again, so they must not advertise
#: a recoverable effect or a candidate tool (SPEC §3.5 rule 1).
_UNCLOSABLE_COVERAGE_STATUSES: frozenset[CoverageStatus] = frozenset({
    CoverageStatus.CONFLICTING, CoverageStatus.BLOCKED,
})


def coverage_gaps(
    *,
    plan: ProofPlanSnapshot,
    coverages: Sequence[ObligationCoverage],
    tools: Sequence[ToolSpec],
) -> tuple[CompletionGap, ...]:
    """Map coverage results onto the readiness gate's ``CompletionGap`` (AG-11).

    The gate's decision input is ``required_effects``, **not** the ``recoverable``
    boolean: a non-empty effect set means "closable", whatever ``recoverable``
    says. So an unclosable status must clear the effects, and every kind here is
    ``required`` -- demoting a coverage gap would turn "we could not prove it"
    into "we may succeed anyway".
    """

    by_id = {item.obligation_id: item for item in plan.obligations}
    gaps: list[CompletionGap] = []
    for coverage in coverages:
        if coverage.status is CoverageStatus.COVERED:
            continue
        obligation = by_id.get(coverage.obligation_id)
        if obligation is None or obligation.retired:
            continue
        kind = _COVERAGE_GAP_KIND_BY_STATUS[coverage.status]
        providers = [
            spec for spec in tools
            if obligation.required_capabilities.intersection(
                capability.value for capability in spec.evidence_capabilities
            )
        ]
        candidates = tuple(sorted({spec.name for spec in providers}))
        if coverage.status in _UNCLOSABLE_COVERAGE_STATUSES:
            effects: tuple[ToolEffect, ...] = ()
            candidates = ()
            recoverable = False
        else:
            effects = tuple(sorted(
                {spec.effect for spec in providers if spec.effect is not ToolEffect.UNSPECIFIED},
                key=lambda item: item.value,
            ))
            recoverable = bool(candidates)
        gaps.append(CompletionGap(
            gap_id=f"proof:{obligation.obligation_id}",
            kind=kind,
            description=(
                f"{coverage.status.value} proof obligation "
                f"{obligation.obligation_id} ({obligation.predicate.value})"
            ),
            status=coverage.status.value,
            required=True,
            recoverable=recoverable,
            # The contract reference is the provenance: it lets the INV-3
            # criterion tell "a real requirement we cannot close" apart from
            # "a gap nobody asked for".
            evidence_reference=obligation.contract_ref,
            expected_scope=",".join(sorted(obligation.required_capabilities)),
            required_effects=effects,
            candidate_tools=candidates,
            observed=coverage.reason_code,
        ))
    return tuple(gaps)
