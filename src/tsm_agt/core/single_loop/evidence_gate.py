"""Generic evidence-gate evaluation for single-loop completion."""

from __future__ import annotations

from collections.abc import Sequence

from tsm_agt.ports.evidence import (
    EvidenceFact,
    EvidenceRequirement,
    GateDecision,
    GateResolution,
    RecoveryAction,
    TaskContract,
)


def evaluate_evidence_contract(
    contract: TaskContract | None,
    facts: Sequence[EvidenceFact],
    capabilities: frozenset[str],
) -> GateDecision:
    """Evaluate only explicitly accepted ACTIVE requirements.

    A continuation is permitted only when every missing requirement has its
    exact capability available. This prevents one available tool from masking
    another requirement that cannot be satisfied in the current environment.
    """
    if contract is None or not contract.is_mandatory or not contract.evidence_requirements:
        return GateDecision(GateResolution.PASS, reason_code="NO_MANDATORY_CONTRACT")
    missing = tuple(
        requirement
        for requirement in contract.evidence_requirements
        if not _satisfied(requirement, facts)
    )
    if not missing:
        return GateDecision(GateResolution.PASS, reason_code="REQUIREMENTS_SATISFIED")
    required_capabilities = tuple(sorted({item.kind for item in missing}))
    if all(requirement.kind in capabilities for requirement in missing):
        return GateDecision(
            GateResolution.CONTINUE,
            tuple(item.requirement_id for item in missing),
            required_capabilities,
            "EVIDENCE_REQUIREMENTS_MISSING",
            "User-accepted evidence requirements are incomplete.",
        )
    return GateDecision(
        GateResolution.BLOCKED,
        tuple(item.requirement_id for item in missing),
        required_capabilities,
        "EVIDENCE_CAPABILITY_UNAVAILABLE",
        "No visible provider declares every capability needed by the missing requirements.",
        (
            RecoveryAction("provide_source", "提供来源或启用所需能力"),
            RecoveryAction("edit_contract", "修改合同"),
            RecoveryAction("clear_contract", "按普通任务继续"),
        ),
    )


def _satisfied(requirement: EvidenceRequirement, facts: Sequence[EvidenceFact]) -> bool:
    matching = [
        fact for fact in facts
        if fact.kind == requirement.kind
        and fact.complete
        and _matches_predicates(fact, requirement)
    ]
    if requirement.distinct_by:
        values = {
            str(fact.attributes[requirement.distinct_by])
            for fact in matching
            if requirement.distinct_by in fact.attributes
        }
        return len(values) >= requirement.minimum_count
    return len(matching) >= requirement.minimum_count


def _matches_predicates(
    fact: EvidenceFact,
    requirement: EvidenceRequirement,
) -> bool:
    return all(fact.attributes.get(key) == value for key, value in requirement.predicates.items())
