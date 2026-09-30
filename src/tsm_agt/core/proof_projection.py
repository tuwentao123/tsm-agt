"""Replay Proof Plan / Artifact Graph / Evidence / Coverage from Runtime events.

Design constraints (see 《任务证明闭环实施SPEC.md》 §3.4 / §7 P0):

* **Pure and replayable** -- everything is rebuilt from durable events, so the
  projection can be recomputed at any time and stored in a checkpoint.
* **No raw tool output** in the summary: counts, obligation ids and reason codes
  only (SPEC §7 P4).
* **Read-only in ``OBSERVE``**: this module never mutates a Task or a terminal
  state. P0 uses the result for diagnosis only.
* Event names are declared here so the emitter and the replayer cannot drift.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tsm_agt.ports import (
    EvidenceCapability, RuntimeEvent, ToolSpec, derive_evidence_capabilities,
)

from .artifact_graph import (
    ArtifactEdge, ArtifactGraphProjection, ArtifactKind, ArtifactNode, ArtifactRef,
)
from .configuration import canonical_hash
from .evidence_coverage import (
    CoverageStatus, EvidenceIntent, EvidenceIntentValidation, EvidenceRecord,
    ObligationCoverage, ProofEnforcementMode, ProofPlanSnapshot,
    evaluate_coverage, validate_evidence_intent,
)
from .execution import ToolCommitState, ToolExecutionRecord
from .execution_facts import project_execution_facts
from .task_spec import CompilationRejection, ProofCompilation, TaskSpecSnapshot
from .workspace import MutationRecord

#: Event types owned by the proof layer.
PROOF_PLAN_PUBLISHED = "proof.plan_published"
PROOF_PLAN_TRUNCATED = "proof.plan_truncated"
PROOF_OBLIGATION_UNFULFILLABLE = "proof.obligation_unfulfillable"
PROOF_OBLIGATION_ADVISORY = "proof.obligation_advisory"
PROOF_OBLIGATION_REJECTED = "proof.obligation_rejected"
PROOF_OBSERVATION_RECORDED = "proof.observation_recorded"
PROOF_COVERAGE_EVALUATED = "proof.coverage_evaluated"
#: AG-13: the scope an obligation is judged against must be auditable -- both
#: what the contract declared and every later narrowing must leave an event.
PROOF_SCOPE_DECLARED = "proof.scope_declared"
PROOF_SCOPE_NARROWED = "proof.scope_narrowed"
PROOF_ENFORCEMENT_CHANGED = "proof.enforcement_changed"
ARTIFACT_OBSERVED = "artifact.observed"
ARTIFACT_RELATION_OBSERVED = "artifact.relation_observed"
EVIDENCE_RECORDED = "evidence.recorded"


@dataclass(frozen=True, slots=True)
class ProofProjection:
    """Everything the proof layer knows about one Task, at one event sequence."""

    plan: ProofPlanSnapshot | None = None
    graph: ArtifactGraphProjection = field(default_factory=ArtifactGraphProjection)
    evidence: tuple[EvidenceRecord, ...] = ()
    coverages: tuple[ObligationCoverage, ...] = ()
    unfulfillable: tuple[str, ...] = ()
    advisories: tuple[str, ...] = ()
    rejected: tuple[CompilationRejection, ...] = ()
    truncated: bool = False
    required_overflow: bool = False
    observation_count: int = 0
    auto_attributed_count: int = 0
    observation_ids: tuple[str, ...] = ()
    declared_scopes: tuple[str, ...] = ()
    #: Obligations whose observed scope turned out narrower than the contract
    #: declared it -- never a silent downgrade (AG-13).
    narrowed_scopes: tuple[str, ...] = ()

    @property
    def required_coverages(self) -> tuple[ObligationCoverage, ...]:
        if self.plan is None:
            return ()
        required_ids = {item.obligation_id for item in self.plan.required}
        return tuple(
            item for item in self.coverages if item.obligation_id in required_ids
        )

    @property
    def coverage_counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in CoverageStatus}
        for item in self.coverages:
            counts[item.status.value] += 1
        return counts

    @property
    def auto_attribution_rate(self) -> float | None:
        """Share of observations that could be attributed to an obligation.

        ``None`` (not ``0.0``) when there is nothing to measure, so a caller
        cannot mistake "no data" for "the compiler is broken".
        """

        if not self.observation_count:
            return None
        return self.auto_attributed_count / self.observation_count

    @property
    def advisory_obligation_rate(self) -> float | None:
        """Share of compiled obligations that are diagnostic-only.

        A high rate means the structured compile source is too thin, which is a
        compiler-quality signal -- it is the trigger for reconsidering an
        optional ``ProofTemplate`` (SPEC §9), not a reason to ask the model to
        fill in more by hand.
        """

        if self.plan is None or not self.plan.obligations:
            return None
        return len(self.plan.advisory) / len(self.plan.obligations)

    @property
    def coverage_gap_ratio(self) -> float | None:
        """Share of required obligations that are not yet ``COVERED``."""

        required = self.required_coverages
        if not required:
            return None
        return sum(1 for item in required if not item.covered) / len(required)

    @property
    def attribution_gap(self) -> tuple[str, ...]:
        """Required obligations nobody could even attribute evidence to.

        A non-empty value is a *compiler* signal, not a model failure: it means
        the compiled locator / Scope never matched an observation, so no amount
        of re-asking the model would close it (SPEC §3.7).
        """

        return tuple(
            item.obligation_id for item in self.required_coverages
            if item.reason_code in {"NO_ELIGIBLE_CAPABILITY", "OBSERVED_A_DIFFERENT_SUBJECT"}
        )

    def summary(
        self, *, mode: ProofEnforcementMode | str = ProofEnforcementMode.OBSERVE,
    ) -> dict[str, Any]:
        """Counts and identifiers only -- never raw tool output.

        ``mode`` accepts the enum or its value; ``StrEnum`` renders as its value,
        so the payload stays a plain string either way.
        """

        mode_label = str(mode)
        if self.plan is None:
            return {
                "present": False, "mode": mode_label,
                "obligations": {"required": 0, "advisory": 0, "retired": 0},
                "coverage": self.coverage_counts, "gaps": [],
                "unfulfillable": [], "truncated": False,
                "required_overflow": False, "auto_attribution_rate": None,
                "advisory_obligation_rate": None, "coverage_gap_ratio": None,
                "attribution_gap": [],
            }
        retired = sum(1 for item in self.plan.obligations if item.retired)
        return {
            "present": True,
            "mode": mode_label,
            "plan_hash": self.plan.plan_hash,
            "task_spec_revision": self.plan.task_spec_revision,
            "obligations": {
                "required": len(self.plan.required),
                "advisory": len(self.plan.advisory),
                "retired": retired,
                "total": len(self.plan.obligations),
            },
            "coverage": self.coverage_counts,
            "gaps": [
                {
                    "obligation_id": item.obligation_id,
                    "status": item.status.value,
                    "reason_code": item.reason_code,
                }
                for item in self.coverages
                if item.status is not CoverageStatus.COVERED
            ],
            "unfulfillable": list(self.unfulfillable),
            "truncated": self.truncated,
            "required_overflow": self.required_overflow,
            "auto_attribution_rate": self.auto_attribution_rate,
            "advisory_obligation_rate": self.advisory_obligation_rate,
            "coverage_gap_ratio": self.coverage_gap_ratio,
            "attribution_gap": list(self.attribution_gap),
            "declared_scopes": list(self.declared_scopes),
            "narrowed_scopes": list(self.narrowed_scopes),
        }

    def to_data(self) -> dict[str, Any]:
        return {
            "plan": None if self.plan is None else self.plan.to_data(),
            "graph": self.graph.to_data(),
            "evidence": [item.to_data() for item in self.evidence],
            "unfulfillable": list(self.unfulfillable),
            "advisories": list(self.advisories),
            "rejected": [item.to_data() for item in self.rejected],
            "truncated": self.truncated,
            "required_overflow": self.required_overflow,
            "observation_count": self.observation_count,
            "auto_attributed_count": self.auto_attributed_count,
        }


def replay_proof_projection(
    events: Sequence[RuntimeEvent],
) -> ProofProjection:
    """Rebuild the proof projection from a Task's durable events.

    Tolerant by construction: an unknown event type is ignored, and a malformed
    proof payload is skipped rather than making the whole Task unreadable. The
    proof layer must never be able to break the existing reconstruction paths.
    """

    plan: ProofPlanSnapshot | None = None
    graph = ArtifactGraphProjection()
    evidence: list[EvidenceRecord] = []
    unfulfillable: list[str] = []
    advisories: list[str] = []
    rejected: list[CompilationRejection] = []
    truncated = False
    required_overflow = False
    observation_count = 0
    auto_attributed_count = 0
    observation_ids: list[str] = []
    declared_scopes: list[str] = []
    narrowed_scopes: list[str] = []

    for event in sorted(events, key=lambda item: item.sequence):
        event_type = event.event_type
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        try:
            if event_type == PROOF_PLAN_PUBLISHED:
                raw = payload.get("snapshot")
                if isinstance(raw, Mapping):
                    plan = ProofPlanSnapshot.build(
                        task_id=str(raw.get("task_id", event.task_id)),
                        task_spec_revision=int(raw.get("task_spec_revision", 1)),
                        obligations=tuple(
                            _obligation(item) for item in raw.get("obligations") or ()
                        ),
                    )
            elif event_type == PROOF_PLAN_TRUNCATED:
                truncated = True
                required_overflow = required_overflow or bool(
                    payload.get("required_overflow")
                )
            elif event_type == PROOF_OBLIGATION_UNFULFILLABLE:
                unfulfillable.append(str(payload.get("obligation_id", "")))
            elif event_type == PROOF_OBLIGATION_ADVISORY:
                advisories.append(str(payload.get("obligation_id", "")))
            elif event_type == PROOF_OBLIGATION_REJECTED:
                rejected.append(CompilationRejection(
                    str(payload.get("contract_ref", "")),
                    str(payload.get("reason", "")),
                ))
            elif event_type == PROOF_SCOPE_DECLARED:
                declared_scopes.append(str(payload.get("obligation_id", "")))
            elif event_type == PROOF_SCOPE_NARROWED:
                narrowed_scopes.append(str(payload.get("obligation_id", "")))
            elif event_type == PROOF_OBSERVATION_RECORDED:
                observation_count += 1
                observation_id = str(payload.get("observation_id", ""))
                if observation_id:
                    observation_ids.append(observation_id)
                if payload.get("attributed"):
                    auto_attributed_count += 1
            elif event_type == ARTIFACT_OBSERVED:
                raw = payload.get("node")
                if isinstance(raw, Mapping):
                    graph = graph.observe_node(ArtifactNode.from_data(raw))
            elif event_type == ARTIFACT_RELATION_OBSERVED:
                raw = payload.get("edge")
                if isinstance(raw, Mapping):
                    graph = graph.observe_edge(ArtifactEdge.from_data(raw))
            elif event_type == EVIDENCE_RECORDED:
                raw = payload.get("record")
                if isinstance(raw, Mapping):
                    evidence.append(EvidenceRecord.from_data(raw))
        except (KeyError, TypeError, ValueError):
            # A malformed proof payload must never break Task reconstruction.
            continue

    coverages = (
        ()
        if plan is None
        else evaluate_coverage(
            obligations=plan.obligations, evidence=tuple(evidence), graph=graph,
        )
    )
    return ProofProjection(
        plan=plan, graph=graph, evidence=tuple(evidence), coverages=coverages,
        unfulfillable=tuple(unfulfillable), advisories=tuple(advisories),
        rejected=tuple(rejected), truncated=truncated,
        required_overflow=required_overflow,
        observation_count=observation_count,
        auto_attributed_count=auto_attributed_count,
        observation_ids=tuple(observation_ids),
        declared_scopes=tuple(declared_scopes),
        narrowed_scopes=tuple(narrowed_scopes),
    )


def _obligation(raw: Any):
    from .proof_obligation import ProofObligation

    if not isinstance(raw, Mapping):
        raise ValueError("obligation payload is not a mapping")
    return ProofObligation.from_data(raw)


#: The gate that must pass before coverage may *withhold* success (SPEC §9).
#: Deliberately conservative: a wrong "ready" verdict causes systematic
#: mis-blocking, while a late one only costs more observation time.
MIN_ENFORCEMENT_WINDOW = 20
MIN_AUTO_ATTRIBUTION_RATE = 0.95
MAX_ADVISORY_OBLIGATION_RATE = 0.30


@dataclass(frozen=True, slots=True)
class EnforcementReadiness:
    ready: bool
    reason_codes: tuple[str, ...]
    sample_size: int
    auto_attribution_rate: float | None
    advisory_obligation_rate: float | None


def enforcement_readiness(
    projections: Sequence[ProofProjection],
) -> EnforcementReadiness:
    """Decide whether ``ENFORCE_NEW_TASKS`` may be switched on (SPEC §7 P1.5).

    The window is the most recent ``MIN_ENFORCEMENT_WINDOW`` settled Tasks. Below
    that sample size the answer is always "not yet": the metric gates an
    irreversible rollout, so noise must not be able to approve it.
    """

    window = tuple(projections)[-MIN_ENFORCEMENT_WINDOW:]
    observations = sum(item.observation_count for item in window)
    attributed = sum(item.auto_attributed_count for item in window)
    rate = (attributed / observations) if observations else None
    advisory_rates = [
        item.advisory_obligation_rate for item in window
        if item.advisory_obligation_rate is not None
    ]
    advisory = (
        sum(advisory_rates) / len(advisory_rates) if advisory_rates else None
    )

    reasons: list[str] = []
    if len(window) < MIN_ENFORCEMENT_WINDOW:
        reasons.append("INSUFFICIENT_SAMPLE")
    if rate is None:
        reasons.append("NO_ATTRIBUTION_DATA")
    elif rate < MIN_AUTO_ATTRIBUTION_RATE:
        reasons.append("AUTO_ATTRIBUTION_BELOW_THRESHOLD")
    if advisory is not None and advisory > MAX_ADVISORY_OBLIGATION_RATE:
        reasons.append("ADVISORY_RATE_ABOVE_CEILING")
    if any(item.required_overflow for item in window):
        # A required obligation was dropped by the bound: the plan cannot be
        # trusted to represent the contract, so enforcement must wait.
        reasons.append("REQUIRED_OVERFLOW_OBSERVED")
    return EnforcementReadiness(
        ready=not reasons, reason_codes=tuple(reasons),
        sample_size=len(window),
        auto_attribution_rate=rate, advisory_obligation_rate=advisory,
    )


#: Deterministic pick when a tool can prove several things. Verification first
#: (the strongest fact), then mutation, then observation.
_CAPABILITY_PRIORITY: tuple[EvidenceCapability, ...] = (
    EvidenceCapability.EXECUTE_VERIFICATION,
    EvidenceCapability.MUTATE_ARTIFACT,
    EvidenceCapability.OBSERVE_EFFECT,
    EvidenceCapability.READ_ARTIFACT,
    EvidenceCapability.ENUMERATE_SCOPE,
    EvidenceCapability.OBSERVE_RELATION,
    EvidenceCapability.OBSERVE_RUNTIME,
    EvidenceCapability.OBSERVE_EXTERNAL,
)


def _representative_capability(
    capabilities: frozenset[EvidenceCapability],
) -> EvidenceCapability | None:
    for candidate in _CAPABILITY_PRIORITY:
        if candidate in capabilities:
            return candidate
    return None


def artifact_id_for_locator(locator: str) -> str:
    """Version-free artifact identity: one locator, one node, across edits."""

    return f"art-{canonical_hash({'locator': locator})[:16]}"


def derive_proof_events(
    *,
    task_spec: TaskSpecSnapshot,
    compilation: ProofCompilation,
    executions: Sequence[ToolExecutionRecord],
    specs_by_name: Mapping[str, ToolSpec],
    mutation_records: Sequence[MutationRecord],
    current: ProofProjection,
    events: Sequence[RuntimeEvent] = (),
    available_capabilities: frozenset[str] | None = None,
    mode: ProofEnforcementMode | str = ProofEnforcementMode.OBSERVE,
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """Derive the proof-layer events a Task has not recorded yet.

    Pure: it only reads the current projection, the frozen execution ledger and
    the mutation journal. The kernel appends the result verbatim, so all of this
    is unit-testable without a Runtime.

    Idempotent: a published plan is keyed by ``plan_hash``, an observation by
    ``execution_id``, an artifact by ``(artifact_id, version)`` and evidence by
    its derived id. Re-running therefore emits nothing new.
    """

    pending: list[tuple[str, Mapping[str, Any]]] = []

    if current.plan is None or current.plan.plan_hash != compilation.plan.plan_hash:
        pending.append((PROOF_PLAN_PUBLISHED, {
            "snapshot": compilation.plan.to_data(),
        }))
        for obligation in compilation.unfulfillable:
            pending.append((PROOF_OBLIGATION_UNFULFILLABLE, {
                "obligation_id": obligation.obligation_id,
                "reason": "NO_AVAILABLE_CAPABILITY",
            }))
        for obligation in compilation.advisories:
            pending.append((PROOF_OBLIGATION_ADVISORY, {
                "obligation_id": obligation.obligation_id,
                "reason": "PROSE_ONLY",
            }))
        for rejection in compilation.rejected:
            pending.append((PROOF_OBLIGATION_REJECTED, rejection.to_data()))
        if compilation.truncation.truncated:
            pending.append((PROOF_PLAN_TRUNCATED, {
                "dropped": len(compilation.truncation.dropped),
                "required_overflow": compilation.truncation.required_overflow,
            }))
        # AG-13: every scope the contract declares is recorded, so a later
        # narrowing can be recognised as a change rather than an assumption.
        for obligation in compilation.plan.obligations:
            if obligation.scope:
                pending.append((PROOF_SCOPE_DECLARED, {
                    "obligation_id": obligation.obligation_id,
                    "scope": dict(obligation.scope),
                    "source": "contract",
                }))

    # --- Artifacts: only from durable mutation records (real observations) ---
    observed_mutations: set[str] = set()
    for event in events:
        if event.event_type == ARTIFACT_OBSERVED:
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            raw = payload.get("node")
            if isinstance(raw, Mapping):
                observed_mutations.add(
                    str((raw.get("attributes") or {}).get("mutation_id", ""))
                )
    for record in mutation_records:
        version = record.after_hash or record.before_hash
        if not version or record.mutation_id in observed_mutations:
            continue
        node = ArtifactNode(
            artifact_id=artifact_id_for_locator(record.path),
            kind=ArtifactKind.FILE,
            locator=record.path,
            version=version,
            attributes={
                "mutation_id": record.mutation_id,
                "operation": record.operation.value,
            },
            source_event_refs=(f"mutation:{record.mutation_id}",),
        )
        if not current.graph.is_current(node.ref()):
            pending.append((ARTIFACT_OBSERVED, {"node": node.to_data()}))

    # --- Observations and evidence from the frozen execution ledger ---
    known_observations = set(current.observation_ids)
    facts_by_execution = {
        fact.execution_id: fact
        for fact in project_execution_facts(
            executions, specs_by_name, include_successful=True,
            known_mutation_ids=frozenset(
                record.mutation_id for record in mutation_records
            ),
        )
    }
    live_graph = current.graph
    for record in sorted(executions, key=lambda item: item.execution_id):
        if record.state is not ToolCommitState.COMMITTED:
            continue
        observation_id = f"obs-{canonical_hash({'execution': record.execution_id})[:16]}"
        if observation_id in known_observations:
            continue
        spec = specs_by_name.get(record.call.name)
        capabilities = (
            spec.evidence_capabilities if spec is not None
            else derive_evidence_capabilities(record.effect, record.result_authority)
        )
        fact = facts_by_execution.get(record.execution_id)
        refs = _observation_refs(record, live_graph)
        # One result can prove several different things: a directory listing both
        # names artifacts (READ_ARTIFACT) and reports that its scope was covered
        # (ENUMERATE_SCOPE). Emitting only the "strongest" capability would make
        # enumeration obligations permanently unclosable, so every capability a
        # live obligation actually asks for becomes its own evidence record.
        required_now = {
            name
            for obligation in compilation.plan.obligations if not obligation.retired
            for name in obligation.required_capabilities
        }
        relevant = tuple(sorted(
            (item for item in capabilities if item.value in required_now),
            key=lambda item: (
                _CAPABILITY_PRIORITY.index(item)
                if item in _CAPABILITY_PRIORITY else len(_CAPABILITY_PRIORITY)
            ),
        ))
        validation = EvidenceIntentValidation()
        intent = EvidenceIntent.from_data(record.call.evidence_intent)
        if intent is not None:
            validation = validate_evidence_intent(
                intent=intent, plan=compilation.plan,
                tool_capabilities=capabilities, graph=live_graph,
            )
        auto_attributed = tuple(dict.fromkeys(
            obligation.obligation_id
            for obligation in compilation.plan.obligations
            if not obligation.retired
            and obligation.required_capabilities.intersection(
                item.value for item in relevant
            )
        ))
        attributed = tuple(dict.fromkeys(
            auto_attributed + validation.accepted_obligation_ids
        ))
        pending.append((PROOF_OBSERVATION_RECORDED, {
            "observation_id": observation_id,
            "execution_id": record.execution_id,
            "capability": (
                relevant[0].value if relevant
                else next(
                    (item.value for item in _CAPABILITY_PRIORITY
                     if item in capabilities), "",
                )
            ),
            "attributed": bool(attributed),
            "intent_rejections": list(validation.reason_codes),
        }))
        if not attributed or not relevant:
            # AG-5: an unattributed observation only extends the graph.
            continue
        coverage_facts = _observation_coverage(record)
        pending.extend(_scope_narrowing_events(
            compilation=compilation, attributed=attributed,
            coverage=coverage_facts, already=set(current.narrowed_scopes),
        ))
        for capability in relevant:
            scoped_ids = tuple(
                obligation.obligation_id
                for obligation in compilation.plan.obligations
                if not obligation.retired
                and capability.value in obligation.required_capabilities
                and obligation.obligation_id in set(attributed)
            )
            if not scoped_ids:
                continue
            evidence = EvidenceRecord(
                evidence_id=(
                    "evd-"
                    + canonical_hash({
                        "execution": record.execution_id,
                        "capability": capability.value,
                    })[:16]
                ),
                obligation_ids=scoped_ids,
                capability=capability,
                artifact_refs=refs,
                scope=_observation_scope(record, refs),
                coverage=coverage_facts,
                observation=_observation_facts(fact, record, capability),
                binding_source=(
                    "explicit" if validation.accepted else "auto_attributed"
                ),
            )
            pending.append((EVIDENCE_RECORDED, {"record": evidence.to_data()}))

    if not pending:
        return ()

    # --- Coverage is evaluated on the post-append state, purely ---
    projected = replay_proof_projection(tuple(events) + tuple(
        RuntimeEvent(
            event_id=f"pending-{index}", task_id=task_spec.task_id,
            sequence=10_000 + index, event_type=event_type, payload=payload,
        )
        for index, (event_type, payload) in enumerate(pending, start=1)
    ))
    pending.append((PROOF_COVERAGE_EVALUATED, projected.summary(mode=mode)))
    return tuple(pending)


def _scope_narrowing_events(
    *,
    compilation: ProofCompilation,
    attributed: Sequence[str],
    coverage: Mapping[str, Any],
    already: set[str],
) -> list[tuple[str, Mapping[str, Any]]]:
    """Record when an observation covers less than the contract asked for.

    AG-13 requires the fact to be auditable: an inquiry that scanned only part of
    its scope must say so, and that statement must not quietly become "the whole
    scope satisfies this".
    """

    events: list[tuple[str, Mapping[str, Any]]] = []
    attributed_ids = set(attributed)
    for obligation in compilation.plan.obligations:
        if obligation.obligation_id not in attributed_ids:
            continue
        requires_complete = bool(
            obligation.scope.get("require_complete")
            or obligation.expected.get("require_complete")
        )
        if not requires_complete or coverage.get("complete") is True:
            continue
        if obligation.obligation_id in already:
            continue
        already.add(obligation.obligation_id)
        events.append((PROOF_SCOPE_NARROWED, {
            "obligation_id": obligation.obligation_id,
            "observed_complete": False,
            "declared_member_count": obligation.scope.get("member_count"),
            "observed_member_count": coverage.get("observed_count"),
            "source": "observation",
        }))
    return events


def _observation_refs(
    record: ToolExecutionRecord, graph: ArtifactGraphProjection,
) -> tuple[ArtifactRef, ...]:
    """Artifact references this execution can justify, from structured data only."""

    data = record.result.data if record.result is not None else None
    if not isinstance(data, Mapping):
        return ()
    mutation_ids = data.get("mutation_ids")
    if not isinstance(mutation_ids, (list, tuple)):
        single = data.get("mutation_id")
        mutation_ids = [single] if isinstance(single, str) else []
    wanted = {str(item) for item in mutation_ids if isinstance(item, str)}
    if not wanted:
        return ()
    references: list[ArtifactRef] = []
    for node in graph.nodes:
        if str(node.attributes.get("mutation_id", "")) in wanted:
            references.append(node.ref())
    return tuple(references)


def _observation_scope(
    record: ToolExecutionRecord, refs: Sequence[ArtifactRef],
) -> dict[str, Any]:
    scope: dict[str, Any] = {"execution_id": record.execution_id}
    if refs:
        scope["locators"] = sorted({item.locator for item in refs})
    return scope


def _observation_coverage(record: ToolExecutionRecord) -> dict[str, Any]:
    """Producer-declared completeness, when the tool reported it.

    Falls back to the tool's own truncation flag: a truncated listing or search
    did not cover its scope, so it must never certify absence (AG-13).
    """

    data = record.result.data if record.result is not None else None
    if isinstance(data, Mapping):
        coverage = data.get("coverage")
        if isinstance(coverage, Mapping):
            return dict(coverage)
    if record.result is not None:
        return {"complete": not record.result.truncated}
    return {}


def _observation_facts(
    fact: Any, record: ToolExecutionRecord, capability: EvidenceCapability,
) -> dict[str, Any]:
    """Structured facts the coverage evaluator is allowed to read."""

    observation: dict[str, Any] = {}
    if fact is not None:
        observation["effect_status"] = fact.effect_status.value
    data = record.result.data if record.result is not None else None
    if isinstance(data, Mapping):
        exit_code = data.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            observation["exit_code"] = exit_code
        if data.get("conflicting") is True:
            observation["conflicting"] = True
        if isinstance(data.get("relation_source"), str):
            observation["relation_source"] = data["relation_source"]
    return observation
