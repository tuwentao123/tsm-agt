"""P0 tests for the task proof closure domain model.

Covers 《任务证明闭环实施SPEC.md》 §3 and §4.1. Deliberately runs without a
Kernel: the whole point of P0 is that the domain model is a pure, inspectable,
independently testable layer that changes no completion behaviour.
"""

from __future__ import annotations

import unittest

from tsm_agt.core.artifact_graph import (
    ArtifactGraphProjection, ArtifactKind, ArtifactNode, ArtifactRef,
    ArtifactRelation, command_version, content_version, edge_version,
    external_version,
)
from tsm_agt.core.evidence_coverage import (
    ESCALATED_GAP_KINDS, CoverageStatus, ProofEnforcementMode, EvidenceRecord, ProofPlanSnapshot, can_succeed,
    evaluate_coverage,
)
from tsm_agt.core.proof_obligation import (
    MAX_OBLIGATIONS_PER_SOURCE, MAX_PROOF_OBLIGATIONS, ObligationPredicate,
    make_obligation, normalize_text, truncate_obligations,
)
from tsm_agt.core.proof_projection import (
    PROOF_COVERAGE_EVALUATED, PROOF_PLAN_PUBLISHED, PROOF_SCOPE_NARROWED,
    replay_proof_projection,
)
from tsm_agt.core.task import TaskState
from tsm_agt.core.task_spec import (
    ProofPlanCompiler, REQUIRED_EFFECT_CRITERION_ID, TaskAcceptanceCriterion,
    TaskCriterionKind, TaskOutcomeCompletionPolicy, TaskOutcomeKind,
    TaskOutcomeSnapshot, TaskSpecSnapshot,
)
from tsm_agt.ports import (
    CompletionReadinessAction, EvidenceCapability, JudgeVerdict, RuntimeEvent, ToolEffect, ToolResultAuthority, ToolRisk,
    ToolSpec,
)


def tool(
    name: str,
    effect: ToolEffect,
    authority: ToolResultAuthority,
    **extra: object,
) -> ToolSpec:
    return ToolSpec(
        name=name, description="test tool",
        parameters={"type": "object", "properties": {}},
        risk=ToolRisk.R0, effect=effect, result_authority=authority, **extra,
    )


def obligation(
    *,
    contract_ref: str = "c1",
    predicate: ObligationPredicate = ObligationPredicate.ARTIFACT_CONTENT,
    text: str = "the report exists",
    selector: dict | None = None,
    required: bool = True,
    capabilities: tuple[str, ...] = (),
    scope: dict | None = None,
    expected: dict | None = None,
):
    return make_obligation(
        contract_ref=contract_ref, predicate=predicate, requirement_text=text,
        selector=selector or {"locator": "src/a.py"}, required=required,
        required_capabilities=capabilities, scope=scope or {},
        expected=expected or {},
    )


def graph_with(*nodes: ArtifactNode) -> ArtifactGraphProjection:
    projection = ArtifactGraphProjection()
    for node in nodes:
        projection = projection.observe_node(node)
    return projection


def file_node(artifact_id: str = "artifact-1", content: str = "v1") -> ArtifactNode:
    return ArtifactNode(
        artifact_id=artifact_id, kind=ArtifactKind.FILE, locator="src/a.py",
        version=content_version(content),
    )


class ObligationIdentityTest(unittest.TestCase):
    """AG-10: deterministic, content-sensitive, version-free identity."""

    def test_identity_is_deterministic(self) -> None:
        first = obligation()
        second = obligation()
        self.assertEqual(first.obligation_id, second.obligation_id)

    def test_changed_requirement_changes_the_id(self) -> None:
        before = obligation(text="the report must not show the banner")
        after = obligation(text="the report must show the banner")
        self.assertNotEqual(before.obligation_id, after.obligation_id)

    def test_version_bearing_selector_does_not_change_the_id(self) -> None:
        without = obligation(selector={"locator": "src/a.py"})
        with_version = obligation(selector={
            "locator": "src/a.py", "version": "sha256:deadbeef",
            "artifact_id": "artifact-1", "content_hash": "abc", "mtime": "123",
        })
        self.assertEqual(without.obligation_id, with_version.obligation_id)
        self.assertNotIn("version", with_version.subject_selector)

    def test_normalization_is_stable_across_cosmetic_variants(self) -> None:
        base = normalize_text("Show   the Banner.")
        self.assertEqual(base, normalize_text("  show the banner  "))
        self.assertEqual(base, normalize_text("Show the banner\u3002"))
        self.assertEqual(base, normalize_text('"Show the Banner."'))
        self.assertEqual(base, normalize_text("\u201cShow the Banner\u3002\u201d"))

    def test_retirement_keeps_identity(self) -> None:
        from tsm_agt.core.proof_obligation import retire_obligation

        original = obligation()
        retired = retire_obligation(original)
        self.assertEqual(original.obligation_id, retired.obligation_id)
        self.assertTrue(retired.retired)


class ObligationBoundTest(unittest.TestCase):
    """SPEC D1: the bound may drop advisory work, never required work silently."""

    def test_under_the_bound_keeps_everything(self) -> None:
        result = truncate_obligations(tuple(obligation(contract_ref=f"c{i}") for i in range(3)))
        self.assertFalse(result.truncated)
        self.assertFalse(result.required_overflow)

    def test_over_the_bound_reports_required_overflow(self) -> None:
        ordered = tuple(
            obligation(contract_ref=f"required-{i}") for i in range(MAX_PROOF_OBLIGATIONS + 1)
        )
        result = truncate_obligations(ordered)
        self.assertTrue(result.truncated)
        self.assertTrue(result.required_overflow)

    def test_dropping_only_advisory_work_is_not_an_overflow(self) -> None:
        required = tuple(
            obligation(contract_ref=f"required-{i}") for i in range(MAX_PROOF_OBLIGATIONS)
        )
        advisory = obligation(contract_ref="advisory", required=False)
        result = truncate_obligations(required + (advisory,))
        self.assertTrue(result.truncated)
        self.assertFalse(result.required_overflow)

    def test_per_source_bound_is_declared(self) -> None:
        self.assertEqual(MAX_OBLIGATIONS_PER_SOURCE, 4)


class ArtifactVersionTest(unittest.TestCase):
    """§3.1: every kind defines what a version change means."""

    def test_content_change_produces_a_new_version(self) -> None:
        self.assertNotEqual(content_version("v1"), content_version("v2"))

    def test_command_version_ignores_run_results(self) -> None:
        self.assertEqual(
            command_version(("/usr/bin/python3", "-m", "pytest")),
            command_version(("python3", "-m", "pytest")),
        )
        self.assertNotEqual(
            command_version(("python3", "-m", "pytest")),
            command_version(("python3", "-m", "unittest")),
        )

    def test_external_resource_without_stable_version_is_unknown(self) -> None:
        self.assertEqual(external_version(None), "unknown")
        self.assertEqual(external_version("  "), "unknown")
        self.assertEqual(external_version("etag-1"), "etag-1")

    def test_edge_version_changes_when_either_endpoint_changes(self) -> None:
        relation = ArtifactRelation.VERIFIES
        base = edge_version(source_version="a1", relation=relation, target_version="b1")
        other_source = edge_version(
            source_version="a2", relation=relation, target_version="b1",
        )
        other_target = edge_version(
            source_version="a1", relation=relation, target_version="b2",
        )
        self.assertNotEqual(base, other_source)
        self.assertNotEqual(base, other_target)

    def test_projection_reports_stale_after_a_version_change(self) -> None:
        first = graph_with(file_node(content="v1"))
        reference = first.node("artifact-1").ref()
        self.assertTrue(first.is_current(reference))

        second = first.observe_node(file_node(content="v2"))
        self.assertFalse(second.is_current(reference))
        self.assertTrue(second.is_current(second.node("artifact-1").ref()))

    def test_edge_is_only_built_from_observed_endpoints(self) -> None:
        projection = graph_with(file_node())
        self.assertIsNone(projection.make_edge(
            source_artifact_id="artifact-1", relation=ArtifactRelation.VERIFIES,
            target_artifact_id="missing",
        ))
        other = projection.observe_node(ArtifactNode(
            "artifact-2", ArtifactKind.TEST, "tests/test_a.py", "t1",
        ))
        edge = other.make_edge(
            source_artifact_id="artifact-2", relation=ArtifactRelation.VERIFIES,
            target_artifact_id="artifact-1",
        )
        self.assertIsNotNone(edge)
        self.assertTrue(edge.edge_id.startswith("edg-"))


class EvidenceCapabilityTest(unittest.TestCase):
    """§3.3: capabilities are derived, never a second adapter-maintained list."""

    def test_derivation_table(self) -> None:
        cases = {
            (ToolEffect.OBSERVE, ToolResultAuthority.WORKSPACE_FACT): "READ_ARTIFACT",
            (ToolEffect.MUTATE, ToolResultAuthority.MUTATION_FACT): "MUTATE_ARTIFACT",
            (ToolEffect.OBSERVE, ToolResultAuthority.PROCESS_FACT): "OBSERVE_EFFECT",
            (ToolEffect.EXECUTE, ToolResultAuthority.PROCESS_FACT): "EXECUTE_VERIFICATION",
            (ToolEffect.OBSERVE, ToolResultAuthority.RUNTIME_FACT): "OBSERVE_RUNTIME",
            (ToolEffect.OBSERVE, ToolResultAuthority.EXTERNAL_SERVICE): "OBSERVE_EXTERNAL",
        }
        for (effect, authority), expected in cases.items():
            with self.subTest(effect=effect, authority=authority):
                spec = tool("core.x", effect, authority)
                self.assertEqual(
                    {item.value for item in spec.evidence_capabilities}, {expected},
                )

    def test_existing_tools_need_no_change(self) -> None:
        # The whole P0 migration story: declaring only the two existing fields
        # is enough, so no adapter has to change.
        spec = tool("core.read", ToolEffect.OBSERVE, ToolResultAuthority.WORKSPACE_FACT)
        self.assertEqual(spec.extra_evidence_capabilities, frozenset())
        self.assertIn(EvidenceCapability.READ_ARTIFACT, spec.evidence_capabilities)

    def test_explicit_declaration_adds_non_derivable_capabilities(self) -> None:
        spec = tool(
            "core.enumerate", ToolEffect.OBSERVE, ToolResultAuthority.WORKSPACE_FACT,
            extra_evidence_capabilities=frozenset({
                EvidenceCapability.ENUMERATE_SCOPE, EvidenceCapability.OBSERVE_RELATION,
            }),
        )
        self.assertIn(EvidenceCapability.ENUMERATE_SCOPE, spec.evidence_capabilities)
        self.assertIn(EvidenceCapability.READ_ARTIFACT, spec.evidence_capabilities)

    def test_declaring_a_derivable_capability_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "derived from effect"):
            tool(
                "core.bad", ToolEffect.OBSERVE, ToolResultAuthority.WORKSPACE_FACT,
                extra_evidence_capabilities=frozenset({
                    EvidenceCapability.READ_ARTIFACT,
                }),
            )

    def test_explicit_capability_requires_an_observable_pair(self) -> None:
        with self.assertRaisesRegex(ValueError, "observable"):
            tool(
                "core.bad", ToolEffect.UNSPECIFIED, ToolResultAuthority.NONE,
                extra_evidence_capabilities=frozenset({
                    EvidenceCapability.ENUMERATE_SCOPE,
                }),
            )


class CoverageSufficiencyTest(unittest.TestCase):
    """§3.7 / AG-12 / AG-13 / AG-14: strict sufficiency, no false success."""

    def coverage_of(self, obligation_item, records, graph=None):
        results = evaluate_coverage(
            obligations=(obligation_item,), evidence=tuple(records),
            graph=graph or ArtifactGraphProjection(),
        )
        self.assertEqual(len(results), 1)
        return results[0]

    def test_current_version_read_covers_content(self) -> None:
        graph = graph_with(file_node())
        item = obligation(capabilities=("READ_ARTIFACT",))
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.READ_ARTIFACT,
            artifact_refs=(graph.node("artifact-1").ref(),),
        )
        coverage = self.coverage_of(item, (record,), graph)
        self.assertIs(coverage.status, CoverageStatus.COVERED)

    def test_version_change_makes_the_evidence_stale(self) -> None:
        old_graph = graph_with(file_node(content="v1"))
        reference = ArtifactRef("artifact-1", content_version("v1"), "src/a.py")
        item = obligation(capabilities=("READ_ARTIFACT",))
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.READ_ARTIFACT,
            artifact_refs=(reference,),
        )
        new_graph = old_graph.observe_node(file_node(content="v2"))
        coverage = self.coverage_of(item, (record,), new_graph)
        self.assertIs(coverage.status, CoverageStatus.STALE)
        self.assertEqual(coverage.reason_code, "ARTIFACT_VERSION_CHANGED")

    def test_narrow_evidence_cannot_close_a_wide_obligation(self) -> None:
        graph = graph_with(file_node())
        item = obligation(
            capabilities=("READ_ARTIFACT",),
            scope={"require_complete": True, "member_count": 3},
        )
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.READ_ARTIFACT,
            artifact_refs=(graph.node("artifact-1").ref(),),
            coverage={"complete": False, "observed_count": 1},
        )
        coverage = self.coverage_of(item, (record,), graph)
        self.assertIsNot(coverage.status, CoverageStatus.COVERED)
        self.assertEqual(
            coverage.reason_code, "NARROW_EVIDENCE_CANNOT_CLOSE_WIDE_OBLIGATION",
        )

    def test_wide_obligation_is_covered_by_a_complete_enumeration(self) -> None:
        graph = graph_with(file_node())
        item = obligation(
            capabilities=("READ_ARTIFACT",),
            scope={"require_complete": True, "member_count": 3},
        )
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.READ_ARTIFACT,
            artifact_refs=(graph.node("artifact-1").ref(),),
            coverage={"complete": True, "observed_count": 3},
        )
        self.assertIs(
            self.coverage_of(item, (record,), graph).status, CoverageStatus.COVERED,
        )

    def test_zero_result_without_an_observed_scope_root_is_blocked(self) -> None:
        item = obligation(
            predicate=ObligationPredicate.QUERY_CARDINALITY,
            selector={"locator": "src"}, capabilities=("ENUMERATE_SCOPE",),
            scope={"root_artifact_id": "never-observed"}, expected={"count": 0},
        )
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.ENUMERATE_SCOPE,
            coverage={"complete": True, "count": 0},
        )
        coverage = self.coverage_of(item, (record,), ArtifactGraphProjection())
        self.assertIs(coverage.status, CoverageStatus.BLOCKED)
        self.assertEqual(coverage.reason_code, "SCOPE_ROOT_NOT_OBSERVED")

    def test_incomplete_enumeration_cannot_certify_absence(self) -> None:
        graph = graph_with(ArtifactNode(
            "artifact-root", ArtifactKind.FILE, "src", "r1",
        ))
        item = obligation(
            predicate=ObligationPredicate.QUERY_CARDINALITY,
            selector={"locator": "src"}, capabilities=("ENUMERATE_SCOPE",),
            scope={"root_artifact_id": "artifact-root"}, expected={"count": 0},
        )
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.ENUMERATE_SCOPE,
            coverage={"complete": False, "count": 0},
        )
        self.assertIs(
            self.coverage_of(item, (record,), graph).status, CoverageStatus.BLOCKED,
        )

    def test_complete_enumeration_with_matching_count_is_covered(self) -> None:
        graph = graph_with(ArtifactNode(
            "artifact-root", ArtifactKind.FILE, "src", "r1",
        ))
        item = obligation(
            predicate=ObligationPredicate.QUERY_CARDINALITY,
            selector={"locator": "src"}, capabilities=("ENUMERATE_SCOPE",),
            scope={"root_artifact_id": "artifact-root"}, expected={"count": 0},
        )
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.ENUMERATE_SCOPE,
            coverage={"complete": True, "count": 0},
        )
        self.assertIs(
            self.coverage_of(item, (record,), graph).status, CoverageStatus.COVERED,
        )

    def test_effect_that_did_not_happen_is_missing_not_failed(self) -> None:
        item = obligation(
            predicate=ObligationPredicate.EFFECT_OBSERVED,
            capabilities=("MUTATE_ARTIFACT",),
        )
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.MUTATE_ARTIFACT,
            observation={"effect_status": "unknown"},
        )
        coverage = self.coverage_of(item, (record,))
        self.assertIs(coverage.status, CoverageStatus.MISSING)
        self.assertEqual(coverage.reason_code, "EFFECT_NOT_RECORDED")

    def test_failed_command_is_missing_not_failed(self) -> None:
        item = obligation(
            predicate=ObligationPredicate.COMMAND_VERIFIED,
            capabilities=("EXECUTE_VERIFICATION",),
        )
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.EXECUTE_VERIFICATION,
            observation={"exit_code": 1},
        )
        coverage = self.coverage_of(item, (record,))
        self.assertIs(coverage.status, CoverageStatus.MISSING)

    def test_judge_affirmation_without_evidence_is_blocked(self) -> None:
        item = obligation(
            predicate=ObligationPredicate.SEMANTIC_REVIEW,
            selector={"criterion": "rubric"},
        )
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.READ_ARTIFACT,
            observation={"verdict": "SATISFIED"},
        )
        coverage = self.coverage_of(item, (record,))
        self.assertIs(coverage.status, CoverageStatus.BLOCKED)
        self.assertEqual(coverage.reason_code, "JUDGE_AFFIRMED_WITHOUT_EVIDENCE")

    def test_relation_claim_needs_a_durable_source(self) -> None:
        item = obligation(
            predicate=ObligationPredicate.RELATION_EXISTS,
            capabilities=("OBSERVE_RELATION",),
        )
        weak = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.OBSERVE_RELATION,
            observation={},
        )
        self.assertIs(self.coverage_of(item, (weak,)).status, CoverageStatus.BLOCKED)
        strong = EvidenceRecord(
            "ev-2", (item.obligation_id,), EvidenceCapability.OBSERVE_RELATION,
            observation={"relation_source": "parse"},
        )
        self.assertIs(self.coverage_of(item, (strong,)).status, CoverageStatus.COVERED)

    def test_conflicting_evidence_wins(self) -> None:
        graph = graph_with(file_node())
        item = obligation(capabilities=("READ_ARTIFACT",))
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.READ_ARTIFACT,
            artifact_refs=(graph.node("artifact-1").ref(),),
            observation={"conflicting": True},
        )
        self.assertIs(
            self.coverage_of(item, (record,), graph).status,
            CoverageStatus.CONFLICTING,
        )

    def test_one_observation_can_attribute_to_many_obligations(self) -> None:
        graph = graph_with(file_node())
        first = obligation(contract_ref="c1", capabilities=("READ_ARTIFACT",))
        second = obligation(contract_ref="c2", capabilities=("READ_ARTIFACT",))
        record = EvidenceRecord(
            "ev-1", (first.obligation_id, second.obligation_id),
            EvidenceCapability.READ_ARTIFACT,
            artifact_refs=(graph.node("artifact-1").ref(),),
        )
        results = evaluate_coverage(
            obligations=(first, second), evidence=(record,), graph=graph,
        )
        self.assertEqual([item.status for item in results], [CoverageStatus.COVERED] * 2)

    def test_retired_obligation_is_not_evaluated(self) -> None:
        from tsm_agt.core.proof_obligation import retire_obligation

        item = retire_obligation(obligation())
        self.assertEqual(
            evaluate_coverage(
                obligations=(item,), evidence=(), graph=ArtifactGraphProjection(),
            ),
            (),
        )

    def test_can_succeed_requires_every_obligation_and_deterministic_pass(self) -> None:
        graph = graph_with(file_node())
        item = obligation(capabilities=("READ_ARTIFACT",))
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.READ_ARTIFACT,
            artifact_refs=(graph.node("artifact-1").ref(),),
        )
        coverages = evaluate_coverage(
            obligations=(item,), evidence=(record,), graph=graph,
        )
        self.assertTrue(can_succeed(coverages=coverages, deterministic_passed=True))
        self.assertFalse(can_succeed(coverages=coverages, deterministic_passed=False))
        self.assertFalse(can_succeed(
            coverages=(), deterministic_passed=True, semantic_review_passed=False,
        ))


class ProofPlanCompilerTest(unittest.TestCase):
    """§4.1 / AG-15: structured-field compilation, deterministic and prose-free."""

    def spec(self, criteria, outcomes=(), goal="do the work") -> TaskSpecSnapshot:
        return TaskSpecSnapshot(
            task_id="task-1", revision=1, goal=goal, scope=(), constraints=(),
            acceptance_criteria=tuple(criteria), outcomes=tuple(outcomes),
        )

    def predicates(self, compilation) -> set[str]:
        return {item.predicate.value for item in compilation.plan.obligations}

    def test_compilation_is_deterministic(self) -> None:
        spec = self.spec([
            TaskAcceptanceCriterion(
                "c1", "read the report", TaskCriterionKind.EVIDENCE_REFERENCE, "event:7",
            ),
        ])
        first = ProofPlanCompiler.compile(spec)
        second = ProofPlanCompiler.compile(spec)
        self.assertEqual(
            [item.obligation_id for item in first.plan.obligations],
            [item.obligation_id for item in second.plan.obligations],
        )
        self.assertEqual(first.plan.plan_hash, second.plan.plan_hash)

    def test_description_changes_identity_but_not_the_mapping(self) -> None:
        def compile_with(description: str):
            return ProofPlanCompiler.compile(self.spec([
                TaskAcceptanceCriterion(
                    "c1", description, TaskCriterionKind.EVIDENCE_REFERENCE, "event:7",
                ),
            ]))

        before = compile_with("read the report")
        after = compile_with("read the report before the demo")
        self.assertNotEqual(
            before.plan.obligations[0].obligation_id,
            after.plan.obligations[0].obligation_id,
        )
        # AG-15: prose never selects a predicate or a selector.
        self.assertEqual(
            before.plan.obligations[0].predicate, after.plan.obligations[0].predicate,
        )
        self.assertEqual(
            dict(before.plan.obligations[0].subject_selector),
            dict(after.plan.obligations[0].subject_selector),
        )

    def test_prose_never_produces_an_extra_required_obligation(self) -> None:
        plain = ProofPlanCompiler.compile(self.spec([
            TaskAcceptanceCriterion(
                "c1", "read it", TaskCriterionKind.EVIDENCE_REFERENCE, "event:7",
            ),
        ]))
        prose = ProofPlanCompiler.compile(self.spec([
            TaskAcceptanceCriterion(
                "c1",
                "read it, then also update the front-end template and the iOS "
                "client, and make sure the Spring controller agrees",
                TaskCriterionKind.EVIDENCE_REFERENCE, "event:7",
            ),
        ]))
        self.assertEqual(len(plain.required), len(prose.required))
        self.assertEqual(self.predicates(plain), self.predicates(prose))

    def test_reference_prefix_decides_the_predicate(self) -> None:
        compilation = ProofPlanCompiler.compile(self.spec([
            TaskAcceptanceCriterion(
                "c-event", "event", TaskCriterionKind.EVIDENCE_REFERENCE, "event:3",
            ),
            TaskAcceptanceCriterion(
                "c-mutation", "mutation", TaskCriterionKind.EVIDENCE_REFERENCE,
                "mutation:9",
            ),
        ]))
        self.assertIn("ARTIFACT_EXISTS", self.predicates(compilation))
        self.assertIn("EFFECT_OBSERVED", self.predicates(compilation))

    def test_tool_call_reference_needs_its_recorded_capability(self) -> None:
        criterion = TaskAcceptanceCriterion(
            "c-tool", "tool call", TaskCriterionKind.EVIDENCE_REFERENCE, "tool_call:42",
        )
        unresolved = ProofPlanCompiler.compile(self.spec([criterion]))
        self.assertEqual(unresolved.required, ())
        self.assertEqual(len(unresolved.advisories), 1)
        self.assertEqual(
            unresolved.advisories[0].expected["advisory_reason"],
            "PENDING_REFERENCE_CAPABILITY",
        )

        resolved = ProofPlanCompiler.compile(
            self.spec([criterion]),
            reference_capabilities={"tool_call:42": "EXECUTE_VERIFICATION"},
        )
        self.assertEqual(self.predicates(resolved), {"COMMAND_VERIFIED"})

    def test_unknown_reference_prefix_is_rejected(self) -> None:
        compilation = ProofPlanCompiler.compile(self.spec([
            TaskAcceptanceCriterion(
                "c-turn", "copied", TaskCriterionKind.EVIDENCE_REFERENCE, "turn-abc",
            ),
        ]))
        self.assertEqual(compilation.required, ())
        self.assertEqual(
            [item.reason for item in compilation.rejected],
            ["NOT_STRUCTURALLY_KNOWABLE"],
        )

    def test_only_rubric_and_goal_alignment_produce_semantic_review(self) -> None:
        compilation = ProofPlanCompiler.compile(self.spec([
            TaskAcceptanceCriterion("c1", "rubric", TaskCriterionKind.RUBRIC),
            TaskAcceptanceCriterion(
                "c2", "alignment", TaskCriterionKind.GOAL_ALIGNMENT,
            ),
            TaskAcceptanceCriterion(
                "c3", "event", TaskCriterionKind.EVIDENCE_REFERENCE, "event:1",
            ),
        ]))
        semantic = [
            item for item in compilation.plan.obligations
            if item.predicate is ObligationPredicate.SEMANTIC_REVIEW
        ]
        self.assertEqual(len(semantic), 2)
        self.assertTrue(all(item.required for item in semantic))

    def test_required_effect_criterion_reads_outcome_effects(self) -> None:
        outcome = TaskOutcomeSnapshot(
            "o1", "deliver the change", TaskOutcomeKind.WORKSPACE_DELIVERY,
            (ToolEffect.MUTATE,),
        )
        compilation = ProofPlanCompiler.compile(self.spec(
            [TaskAcceptanceCriterion(
                "required-effect-delivery", "delivery",
                TaskCriterionKind.REQUIRED_EFFECT,
            )],
            [outcome],
        ))
        required = compilation.required
        self.assertEqual(len(required), 1)
        self.assertEqual(required[0].predicate, ObligationPredicate.EFFECT_OBSERVED)
        self.assertEqual(dict(required[0].subject_selector), {"effect": "mutate"})
        self.assertEqual(required[0].contract_ref, REQUIRED_EFFECT_CRITERION_ID)

    def test_workspace_delivery_does_not_duplicate_the_required_effect(self) -> None:
        outcome = TaskOutcomeSnapshot(
            "o1", "deliver the change", TaskOutcomeKind.WORKSPACE_DELIVERY,
            (ToolEffect.MUTATE,),
        )
        compilation = ProofPlanCompiler.compile(self.spec(
            [TaskAcceptanceCriterion(
                "required-effect-delivery", "delivery",
                TaskCriterionKind.REQUIRED_EFFECT,
            )],
            [outcome],
        ))
        self.assertEqual(len(compilation.required), 1)

    def test_artifact_delivery_upgrades_only_on_a_structured_path(self) -> None:
        structured = TaskOutcomeSnapshot(
            "o1", "write the file", TaskOutcomeKind.ARTIFACT_DELIVERY,
            (ToolEffect.MUTATE,),
            completion_policy=TaskOutcomeCompletionPolicy.ATOMIC_ACTION,
            atomic_action={
                "tool_name": "core.write_file",
                "arguments": {"path": "src/a.py"},
            },
        )
        upgraded = ProofPlanCompiler.compile(self.spec(
            [TaskAcceptanceCriterion(
                "c1", "integrity", TaskCriterionKind.WORKSPACE_INTEGRITY,
            )],
            [structured],
        ))
        self.assertIn("ARTIFACT_CONTENT", self.predicates(upgraded))

        opaque = TaskOutcomeSnapshot(
            "o2", "write something", TaskOutcomeKind.ARTIFACT_DELIVERY,
            (ToolEffect.MUTATE,),
        )
        plain = ProofPlanCompiler.compile(self.spec(
            [TaskAcceptanceCriterion(
                "c1", "integrity", TaskCriterionKind.WORKSPACE_INTEGRITY,
            )],
            [opaque],
        ))
        obligations = [
            item for item in plain.plan.obligations
            if item.contract_ref == "o2"
        ]
        self.assertEqual(
            [item.predicate for item in obligations],
            [ObligationPredicate.ARTIFACT_EXISTS],
        )

    def test_answer_outcome_invents_no_artifact_obligation(self) -> None:
        answer = TaskOutcomeSnapshot(
            "o1", "answer the question", TaskOutcomeKind.ANSWER, (),
        )
        compilation = ProofPlanCompiler.compile(self.spec(
            [TaskAcceptanceCriterion("c1", "alignment", TaskCriterionKind.GOAL_ALIGNMENT)],
            [answer],
        ))
        self.assertEqual(self.predicates(compilation), {"SEMANTIC_REVIEW"})

    def test_unfulfillable_required_stays_required(self) -> None:
        compilation = ProofPlanCompiler.compile(
            self.spec([TaskAcceptanceCriterion(
                "c-verify", "verify", TaskCriterionKind.POST_MUTATION_COMMAND,
            )]),
            available_capabilities=frozenset({"READ_ARTIFACT"}),
        )
        self.assertEqual(len(compilation.unfulfillable), 1)
        self.assertTrue(compilation.unfulfillable[0].required)
        self.assertIn(compilation.unfulfillable[0].obligation_id, {
            item.obligation_id for item in compilation.plan.obligations
        })

    def test_neutrality_of_project_vocabulary(self) -> None:
        criteria = [
            TaskAcceptanceCriterion(
                "c1", "read src/main.py", TaskCriterionKind.EVIDENCE_REFERENCE, "event:1",
            ),
        ]
        renamed = [
            TaskAcceptanceCriterion(
                "c1", "read frontend/App.vue", TaskCriterionKind.EVIDENCE_REFERENCE,
                "event:1",
            ),
        ]
        first = ProofPlanCompiler.compile(self.spec(criteria))
        second = ProofPlanCompiler.compile(self.spec(renamed))
        self.assertEqual(len(first.plan.obligations), len(second.plan.obligations))
        self.assertEqual(self.predicates(first), self.predicates(second))


class ProofPlanSnapshotTest(unittest.TestCase):
    def test_plan_hash_covers_the_obligation_set(self) -> None:
        first = obligation(contract_ref="c1")
        second = obligation(contract_ref="c2")
        one = ProofPlanSnapshot.build(
            task_id="t", task_spec_revision=1, obligations=(first,),
        )
        two = ProofPlanSnapshot.build(
            task_id="t", task_spec_revision=1, obligations=(first, second),
        )
        self.assertNotEqual(one.plan_hash, two.plan_hash)

    def test_required_and_advisory_partition(self) -> None:
        plan = ProofPlanSnapshot.build(
            task_id="t", task_spec_revision=1,
            obligations=(obligation(contract_ref="c1"), obligation(
                contract_ref="c2", required=False,
            )),
        )
        self.assertEqual(len(plan.required), 1)
        self.assertEqual(len(plan.advisory), 1)


class ProofProjectionReplayTest(unittest.TestCase):
    """§3.4 / §7 P0: the proof layer is a pure replay and never breaks existing
    reconstruction."""

    def events(self, *pairs):
        return tuple(
            RuntimeEvent(f"e{i}", "t1", i, event_type, payload)
            for i, (event_type, payload) in enumerate(pairs, start=1)
        )

    def compiled(self):
        spec = TaskSpecSnapshot(
            task_id="t1", revision=1, goal="g", scope=(), constraints=(),
            acceptance_criteria=(TaskAcceptanceCriterion(
                "c1", "read the file", TaskCriterionKind.EVIDENCE_REFERENCE,
                "event:1",
            ),),
        )
        return ProofPlanCompiler.compile(spec)

    def test_replay_rebuilds_plan_graph_evidence_and_coverage(self) -> None:
        from tsm_agt.core.proof_projection import (
            ARTIFACT_OBSERVED, EVIDENCE_RECORDED, PROOF_OBSERVATION_RECORDED,
            PROOF_PLAN_PUBLISHED, replay_proof_projection,
        )

        compilation = self.compiled()
        item = compilation.plan.obligations[0]
        node = ArtifactNode("a1", ArtifactKind.FILE, "event:1", content_version("v1"))
        record = EvidenceRecord(
            "ev1", (item.obligation_id,), EvidenceCapability.READ_ARTIFACT,
            artifact_refs=(node.ref(),),
        )
        projection = replay_proof_projection(self.events(
            (PROOF_PLAN_PUBLISHED, {"snapshot": compilation.plan.to_data()}),
            (PROOF_OBSERVATION_RECORDED, {"attributed": True}),
            (ARTIFACT_OBSERVED, {"node": node.to_data()}),
            (EVIDENCE_RECORDED, {"record": record.to_data()}),
        ))
        self.assertIsNotNone(projection.plan)
        self.assertEqual(len(projection.required_coverages), 1)
        self.assertIs(
            projection.required_coverages[0].status, CoverageStatus.COVERED,
        )
        self.assertEqual(projection.auto_attribution_rate, 1.0)

    def test_empty_replay_is_not_an_error(self) -> None:
        from tsm_agt.core.proof_projection import replay_proof_projection

        projection = replay_proof_projection(())
        summary = projection.summary()
        self.assertFalse(summary["present"])
        self.assertIsNone(summary["auto_attribution_rate"])

    def test_malformed_proof_payload_never_breaks_projection(self) -> None:
        from tsm_agt.core.proof_projection import (
            ARTIFACT_OBSERVED, PROOF_PLAN_PUBLISHED, replay_proof_projection,
        )

        projection = replay_proof_projection(self.events(
            (PROOF_PLAN_PUBLISHED, {"snapshot": {"obligations": "not-a-list"}}),
            (ARTIFACT_OBSERVED, {"node": {"missing": "fields"}}),
        ))
        self.assertIsNone(projection.plan)
        self.assertEqual(projection.graph.nodes, ())

    def test_summary_exposes_counts_and_ids_only(self) -> None:
        from tsm_agt.core.proof_projection import (
            PROOF_PLAN_PUBLISHED, replay_proof_projection,
        )

        compilation = self.compiled()
        projection = replay_proof_projection(self.events(
            (PROOF_PLAN_PUBLISHED, {"snapshot": compilation.plan.to_data()}),
        ))
        summary = projection.summary()
        self.assertEqual(summary["obligations"]["required"], 1)
        self.assertEqual(summary["coverage"]["MISSING"], 1)
        self.assertEqual(
            [gap["reason_code"] for gap in summary["gaps"]], ["NO_ELIGIBLE_CAPABILITY"],
        )
        # No raw tool output and no obligation payloads leak into the summary.
        self.assertIn("mode", summary)
        self.assertNotIn("plan", summary)

    def test_truncation_and_unfulfillable_are_surfaced(self) -> None:
        from tsm_agt.core.proof_projection import (
            PROOF_OBLIGATION_UNFULFILLABLE, PROOF_PLAN_TRUNCATED,
            PROOF_PLAN_PUBLISHED, replay_proof_projection,
        )

        compilation = self.compiled()
        projection = replay_proof_projection(self.events(
            (PROOF_PLAN_PUBLISHED, {"snapshot": compilation.plan.to_data()}),
            (PROOF_PLAN_TRUNCATED, {"required_overflow": True}),
            (PROOF_OBLIGATION_UNFULFILLABLE, {"obligation_id": "obg-x"}),
        ))
        summary = projection.summary()
        self.assertTrue(summary["truncated"])
        self.assertTrue(summary["required_overflow"])
        self.assertEqual(summary["unfulfillable"], ["obg-x"])


class DeriveProofEventsTest(unittest.TestCase):
    """§7 P0: emitting proof events is a pure, idempotent derivation."""

    def spec_and_plan(self, kind=TaskCriterionKind.WORKSPACE_INTEGRITY):
        spec = TaskSpecSnapshot(
            task_id="t1", revision=1, goal="g", scope=(), constraints=(),
            acceptance_criteria=(TaskAcceptanceCriterion(
                "c1", "workspace integrity", kind,
            ),),
        )
        return spec, ProofPlanCompiler.compile(spec)

    def execution(
        self, *, data=None, effect=ToolEffect.MUTATE,
        authority=ToolResultAuthority.MUTATION_FACT,
    ):
        from datetime import datetime, timezone

        from tsm_agt.core.execution import ToolCommitState, ToolExecutionRecord
        from tsm_agt.ports import ToolCall, ToolIdempotency, ToolResult

        call = ToolCall("call-1", "core.apply_patch", {})
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return ToolExecutionRecord(
            execution_id="exec-1", turn_id="turn-1", invocation_id="inv-1",
            call=call, payload_hash="h", policy_decision_id="p",
            effective_risk=ToolRisk.R0, approval_request_id=None,
            idempotency=ToolIdempotency.IDEMPOTENT, idempotency_key=None,
            state=ToolCommitState.COMMITTED,
            result=ToolResult("call-1", True, data=data or {}),
            started_at=stamp, updated_at=stamp,
            effect=effect, result_authority=authority,
        )

    def mutation(self):
        from datetime import datetime, timezone

        from tsm_agt.core.workspace import MutationOperation, MutationRecord

        return MutationRecord(
            "m1", "src/a.py", MutationOperation.CREATE, None, "sha-after",
            "step-m1", None, datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def test_plan_is_published_once(self) -> None:
        from tsm_agt.core.proof_projection import (
            PROOF_COVERAGE_EVALUATED, PROOF_PLAN_PUBLISHED, derive_proof_events,
            replay_proof_projection,
        )

        spec, compilation = self.spec_and_plan()
        empty = replay_proof_projection(())
        first = derive_proof_events(
            task_spec=spec, compilation=compilation, executions=(),
            specs_by_name={}, mutation_records=(), current=empty,
        )
        kinds = [event_type for event_type, _ in first]
        self.assertIn(PROOF_PLAN_PUBLISHED, kinds)
        self.assertEqual(kinds[-1], PROOF_COVERAGE_EVALUATED)

        recorded = replay_proof_projection(tuple(
            RuntimeEvent(f"e{i}", "t1", i, event_type, payload)
            for i, (event_type, payload) in enumerate(first, start=1)
        ))
        again = derive_proof_events(
            task_spec=spec, compilation=compilation, executions=(),
            specs_by_name={}, mutation_records=(), current=recorded,
        )
        self.assertEqual(again, ())

    def test_mutation_produces_artifact_observation_and_evidence(self) -> None:
        from tsm_agt.core.proof_projection import (
            ARTIFACT_OBSERVED, EVIDENCE_RECORDED, PROOF_OBSERVATION_RECORDED,
            derive_proof_events, replay_proof_projection,
        )

        spec, compilation = self.spec_and_plan()
        pending = derive_proof_events(
            task_spec=spec, compilation=compilation,
            executions=(self.execution(data={"mutation_ids": ["m1"]}),),
            specs_by_name={}, mutation_records=(self.mutation(),),
            current=replay_proof_projection(()),
        )
        kinds = {event_type for event_type, _ in pending}
        self.assertIn(ARTIFACT_OBSERVED, kinds)
        self.assertIn(PROOF_OBSERVATION_RECORDED, kinds)
        self.assertIn(EVIDENCE_RECORDED, kinds)

        recorded = replay_proof_projection(tuple(
            RuntimeEvent(f"e{i}", "t1", i, event_type, payload)
            for i, (event_type, payload) in enumerate(pending, start=1)
        ))
        summary = recorded.summary()
        self.assertEqual(summary["coverage"]["COVERED"], 1)

    def test_unattributed_observation_only_extends_the_graph(self) -> None:
        from tsm_agt.core.proof_projection import (
            EVIDENCE_RECORDED, PROOF_OBSERVATION_RECORDED, derive_proof_events,
            replay_proof_projection,
        )

        # The plan needs MUTATE_ARTIFACT; this execution only reads.
        spec, compilation = self.spec_and_plan()
        pending = derive_proof_events(
            task_spec=spec, compilation=compilation,
            executions=(self.execution(
                data={"mutation_ids": ["m1"]}, effect=ToolEffect.OBSERVE,
                authority=ToolResultAuthority.WORKSPACE_FACT,
            ),),
            specs_by_name={}, mutation_records=(self.mutation(),),
            current=replay_proof_projection(()),
        )
        payloads = [
            payload for event_type, payload in pending
            if event_type == PROOF_OBSERVATION_RECORDED
        ]
        self.assertTrue(payloads)
        self.assertFalse(any(item["attributed"] for item in payloads))
        self.assertNotIn(EVIDENCE_RECORDED, {kind for kind, _ in pending})

    def test_no_plan_change_and_no_new_execution_emits_nothing(self) -> None:
        from tsm_agt.core.proof_projection import (
            derive_proof_events, replay_proof_projection,
        )

        spec, compilation = self.spec_and_plan()
        pending = derive_proof_events(
            task_spec=spec, compilation=compilation, executions=(),
            specs_by_name={}, mutation_records=(), current=replay_proof_projection(()),
        )
        recorded = replay_proof_projection(tuple(
            RuntimeEvent(f"e{i}", "t1", i, event_type, payload)
            for i, (event_type, payload) in enumerate(pending, start=1)
        ))
        self.assertEqual(derive_proof_events(
            task_spec=spec, compilation=compilation, executions=(),
            specs_by_name={}, mutation_records=(), current=recorded,
        ), ())


class CapabilityDeclarationContractTest(unittest.IsolatedAsyncioTestCase):
    """§7 P1: declarations must match what a tool can actually establish.

    These lock the two non-derivable capabilities. A tool may only claim
    ``ENUMERATE_SCOPE`` when it reports whether its scope was covered, and only
    relation producers may claim ``OBSERVE_RELATION``.
    """

    async def _tools(self):
        from tsm_agt.bootstrap import compose_readonly_application

        application = compose_readonly_application()
        await application.registry.start_all()
        try:
            return {
                spec.name: spec for spec in await application.kernel.list_tools()
            }
        finally:
            await application.registry.stop_all()

    async def test_read_only_tools_derive_their_capability(self) -> None:
        tools = await self._tools()
        self.assertTrue(tools)
        for name, spec in tools.items():
            with self.subTest(tool=name):
                # Derived from effect + result_authority -- no per-tool rule.
                self.assertIn(
                    EvidenceCapability.READ_ARTIFACT, spec.evidence_capabilities,
                )

    async def test_only_truncation_reporting_tools_claim_enumeration(self) -> None:
        tools = await self._tools()
        self.assertNotIn(
            EvidenceCapability.ENUMERATE_SCOPE,
            tools["core.read_file"].evidence_capabilities,
            "reading one file cannot certify that a scope was enumerated",
        )
        for name in (
            "core.list_files", "core.find_files", "core.search_text",
            "core.grep_search",
        ):
            with self.subTest(tool=name):
                self.assertIn(
                    EvidenceCapability.ENUMERATE_SCOPE,
                    tools[name].evidence_capabilities,
                )

    def test_relation_capability_is_declared_only_by_relation_producers(self) -> None:
        from tsm_agt.adapters.builtin.code_intelligence_tools import (
            CodeIntelligenceToolProvider,
        )

        specs = {
            spec.name: spec
            for spec in CodeIntelligenceToolProvider(None)._tools
        }
        for name in (
            "code.definition", "code.references", "code.implementations",
        ):
            with self.subTest(tool=name):
                self.assertIn(
                    EvidenceCapability.OBSERVE_RELATION,
                    specs[name].evidence_capabilities,
                )
        for name in ("code.workspace_symbols", "code.symbol_overview"):
            with self.subTest(tool=name):
                self.assertNotIn(
                    EvidenceCapability.OBSERVE_RELATION,
                    specs[name].evidence_capabilities,
                    "a name search is not a parsed relation",
                )


class EnumerationCompletenessTest(unittest.TestCase):
    """AG-13 end to end: a truncated enumeration may not certify absence."""

    def cardinality_obligation(self):
        return obligation(
            predicate=ObligationPredicate.QUERY_CARDINALITY,
            selector={"locator": "src"}, capabilities=("ENUMERATE_SCOPE",),
            scope={"root_artifact_id": "artifact-root"},
        )

    def test_truncated_search_cannot_certify_absence(self) -> None:
        graph = graph_with(ArtifactNode(
            "artifact-root", ArtifactKind.FILE, "src", "r1",
        ))
        item = self.cardinality_obligation()
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.ENUMERATE_SCOPE,
            coverage={"complete": False},
        )
        coverage = evaluate_coverage(
            obligations=(item,), evidence=(record,), graph=graph,
        )[0]
        self.assertIs(coverage.status, CoverageStatus.BLOCKED)
        self.assertEqual(coverage.reason_code, "ENUMERATION_INCOMPLETE")

    def test_complete_search_certifies_absence(self) -> None:
        graph = graph_with(ArtifactNode(
            "artifact-root", ArtifactKind.FILE, "src", "r1",
        ))
        item = self.cardinality_obligation()
        record = EvidenceRecord(
            "ev-1", (item.obligation_id,), EvidenceCapability.ENUMERATE_SCOPE,
            coverage={"complete": True},
        )
        coverage = evaluate_coverage(
            obligations=(item,), evidence=(record,), graph=graph,
        )[0]
        self.assertIs(coverage.status, CoverageStatus.COVERED)

    def test_tool_truncation_flag_becomes_coverage_completeness(self) -> None:
        """The derivation turns the tool's own flag into coverage facts."""

        from tsm_agt.core.proof_projection import derive_proof_events

        spec = TaskSpecSnapshot(
            task_id="t1", revision=1, goal="g", scope=(), constraints=(),
            acceptance_criteria=(TaskAcceptanceCriterion(
                "c1", "workspace integrity", TaskCriterionKind.WORKSPACE_INTEGRITY,
            ),),
        )
        compilation = ProofPlanCompiler.compile(spec)
        for truncated, expected in ((True, False), (False, True)):
            with self.subTest(truncated=truncated):
                pending = derive_proof_events(
                    task_spec=spec, compilation=compilation,
                    executions=(self.execution(truncated=truncated),),
                    specs_by_name={}, mutation_records=(),
                    current=replay_proof_projection(()),
                )
                evidence = next(
                    payload["record"] for event_type, payload in pending
                    if event_type == "evidence.recorded"
                )
                self.assertEqual(
                    evidence["coverage"]["complete"], expected,
                )

    def execution(self, *, truncated: bool):
        from datetime import datetime, timezone

        from tsm_agt.core.execution import ToolCommitState, ToolExecutionRecord
        from tsm_agt.ports import ToolCall, ToolIdempotency, ToolResult

        call = ToolCall("call-1", "core.run_command", {})
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return ToolExecutionRecord(
            execution_id="exec-1", turn_id="turn-1", invocation_id="inv-1",
            call=call, payload_hash="h", policy_decision_id="p",
            effective_risk=ToolRisk.R0, approval_request_id=None,
            idempotency=ToolIdempotency.IDEMPOTENT, idempotency_key=None,
            state=ToolCommitState.COMMITTED,
            result=ToolResult("call-1", True, data={}, truncated=truncated),
            started_at=stamp, updated_at=stamp,
            effect=ToolEffect.MUTATE,
            result_authority=ToolResultAuthority.MUTATION_FACT,
        )


class EngineeringCapabilityContractTest(unittest.IsolatedAsyncioTestCase):
    """§7 P1: the real engineering composition derives truthful capabilities.

    Nothing here reads a tool name to decide evidence strength -- the values all
    come from ``effect`` + ``result_authority`` plus the two explicit extras.
    The point of this test is the *negative* space: tools that must prove
    nothing, and capabilities no tool may over-claim.
    """

    async def _tools(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from tsm_agt.bootstrap import (
            compose_openai_compatible_engineering_application_from_env,
        )

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "TSM_AGT_MODEL_BASE_URL=https://models.example.test/v1\n"
                "TSM_AGT_MODEL=open\nTSM_AGT_OPEN_MODEL=test-model\n"
                "TSM_AGT_MODEL_API_KEY=test-secret\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_env(
                        env_file
                    )
                )
        await application.registry.start_all()
        try:
            return {
                spec.name: spec for spec in await application.kernel.list_tools()
            }, application
        except Exception:
            await application.registry.stop_all()
            raise

    async def test_derived_capabilities_match_the_tool_kind(self) -> None:
        tools, application = await self._tools()
        try:
            self.assertTrue(tools)
            for name in ("core.apply_patch", "core.rollback_mutation"):
                with self.subTest(tool=name):
                    self.assertIn(
                        EvidenceCapability.MUTATE_ARTIFACT,
                        tools[name].evidence_capabilities,
                    )
            self.assertIn(
                EvidenceCapability.EXECUTE_VERIFICATION,
                tools["core.run_command"].evidence_capabilities,
            )
            self.assertIn(
                EvidenceCapability.OBSERVE_RELATION,
                tools["code.definition"].evidence_capabilities,
            )
        finally:
            await application.registry.stop_all()

    async def test_derived_only_tools_prove_nothing(self) -> None:
        tools, application = await self._tools()
        try:
            # A model-derived summary, an interaction and internal state tools
            # establish no evidence fact at all (INV-9: non-authoritative text is
            # never proof).
            for name in (
                "content.summarize", "core.request_input",
                "core.working_memory_update", "core.memory_remember",
            ):
                with self.subTest(tool=name):
                    self.assertEqual(tools[name].evidence_capabilities, frozenset())
        finally:
            await application.registry.stop_all()

    async def test_only_local_enumerations_claim_enumeration(self) -> None:
        tools, application = await self._tools()
        try:
            enumeration_claims = {
                name for name, spec in tools.items()
                if EvidenceCapability.ENUMERATE_SCOPE in spec.evidence_capabilities
            }
            self.assertEqual(enumeration_claims, {
                "core.list_files", "core.find_files", "core.search_text",
                "core.grep_search", "code.diagnostics",
            })
            # A web search is bounded by result count and is not a defensible
            # enumeration of its scope, so it must not claim one.
            self.assertNotIn(
                EvidenceCapability.ENUMERATE_SCOPE,
                tools["web.search"].evidence_capabilities,
            )
        finally:
            await application.registry.stop_all()


class EvidenceIntentValidationTest(unittest.TestCase):
    """§5.2: an explicit intent is validated, may only add, never blocks."""

    def setUp(self) -> None:
        from tsm_agt.core.evidence_coverage import ProofPlanSnapshot

        self.item = make_obligation(
            contract_ref="c1", predicate=ObligationPredicate.ARTIFACT_CONTENT,
            requirement_text="read the file", selector={"locator": "src/a.py"},
            required=True, required_capabilities=("READ_ARTIFACT",),
        )
        self.plan = ProofPlanSnapshot.build(
            task_id="t1", task_spec_revision=1, obligations=(self.item,),
        )
        self.graph = graph_with(file_node())

    def validate(self, intent, capabilities=("READ_ARTIFACT",)):
        from tsm_agt.core.evidence_coverage import (
            EvidenceIntent, validate_evidence_intent,
        )

        return validate_evidence_intent(
            intent=EvidenceIntent.from_data(intent),
            plan=self.plan,
            tool_capabilities=frozenset(
                EvidenceCapability(name) for name in capabilities
            ),
            graph=self.graph,
        )

    def test_valid_intent_is_accepted(self) -> None:
        validation = self.validate({
            "obligation_ids": [self.item.obligation_id],
            "expected_capability": "READ_ARTIFACT",
        })
        self.assertTrue(validation.accepted)
        self.assertEqual(
            validation.accepted_obligation_ids, (self.item.obligation_id,),
        )
        self.assertEqual(validation.reason_codes, ())

    def test_unknown_obligation_is_rejected(self) -> None:
        validation = self.validate({
            "obligation_ids": ["obg-does-not-exist"],
            "expected_capability": "READ_ARTIFACT",
        })
        self.assertFalse(validation.accepted)
        self.assertIn("UNKNOWN_OBLIGATION", validation.reason_codes)

    def test_capability_must_be_declared_by_the_tool(self) -> None:
        validation = self.validate(
            {"obligation_ids": [self.item.obligation_id],
             "expected_capability": "ENUMERATE_SCOPE"},
            capabilities=("READ_ARTIFACT",),
        )
        self.assertIn("CAPABILITY_NOT_SUPPORTED", validation.reason_codes)

    def test_unknown_capability_name_is_rejected(self) -> None:
        validation = self.validate(
            {"obligation_ids": [self.item.obligation_id],
             "expected_capability": "MAKE_COFFEE"},
        )
        self.assertIn("UNKNOWN_CAPABILITY", validation.reason_codes)

    def test_obligations_without_a_capability_claim_are_rejected(self) -> None:
        validation = self.validate({"obligation_ids": [self.item.obligation_id]})
        self.assertIn("MISSING_EXPECTED_CAPABILITY", validation.reason_codes)

    def test_selector_outside_the_allowed_scope_is_rejected(self) -> None:
        validation = self.validate({
            "obligation_ids": [self.item.obligation_id],
            "expected_capability": "READ_ARTIFACT",
            "target_selectors": [{"locator": "src/never-observed.py"}],
        })
        self.assertIn("SELECTOR_OUT_OF_SCOPE", validation.reason_codes)

    def test_selector_naming_an_observed_artifact_is_accepted(self) -> None:
        validation = self.validate({
            "obligation_ids": [self.item.obligation_id],
            "expected_capability": "READ_ARTIFACT",
            "target_selectors": [{"artifact_id": "artifact-1"}],
        })
        self.assertTrue(validation.accepted)

    def test_empty_intent_is_a_no_op(self) -> None:
        self.assertEqual(self.validate({}).reason_codes, ())
        self.assertEqual(self.validate({}).accepted_obligation_ids, ())

    def test_intent_never_blocks_the_action(self) -> None:
        """Even a rejected intent emits the observation (SPEC §5.2 rule 4)."""

        from tsm_agt.core.proof_projection import (
            PROOF_OBSERVATION_RECORDED, derive_proof_events,
            replay_proof_projection,
        )

        spec = TaskSpecSnapshot(
            task_id="t1", revision=1, goal="g", scope=(), constraints=(),
            acceptance_criteria=(TaskAcceptanceCriterion(
                "c1", "workspace integrity", TaskCriterionKind.WORKSPACE_INTEGRITY,
            ),),
        )
        compilation = ProofPlanCompiler.compile(spec)
        execution = _committed_execution(
            intent={"obligation_ids": ["obg-bogus"],
                    "expected_capability": "MUTATE_ARTIFACT"},
        )
        pending = derive_proof_events(
            task_spec=spec, compilation=compilation, executions=(execution,),
            specs_by_name={}, mutation_records=(),
            current=replay_proof_projection(()),
        )
        observations = [
            payload for event_type, payload in pending
            if event_type == PROOF_OBSERVATION_RECORDED
        ]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["intent_rejections"], ["UNKNOWN_OBLIGATION"])

    def test_accepted_intent_adds_without_removing_auto_attribution(self) -> None:
        from tsm_agt.core.proof_projection import (
            EVIDENCE_RECORDED, derive_proof_events, replay_proof_projection,
        )

        spec = TaskSpecSnapshot(
            task_id="t1", revision=1, goal="g", scope=(), constraints=(),
            acceptance_criteria=(TaskAcceptanceCriterion(
                "c1", "workspace integrity", TaskCriterionKind.WORKSPACE_INTEGRITY,
            ),),
        )
        compilation = ProofPlanCompiler.compile(spec)
        auto_id = compilation.plan.obligations[0].obligation_id
        execution = _committed_execution(
            intent={"obligation_ids": [auto_id],
                    "expected_capability": "MUTATE_ARTIFACT"},
        )
        pending = derive_proof_events(
            task_spec=spec, compilation=compilation, executions=(execution,),
            specs_by_name={}, mutation_records=(),
            current=replay_proof_projection(()),
        )
        record = next(
            payload["record"] for event_type, payload in pending
            if event_type == EVIDENCE_RECORDED
        )
        self.assertEqual(record["binding_source"], "explicit")
        self.assertEqual(record["obligation_ids"], [auto_id])


def _committed_execution(*, intent):
    from datetime import datetime, timezone

    from tsm_agt.core.execution import ToolCommitState, ToolExecutionRecord
    from tsm_agt.ports import ToolCall, ToolIdempotency, ToolResult

    call = ToolCall("call-1", "core.apply_patch", {}, evidence_intent=intent)
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ToolExecutionRecord(
        execution_id="exec-1", turn_id="turn-1", invocation_id="inv-1",
        call=call, payload_hash="h", policy_decision_id="p",
        effective_risk=ToolRisk.R0, approval_request_id=None,
        idempotency=ToolIdempotency.IDEMPOTENT, idempotency_key=None,
        state=ToolCommitState.COMMITTED,
        result=ToolResult("call-1", True, data={"mutation_ids": []}),
        started_at=stamp, updated_at=stamp,
        effect=ToolEffect.MUTATE,
        result_authority=ToolResultAuthority.MUTATION_FACT,
    )


class CoverageGapMappingTest(unittest.TestCase):
    """§3.5: coverage becomes a *required* readiness gap, never a soft hint."""

    def obligation_and_coverage(self, status, *, capabilities=("READ_ARTIFACT",)):
        item = make_obligation(
            contract_ref="c1", predicate=ObligationPredicate.ARTIFACT_CONTENT,
            requirement_text="read the file", selector={"locator": "src/a.py"},
            required=True, required_capabilities=capabilities,
        )
        plan = ProofPlanSnapshot.build(
            task_id="t1", task_spec_revision=1, obligations=(item,),
        )
        from tsm_agt.core.evidence_coverage import ObligationCoverage

        coverage = ObligationCoverage(
            item.obligation_id, status, (), frozenset(), "REASON",
        )
        return plan, coverage

    def tools(self):
        return (
            tool("core.read_file", ToolEffect.OBSERVE, ToolResultAuthority.WORKSPACE_FACT),
            tool("core.run_command", ToolEffect.EXECUTE, ToolResultAuthority.PROCESS_FACT),
        )

    def test_missing_gap_is_required_and_closable(self) -> None:
        from tsm_agt.core.evidence_coverage import coverage_gaps

        plan, coverage = self.obligation_and_coverage(CoverageStatus.MISSING)
        gap = coverage_gaps(plan=plan, coverages=(coverage,), tools=self.tools())[0]
        self.assertEqual(gap.kind, "PROOF_COVERAGE_MISSING")
        self.assertTrue(gap.required)
        self.assertEqual(gap.required_effects, (ToolEffect.OBSERVE,))
        self.assertEqual(gap.candidate_tools, ("core.read_file",))
        self.assertTrue(gap.effective_required_effects)
        self.assertEqual(gap.evidence_reference, "c1")

    def test_unclosable_statuses_clear_effects_and_candidates(self) -> None:
        from tsm_agt.core.evidence_coverage import coverage_gaps

        for status, kind in (
            (CoverageStatus.CONFLICTING, "PROOF_COVERAGE_CONFLICTING"),
            (CoverageStatus.BLOCKED, "PROOF_COVERAGE_BLOCKED"),
        ):
            with self.subTest(status=status):
                plan, coverage = self.obligation_and_coverage(status)
                gap = coverage_gaps(
                    plan=plan, coverages=(coverage,), tools=self.tools(),
                )[0]
                self.assertEqual(gap.kind, kind)
                self.assertTrue(gap.required)
                self.assertEqual(gap.required_effects, ())
                self.assertFalse(gap.recoverable)
                # The decisive fact: the gate reads effects, not `recoverable`.
                self.assertFalse(gap.effective_required_effects)

    def test_covered_obligations_produce_no_gap(self) -> None:
        from tsm_agt.core.evidence_coverage import coverage_gaps

        plan, coverage = self.obligation_and_coverage(CoverageStatus.COVERED)
        self.assertEqual(
            coverage_gaps(plan=plan, coverages=(coverage,), tools=self.tools()), (),
        )

    def test_gap_ids_are_stable_across_recompilation(self) -> None:
        from tsm_agt.core.evidence_coverage import coverage_gaps

        first, coverage = self.obligation_and_coverage(CoverageStatus.MISSING)
        second, _ = self.obligation_and_coverage(CoverageStatus.MISSING)
        self.assertEqual(
            coverage_gaps(plan=first, coverages=(coverage,), tools=self.tools())[0].gap_id,
            coverage_gaps(plan=second, coverages=(coverage,), tools=self.tools())[0].gap_id,
        )

    def test_escalated_kinds_are_a_parallel_exemption_set(self) -> None:
        from tsm_agt.core.evidence_coverage import ESCALATED_GAP_KINDS

        self.assertEqual(ESCALATED_GAP_KINDS, frozenset({
            "PROOF_COVERAGE_MISSING", "PROOF_COVERAGE_STALE",
            "PROOF_COVERAGE_CONFLICTING", "PROOF_COVERAGE_BLOCKED",
        }))
        # Deliberately not merged into the judged set.
        self.assertNotIn("UNRESOLVED_EFFECT_FAILURE", ESCALATED_GAP_KINDS)


class ProofMetricTest(unittest.TestCase):
    """§7 P1.5: the four gate metrics are computed from the projection."""

    def project(self, *, obligations, evidence=(), graph=None):
        from tsm_agt.core.proof_projection import replay_proof_projection

        plan = ProofPlanSnapshot.build(
            task_id="t1", task_spec_revision=1, obligations=tuple(obligations),
        )
        events = [RuntimeEvent(
            "e1", "t1", 1, PROOF_PLAN_PUBLISHED, {"snapshot": plan.to_data()},
        )]
        return replay_proof_projection(tuple(events))

    def test_advisory_rate_and_gap_ratio(self) -> None:
        required = obligation(contract_ref="c1")
        advisory = obligation(contract_ref="c2", required=False)
        projection = self.project(obligations=(required, advisory))
        summary = projection.summary()
        self.assertAlmostEqual(summary["advisory_obligation_rate"], 0.5)
        self.assertAlmostEqual(summary["coverage_gap_ratio"], 1.0)

    def test_attribution_gap_names_obligations_nobody_could_attribute(self) -> None:
        projection = self.project(obligations=(
            obligation(contract_ref="c1", capabilities=("READ_ARTIFACT",)),
        ))
        summary = projection.summary()
        self.assertEqual(
            summary["attribution_gap"],
            [projection.plan.obligations[0].obligation_id],
        )

    def test_metrics_are_absent_without_a_plan(self) -> None:
        from tsm_agt.core.proof_projection import replay_proof_projection

        summary = replay_proof_projection(()).summary()
        self.assertIsNone(summary["advisory_obligation_rate"])
        self.assertIsNone(summary["coverage_gap_ratio"])
        self.assertEqual(summary["attribution_gap"], [])


class MultiCapabilityObservationTest(unittest.TestCase):
    """A read+enumerate tool must be able to close enumeration obligations.

    Regression: emitting only the "strongest" capability per execution made
    ``QUERY_CARDINALITY`` permanently unclosable, because ``core.find_files``
    derives READ_ARTIFACT as well and that won the single-capability pick.
    """

    def test_enumeration_obligation_is_closable_by_a_listing_tool(self) -> None:
        from tsm_agt.core.proof_obligation import ObligationTruncation
        from tsm_agt.core.proof_projection import (
            ARTIFACT_OBSERVED, PROOF_PLAN_PUBLISHED, derive_proof_events,
            replay_proof_projection,
        )
        from tsm_agt.core.task_spec import ProofCompilation

        item = make_obligation(
            contract_ref="c-zero", predicate=ObligationPredicate.QUERY_CARDINALITY,
            requirement_text="no match exists in src",
            selector={"locator": "src"}, required=True,
            required_capabilities=("ENUMERATE_SCOPE",),
            scope={"root_artifact_id": "artifact-root", "require_complete": True},
        )
        plan = ProofPlanSnapshot.build(
            task_id="t1", task_spec_revision=1, obligations=(item,),
        )
        compilation = ProofCompilation(
            plan=plan,
            truncation=ObligationTruncation(plan.obligations, (), False),
        )
        root = ArtifactNode("artifact-root", ArtifactKind.FILE, "src", "r1")
        seed = (
            RuntimeEvent("e1", "t1", 1, PROOF_PLAN_PUBLISHED, {
                "snapshot": plan.to_data(),
            }),
            RuntimeEvent("e2", "t1", 2, ARTIFACT_OBSERVED, {
                "node": root.to_data(),
            }),
        )
        current = replay_proof_projection(seed)

        listing_tool = tool(
            "core.find_files", ToolEffect.OBSERVE,
            ToolResultAuthority.WORKSPACE_FACT,
            extra_evidence_capabilities=frozenset({
                EvidenceCapability.ENUMERATE_SCOPE,
            }),
        )
        execution = _listing_execution(truncated=False)
        pending = derive_proof_events(
            task_spec=TaskSpecSnapshot(
                task_id="t1", revision=1, goal="g", scope=(), constraints=(),
                acceptance_criteria=(TaskAcceptanceCriterion(
                    "c1", "workspace integrity",
                    TaskCriterionKind.WORKSPACE_INTEGRITY,
                ),),
            ),
            compilation=compilation, executions=(execution,),
            specs_by_name={"core.find_files": listing_tool},
            mutation_records=(), current=current,
            events=seed, available_capabilities=frozenset({"ENUMERATE_SCOPE"}),
        )
        # The kernel appends the new events to the stored ones; replay both.
        recorded = replay_proof_projection(
            tuple(seed) + tuple(
                RuntimeEvent(f"p{i}", "t1", 100 + i, event_type, payload)
                for i, (event_type, payload) in enumerate(pending, start=1)
            )
        )

        by_id = {item.obligation_id: item for item in recorded.coverages}
        self.assertIs(by_id[item.obligation_id].status, CoverageStatus.COVERED)

    def test_truncated_listing_still_cannot_certify_absence(self) -> None:
        from tsm_agt.core.proof_obligation import ObligationTruncation
        from tsm_agt.core.proof_projection import (
            ARTIFACT_OBSERVED, derive_proof_events, replay_proof_projection,
        )
        from tsm_agt.core.task_spec import ProofCompilation

        item = make_obligation(
            contract_ref="c-zero", predicate=ObligationPredicate.QUERY_CARDINALITY,
            requirement_text="no match exists in src",
            selector={"locator": "src"}, required=True,
            required_capabilities=("ENUMERATE_SCOPE",),
            scope={"root_artifact_id": "artifact-root", "require_complete": True},
        )
        plan = ProofPlanSnapshot.build(
            task_id="t1", task_spec_revision=1, obligations=(item,),
        )
        compilation = ProofCompilation(
            plan=plan,
            truncation=ObligationTruncation(plan.obligations, (), False),
        )
        root = ArtifactNode("artifact-root", ArtifactKind.FILE, "src", "r1")
        seed = (
            RuntimeEvent("e1", "t1", 1, ARTIFACT_OBSERVED, {
                "node": root.to_data(),
            }),
        )
        current = replay_proof_projection(seed)
        listing_tool = tool(
            "core.find_files", ToolEffect.OBSERVE,
            ToolResultAuthority.WORKSPACE_FACT,
            extra_evidence_capabilities=frozenset({
                EvidenceCapability.ENUMERATE_SCOPE,
            }),
        )
        pending = derive_proof_events(
            task_spec=TaskSpecSnapshot(
                task_id="t1", revision=1, goal="g", scope=(), constraints=(),
                acceptance_criteria=(TaskAcceptanceCriterion(
                    "c1", "workspace integrity",
                    TaskCriterionKind.WORKSPACE_INTEGRITY,
                ),),
            ),
            compilation=compilation,
            executions=(_listing_execution(truncated=True),),
            specs_by_name={"core.find_files": listing_tool},
            mutation_records=(), current=current, events=seed,
        )
        kinds = [event_type for event_type, _ in pending]
        self.assertIn(PROOF_SCOPE_NARROWED, kinds)
        # The kernel appends the new events to the stored ones; replay both.
        recorded = replay_proof_projection(
            tuple(seed) + tuple(
                RuntimeEvent(f"p{i}", "t1", 100 + i, event_type, payload)
                for i, (event_type, payload) in enumerate(pending, start=1)
            )
        )
        by_id = {item.obligation_id: item for item in recorded.coverages}
        self.assertIs(by_id[item.obligation_id].status, CoverageStatus.BLOCKED)

    def test_declared_scope_is_recorded_at_publication(self) -> None:
        from tsm_agt.core.proof_obligation import ObligationTruncation
        from tsm_agt.core.proof_projection import (
            PROOF_SCOPE_DECLARED, derive_proof_events, replay_proof_projection,
        )
        from tsm_agt.core.task_spec import ProofCompilation

        item = make_obligation(
            contract_ref="c-zero", predicate=ObligationPredicate.QUERY_CARDINALITY,
            requirement_text="no match exists", selector={"locator": "src"},
            required=True, required_capabilities=("ENUMERATE_SCOPE",),
            scope={"root_artifact_id": "artifact-root", "member_count": 3},
        )
        plan = ProofPlanSnapshot.build(
            task_id="t1", task_spec_revision=1, obligations=(item,),
        )
        pending = derive_proof_events(
            task_spec=TaskSpecSnapshot(
                task_id="t1", revision=1, goal="g", scope=(), constraints=(),
                acceptance_criteria=(TaskAcceptanceCriterion(
                    "c1", "workspace integrity",
                    TaskCriterionKind.WORKSPACE_INTEGRITY,
                ),),
            ),
            compilation=ProofCompilation(
                plan=plan,
                truncation=ObligationTruncation(plan.obligations, (), False),
            ),
            executions=(), specs_by_name={}, mutation_records=(),
            current=replay_proof_projection(()),
        )
        declared = [
            payload for event_type, payload in pending
            if event_type == PROOF_SCOPE_DECLARED
        ]
        self.assertEqual(len(declared), 1)
        self.assertEqual(declared[0]["source"], "contract")
        self.assertEqual(declared[0]["obligation_id"], item.obligation_id)


def _listing_execution(*, truncated: bool):
    from datetime import datetime, timezone

    from tsm_agt.core.execution import ToolCommitState, ToolExecutionRecord
    from tsm_agt.ports import ToolCall, ToolIdempotency, ToolResult

    call = ToolCall("call-list", "core.find_files", {"pattern": "*.py"})
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ToolExecutionRecord(
        execution_id="exec-list", turn_id="turn-1", invocation_id="inv-list",
        call=call, payload_hash="h", policy_decision_id="p",
        effective_risk=ToolRisk.R0, approval_request_id=None,
        idempotency=ToolIdempotency.IDEMPOTENT, idempotency_key=None,
        state=ToolCommitState.COMMITTED,
        result=ToolResult("call-list", True, data={}, truncated=truncated),
        started_at=stamp, updated_at=stamp,
        effect=ToolEffect.OBSERVE,
        result_authority=ToolResultAuthority.WORKSPACE_FACT,
    )


class EnforcementReadinessTest(unittest.TestCase):
    """§7 P1.5 gate: coverage may only withhold success once the data supports it."""

    def project(self, *, required=1, advisory=0, observations=0, attributed=0,
                overflow=False):
        from tsm_agt.core.proof_projection import (
            PROOF_OBSERVATION_RECORDED, PROOF_PLAN_TRUNCATED,
            PROOF_PLAN_PUBLISHED, replay_proof_projection,
        )

        obligations = tuple(
            obligation(contract_ref=f"r{i}") for i in range(required)
        ) + tuple(
            obligation(contract_ref=f"a{i}", required=False)
            for i in range(advisory)
        )
        plan = ProofPlanSnapshot.build(
            task_id="t1", task_spec_revision=1, obligations=obligations,
        )
        events = [RuntimeEvent("e0", "t1", 0, PROOF_PLAN_PUBLISHED, {
            "snapshot": plan.to_data(),
        })]
        if overflow:
            events.append(RuntimeEvent(
                "e1", "t1", 1, PROOF_PLAN_TRUNCATED,
                {"required_overflow": True},
            ))
        for index in range(observations):
            events.append(RuntimeEvent(
                f"o{index}", "t1", 2 + index, PROOF_OBSERVATION_RECORDED,
                {"observation_id": f"obs-{index}",
                 "attributed": index < attributed},
            ))
        return replay_proof_projection(tuple(events))

    def test_small_sample_never_approves(self) -> None:
        from tsm_agt.core.proof_projection import enforcement_readiness

        readiness = enforcement_readiness(
            tuple(self.project(observations=1, attributed=1) for _ in range(19))
        )
        self.assertFalse(readiness.ready)
        self.assertIn("INSUFFICIENT_SAMPLE", readiness.reason_codes)

    def test_healthy_window_is_ready(self) -> None:
        from tsm_agt.core.proof_projection import enforcement_readiness

        readiness = enforcement_readiness(
            tuple(self.project(observations=10, attributed=10) for _ in range(20))
        )
        self.assertTrue(readiness.ready, readiness.reason_codes)
        self.assertAlmostEqual(readiness.auto_attribution_rate, 1.0)

    def test_low_attribution_blocks_enforcement(self) -> None:
        from tsm_agt.core.proof_projection import enforcement_readiness

        readiness = enforcement_readiness(
            tuple(self.project(observations=10, attributed=9) for _ in range(20))
        )
        self.assertIn("AUTO_ATTRIBUTION_BELOW_THRESHOLD", readiness.reason_codes)

    def test_advisory_ceiling_blocks_enforcement(self) -> None:
        from tsm_agt.core.proof_projection import enforcement_readiness

        readiness = enforcement_readiness(
            tuple(
                self.project(required=1, advisory=1, observations=10, attributed=10)
                for _ in range(20)
            )
        )
        self.assertIn("ADVISORY_RATE_ABOVE_CEILING", readiness.reason_codes)

    def test_required_overflow_blocks_enforcement(self) -> None:
        from tsm_agt.core.proof_projection import enforcement_readiness

        readiness = enforcement_readiness(
            tuple(
                self.project(observations=10, attributed=10, overflow=True)
                for _ in range(20)
            )
        )
        self.assertIn("REQUIRED_OVERFLOW_OBSERVED", readiness.reason_codes)

    def test_no_data_blocks_enforcement(self) -> None:
        from tsm_agt.core.proof_projection import enforcement_readiness

        readiness = enforcement_readiness(
            tuple(self.project() for _ in range(20))
        )
        self.assertIn("NO_ATTRIBUTION_DATA", readiness.reason_codes)


class Inv3DemotionCriterionTest(unittest.TestCase):
    """INV-3 upgrade: the deadlock guard may only drop gaps nobody asked for."""

    def gap(self, gap_id, *, reference="", kind="SOME_KIND", effects=(),
            required=True):
        from tsm_agt.ports import CompletionGap

        return CompletionGap(
            gap_id, kind, "description", "MISSING", required, False,
            reference, "", effects, (), "",
        )

    def apply(self, *gaps):
        from tsm_agt.core.kernel import _apply_inv3_demotion

        return _apply_inv3_demotion(tuple(gaps))

    def test_gap_without_provenance_is_demoted(self) -> None:
        gaps, violations = self.apply(self.gap("g1"))
        self.assertEqual([gap.gap_id for gap in violations], ["g1"])
        self.assertFalse(gaps[0].required)

    def test_contract_sourced_gap_is_never_demoted(self) -> None:
        gaps, violations = self.apply(self.gap("g1", reference="c1"))
        self.assertEqual(violations, [])
        self.assertTrue(gaps[0].required)

    def test_ledger_reference_is_never_demoted(self) -> None:
        for reference in ("event:7", "tool_call:9", "mutation:3"):
            with self.subTest(reference=reference):
                gaps, violations = self.apply(self.gap("g1", reference=reference))
                self.assertEqual(violations, [])
                self.assertTrue(gaps[0].required)

    def test_exempt_kinds_are_never_demoted(self) -> None:
        for kind in ("UNRESOLVED_EFFECT_FAILURE", "JUDGED_CRITERION_WITHOUT_JUDGE"):
            with self.subTest(kind=kind):
                gaps, violations = self.apply(self.gap("g1", kind=kind))
                self.assertEqual(violations, [])
                self.assertTrue(gaps[0].required)

    def test_gap_with_effects_is_untouched(self) -> None:
        gaps, violations = self.apply(
            self.gap("g1", effects=(ToolEffect.OBSERVE,))
        )
        self.assertEqual(violations, [])
        self.assertTrue(gaps[0].required)

    def test_already_optional_gap_is_not_a_violation(self) -> None:
        gaps, violations = self.apply(self.gap("g1", required=False))
        self.assertEqual(violations, [])
        self.assertFalse(gaps[0].required)

    def test_a_coverage_gap_survives_inv3(self) -> None:
        """The integration that matters: coverage gaps are required *and* exempt."""

        from tsm_agt.core.evidence_coverage import ObligationCoverage, coverage_gaps

        item = make_obligation(
            contract_ref="c1", predicate=ObligationPredicate.ARTIFACT_CONTENT,
            requirement_text="read the file", selector={"locator": "src/a.py"},
            required=True, required_capabilities=("ENUMERATE_SCOPE",),
        )
        plan = ProofPlanSnapshot.build(
            task_id="t1", task_spec_revision=1, obligations=(item,),
        )
        coverage = ObligationCoverage(
            item.obligation_id, CoverageStatus.BLOCKED, (), frozenset(), "R",
        )
        mapped = coverage_gaps(plan=plan, coverages=(coverage,), tools=())
        self.assertEqual(len(mapped), 1)
        # Empty effects (unclosable) but contract-sourced -> must stay required.
        self.assertFalse(mapped[0].effective_required_effects)
        gaps, violations = self.apply(*mapped)
        self.assertEqual(violations, [])
        self.assertTrue(gaps[0].required)


class CoverageGateEnforcementTest(unittest.IsolatedAsyncioTestCase):
    """SPEC P2: OBSERVE is inert; ENFORCE_* routes coverage into the same gate."""

    async def _gaps(self, *, enforcement):
        import tempfile
        from dataclasses import replace as dataclass_replace
        from pathlib import Path

        from tsm_agt.bootstrap import compose_fixture_application

        with tempfile.TemporaryDirectory() as directory:
            application = compose_fixture_application(tool_adapters=())
            application.kernel._dependencies = dataclass_replace(
                application.kernel._dependencies,
                proof_enforcement_mode=enforcement,
            )
            await application.registry.start_all()
            try:
                task = await application.kernel.create_task(
                    "do a thing", Path(directory),
                    task_id=f"gate-{enforcement.name.casefold()}",
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await application.kernel.transition_task(
                        task.task_id, state, state.value,
                    )
                return await application.kernel._completion_readiness_gaps(
                    task.task_id, await application.kernel.list_tools(),
                )
            finally:
                await application.registry.stop_all()

    async def test_observe_mode_adds_no_coverage_gap(self) -> None:
        gaps = await self._gaps(enforcement=ProofEnforcementMode.OBSERVE)
        self.assertEqual(
            [gap for gap in gaps if gap.kind.startswith("PROOF_COVERAGE_")], [],
        )

    def packet(self, *, required=True):
        from tsm_agt.core.semantic_review import build_semantic_review_packet

        semantic = make_obligation(
            contract_ref="c-rubric", predicate=ObligationPredicate.SEMANTIC_REVIEW,
            requirement_text="the answer is clear", selector={"criterion": "rubric"},
            required=required,
        )
        other = obligation(contract_ref="c-file", capabilities=("READ_ARTIFACT",))
        record = EvidenceRecord(
            "evd-1", (semantic.obligation_id,), EvidenceCapability.READ_ARTIFACT,
            observation={"verdict": "SATISFIED"},
        )
        return build_semantic_review_packet(
            task_id="t1", obligations=(semantic, other),
            graph=graph_with(file_node()),
            evidence=(record,), deterministic_acceptance={"status": "PASSED"},
        ), semantic

    def test_packet_carries_only_the_semantic_scope(self) -> None:
        packet, semantic = self.packet()
        self.assertEqual(packet.obligation_ids, (semantic.obligation_id,))
        # The deterministic obligation is not offered to the judge.
        self.assertNotIn("c-file", packet.task_contract)

    def test_packet_has_no_field_for_worker_private_state(self) -> None:
        packet, _ = self.packet()
        serialized = str(packet.to_data())
        for forbidden in ("working_memory", "api_key", "token", "prompt"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, serialized)

    def test_review_evidence_is_bounded_and_referenceable(self) -> None:
        from tsm_agt.core.semantic_review import review_evidence

        packet, _ = self.packet()
        excerpts = review_evidence(packet)
        self.assertEqual(len(excerpts), 1)
        self.assertEqual(excerpts[0].reference, "evd-1")
        self.assertTrue(excerpts[0].excerpt)

    def test_packet_round_trips(self) -> None:
        from tsm_agt.core.semantic_review import SemanticReviewPacket

        packet, _ = self.packet()
        restored = SemanticReviewPacket.from_data(packet.to_data())
        self.assertEqual(restored.evidence_ids, packet.evidence_ids)
        self.assertEqual(restored.obligation_ids, packet.obligation_ids)


class SemanticReviewVerdictTest(unittest.TestCase):
    """SPEC §6.2: affirmation must cite evidence; nothing fails open."""

    def outcome(self, verdict, cited=()):
        from tsm_agt.core.semantic_review import (
            SemanticReviewPacket, normalize_judgement,
        )

        packet = SemanticReviewPacket(
            task_id="t1", obligation_ids=("ob1",),
            evidence_records=(
                EvidenceRecord("evd-1", ("ob1",), EvidenceCapability.READ_ARTIFACT),
            ),
        )
        return normalize_judgement(
            obligation_ids=("ob1",), verdict=verdict, reason="r",
            cited_evidence_ids=cited, packet=packet,
        )

    def test_uncited_affirmation_is_undecidable(self) -> None:
        outcome = self.outcome(JudgeVerdict.SATISFIED)
        self.assertFalse(outcome.satisfied)
        self.assertEqual(outcome.disposition, "NEEDS_REVIEW")
        self.assertIs(outcome.verdict, JudgeVerdict.UNDECIDABLE)

    def test_citation_outside_the_packet_does_not_count(self) -> None:
        outcome = self.outcome(JudgeVerdict.SATISFIED, ("evd-not-in-packet",))
        self.assertFalse(outcome.satisfied)
        self.assertEqual(outcome.cited_evidence_ids, ())

    def test_cited_affirmation_satisfies(self) -> None:
        outcome = self.outcome(JudgeVerdict.SATISFIED, ("evd-1",))
        self.assertTrue(outcome.satisfied)
        self.assertEqual(outcome.disposition, "SATISFIED")
        self.assertEqual(outcome.cited_evidence_ids, ("evd-1",))

    def test_revision_becomes_bounded_rework(self) -> None:
        outcome = self.outcome(JudgeVerdict.NEEDS_REVISION)
        self.assertEqual(outcome.disposition, "NEEDS_REWORK")

    def test_undecidable_and_error_go_to_review(self) -> None:
        for verdict in (JudgeVerdict.UNDECIDABLE, JudgeVerdict.JUDGE_ERROR):
            with self.subTest(verdict=verdict):
                self.assertEqual(
                    self.outcome(verdict).disposition, "NEEDS_REVIEW",
                )


class ReviewerModelConfigurationTest(unittest.TestCase):
    """SPEC §12.6 D5: the reviewer is a model *name*, never a second endpoint."""

    def configuration(self, *, review=None):
        import tempfile
        from pathlib import Path

        from tsm_agt.bootstrap.model_configuration import load_model_configuration

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            lines = [
                "TSM_AGT_MODEL_BASE_URL=https://models.example.test/v1",
                "TSM_AGT_MODEL=open",
                "TSM_AGT_OPEN_MODEL=primary-model",
                "TSM_AGT_MODEL_API_KEY=test-secret",
            ]
            if review is not None:
                lines.append(f"TSM_AGT_REVIEW_MODEL={review}")
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return load_model_configuration(env_file, {})

    def test_absent_review_model_reuses_the_primary_name(self) -> None:
        configuration = self.configuration()
        self.assertEqual(configuration.review_model, configuration.model)

    def test_review_model_overrides_only_the_name(self) -> None:
        configuration = self.configuration(review="smaller-model")
        self.assertEqual(configuration.review_model, "smaller-model")
        # Same endpoint, same key, same protocol: D5 by construction.
        self.assertEqual(configuration.model, "primary-model")
        self.assertEqual(configuration.protocol, "open")
        self.assertEqual(configuration.base_url, "https://models.example.test/v1")

    def test_empty_review_model_falls_back_to_the_primary_name(self) -> None:
        # An empty value is "not configured", not "model named empty".
        configuration = self.configuration(review="")
        self.assertEqual(configuration.review_model, configuration.model)

    def test_review_model_is_never_a_required_setting(self) -> None:
        # Omitting it entirely must load cleanly; it is an override, not a
        # second provider configuration.
        configuration = self.configuration()
        self.assertTrue(configuration.base_url)
        self.assertTrue(configuration.api_key)


class ReviewerProviderWiringTest(unittest.IsolatedAsyncioTestCase):
    """D5 wiring: no override means the *same* provider object is reused."""

    async def _judge_provider(self, *, review_model):
        import tempfile
        from pathlib import Path

        from tsm_agt.bootstrap import (
            compose_openai_compatible_engineering_application_from_env,
        )
        from tsm_agt.ports import ModelProviderPort, RubricJudgePort

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            lines = [
                "TSM_AGT_MODEL_BASE_URL=https://models.example.test/v1",
                "TSM_AGT_MODEL=open",
                "TSM_AGT_OPEN_MODEL=primary-model",
                "TSM_AGT_MODEL_API_KEY=test-secret",
            ]
            if review_model:
                lines.append(f"TSM_AGT_REVIEW_MODEL={review_model}")
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            import os
            from unittest.mock import patch

            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_env(
                        env_file
                    )
                )
        await application.registry.start_all()
        try:
            return (
                application.registry.require(ModelProviderPort),
                application.registry.require(RubricJudgePort),
            )
        finally:
            await application.registry.stop_all()

    async def test_absent_override_reuses_the_primary_provider(self) -> None:
        primary, judge = await self._judge_provider(review_model="")
        # Identity, not equality: no second instance means no second retry
        # counter or connection state -- the core flow is untouched.
        self.assertIs(judge._model, primary)

    async def test_override_builds_a_separate_provider_with_shared_credentials(
        self,
    ) -> None:
        primary, judge = await self._judge_provider(review_model="smaller-model")
        self.assertIsNot(judge._model, primary)
        # Same endpoint, same key, same protocol -- only the model name differs.
        # The primary registration wraps the physical provider in the resilient
        # layer, so compare the physical providers underneath.
        physical_primary = getattr(primary, "_provider", primary)
        physical_reviewer = getattr(judge._model, "_provider", judge._model)
        self.assertEqual(physical_reviewer._base_url, physical_primary._base_url)
        self.assertEqual(physical_reviewer._api_key, physical_primary._api_key)
        self.assertEqual(physical_reviewer._model, "smaller-model")
        self.assertEqual(physical_primary._model, "primary-model")


class ReadinessEventPayloadTest(unittest.TestCase):
    """SPEC §3.5 rule 6: the readiness event names the coverage contribution."""

    def payload(self, gaps):
        from tsm_agt.core.kernel import _readiness_event_payload
        from tsm_agt.ports import (
            CompletionReadinessAction, CompletionReadinessDecision,
            CompletionReadinessMode, CompletionReadinessProbe,
            CompletionReadinessState,
        )

        probe = CompletionReadinessProbe(
            goal="g", gaps=tuple(gaps), remaining_model_calls=5,
            remaining_tool_calls=5, available_read_tools=(),
            available_effects=frozenset({ToolEffect.OBSERVE}),
        )
        decision = CompletionReadinessDecision(
            CompletionReadinessAction.EXHAUSTED, "coverage_not_satisfied",
            CompletionReadinessState(), tuple(gaps),
        )
        return _readiness_event_payload(
            decision, probe, turn_id="t1",
            mode=CompletionReadinessMode.LEGACY_GATE, forced_wrap_up=False,
        )

    def coverage_gap(self):
        from tsm_agt.core.evidence_coverage import ObligationCoverage, coverage_gaps

        item = make_obligation(
            contract_ref="c1", predicate=ObligationPredicate.ARTIFACT_CONTENT,
            requirement_text="read the file", selector={"locator": "src/a.py"},
            required=True, required_capabilities=("ENUMERATE_SCOPE",),
        )
        plan = ProofPlanSnapshot.build(
            task_id="t1", task_spec_revision=1, obligations=(item,),
        )
        coverage = ObligationCoverage(
            item.obligation_id, CoverageStatus.BLOCKED, (), frozenset(), "R",
        )
        return coverage_gaps(plan=plan, coverages=(coverage,), tools=())[0]

    def ordinary_gap(self):
        from tsm_agt.ports import CompletionGap

        return CompletionGap(
            gap_id="task-spec:AC-9", kind="TASK_SPEC_WORKSPACE_INTEGRITY",
            description="workspace differs from the journal", status="FAILED",
            required=True, required_effects=(ToolEffect.MUTATE,),
        )

    def test_payload_separates_coverage_gaps_from_ordinary_gaps(self) -> None:
        coverage = self.coverage_gap()
        ordinary = self.ordinary_gap()
        payload = self.payload((ordinary, coverage))

        self.assertEqual(payload["coverage_gap_ids"], [coverage.gap_id])
        self.assertNotIn(ordinary.gap_id, payload["coverage_gap_ids"])
        # Both stay visible as ordinary gaps: the id list is a marker, not a
        # replacement for the gap payload.
        self.assertEqual(payload["gap_count"], 2)
        self.assertEqual(len(payload["gaps"]), 2)

    def test_payload_has_no_coverage_ids_outside_the_proof_gate(self) -> None:
        payload = self.payload((self.ordinary_gap(),))
        self.assertEqual(payload["coverage_gap_ids"], [])
        self.assertEqual(payload["gap_count"], 1)
