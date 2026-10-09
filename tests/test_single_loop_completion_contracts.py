from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tsm_agt.adapters.rule_based_evidence_gate import RuleBasedEvidenceGate
from tsm_agt.core import (
    MutationOperation,
    MutationRecord,
    TaskSnapshot,
    ToolCommitState,
    ToolExecutionRecord,
)
from tsm_agt.core.single_loop import (
    IntentDependency,
    IntentDependencyKind,
    TaskBindingKind,
    TaskOrchestrationMetadata,
    TurnIntent,
    TurnIntentKind,
    validate_completion,
)
from tsm_agt.ports import (
    Message,
    MessageRole,
    RuntimeEvent,
    TextBlock,
    ToolCall,
    ToolEffect,
    ToolIdempotency,
    ToolResult,
    ToolResultAuthority,
    ToolRisk,
    ToolSpec,
)
from tsm_agt.ports.evidence import (
    ContractAuthority,
    EvidenceFact,
    EvidenceRequirement,
    TaskContract,
)


class SingleLoopCompletionContractsTest(unittest.TestCase):
    def _metadata(self, goal: str = "answer") -> TaskOrchestrationMetadata:
        return TaskOrchestrationMetadata(
            schema_version=1,
            orchestration_mode="SINGLE_LOOP",
            root_goal=goal,
            root_input_event_ref="session-event:input-1",
            latest_input_event_ref="session-event:input-1",
            binding_kind=TaskBindingKind.NEW,
        )

    def _task(self, *, mutations=(), executions=()) -> TaskSnapshot:
        task = TaskSnapshot.create("task-current", "answer", "/tmp/work")
        for mutation in mutations:
            task = task.with_mutation(mutation)
        for execution in executions:
            task = task.with_tool_execution(execution)
        return task

    def _intent_event(self, intent: TurnIntent) -> RuntimeEvent:
        return RuntimeEvent("intent-1", "task-current", 2, "turn.intent_declared", intent.to_data())

    def _contract_event(self, contract: TaskContract) -> RuntimeEvent:
        return RuntimeEvent(
            "created-1", "task-current", 1, "task.created",
            {"evidence_contract": contract.to_data()},
        )

    def _execution(
        self,
        *,
        effect: ToolEffect,
        name: str = "fixture.tool",
        call_id: str = "call-1",
        arguments=None,
        data=None,
        authority: ToolResultAuthority = ToolResultAuthority.UNSPECIFIED,
        evidence_facts=(),
    ) -> ToolExecutionRecord:
        call = ToolCall(call_id, name, arguments or {})
        result = ToolResult(
            call.call_id,
            True,
            data=data or {},
            evidence_facts=evidence_facts,
        )
        return ToolExecutionRecord.start(
            task_id="task-current",
            turn_id="turn-1",
            invocation_id="inv-1",
            call=call,
            payload_hash="payload",
            policy_decision_id="policy",
            effective_risk=ToolRisk.R0,
            approval_request_id=None,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=effect,
            result_authority=authority,
        ).finish(ToolCommitState.COMMITTED, result)

    @staticmethod
    def _assistant() -> Message:
        return Message("assistant-1", MessageRole.ASSISTANT, (TextBlock("done"),))

    @staticmethod
    def _capability_tool(capability: str) -> ToolSpec:
        return ToolSpec(
            name="fixture.evidence",
            description="Produce typed test evidence.",
            parameters={"type": "object", "properties": {}},
            risk=ToolRisk.R0,
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.EXTERNAL_SERVICE,
            evidence_capability_ids=frozenset({capability}),
        )

    def _validate(
        self,
        *,
        intent: TurnIntent | None = None,
        goal: str = "answer",
        contract: TaskContract | None = None,
        visible_tools=(),
        **kwargs,
    ):
        events = ()
        if contract is not None:
            events += (self._contract_event(contract),)
        if intent is not None:
            events += (self._intent_event(intent),)
        return validate_completion(
            task=self._task(**kwargs),
            metadata=self._metadata(goal),
            events=events,
            turn_id="turn-1",
            assistant_message=self._assistant(),
            visible_tools=visible_tools,
            evidence_gate=RuleBasedEvidenceGate(),
        )

    @staticmethod
    def _source_contract(authority: ContractAuthority = ContractAuthority.USER) -> TaskContract:
        return TaskContract(
            objective="compare implementations",
            authority=authority,
            evidence_requirements=(EvidenceRequirement(
                "source-files",
                "source.artifact",
                minimum_count=2,
                distinct_by="repository",
                predicates={"immutable_revision": True, "complete": True},
            ),),
        )

    @staticmethod
    def _source_fact(call_id: str, repository: str) -> EvidenceFact:
        return EvidenceFact(
            fact_id=f"{call_id}:source.artifact",
            kind="source.artifact",
            execution_ref=f"tool_call:{call_id}",
            attributes={
                "repository": repository,
                "immutable_revision": True,
                "complete": True,
            },
            provenance={"provider": "fixture"},
            complete=True,
        )

    def test_user_contract_blocks_when_source_capability_is_unavailable(self) -> None:
        validation = self._validate(contract=self._source_contract())

        self.assertFalse(validation.complete)
        self.assertEqual(validation.gate_decision.resolution.value, "BLOCKED")
        self.assertEqual(validation.gate_decision.reason_code, "EVIDENCE_CAPABILITY_UNAVAILABLE")
        self.assertFalse(validation.gaps[0].recoverable)

    def test_user_contract_continues_only_with_exact_capability(self) -> None:
        validation = self._validate(
            contract=self._source_contract(),
            visible_tools=(self._capability_tool("source.artifact"),),
        )

        self.assertFalse(validation.complete)
        self.assertEqual(validation.gate_decision.resolution.value, "CONTINUE")
        self.assertEqual(validation.gaps[0].required_capability_ids, ("source.artifact",))
        self.assertEqual(validation.gaps[0].required_effects, ())

    def test_user_contract_passes_with_distinct_complete_source_facts(self) -> None:
        first = self._execution(
            effect=ToolEffect.OBSERVE,
            call_id="first",
            evidence_facts=(self._source_fact("first", "owner/one"),),
        )
        second = self._execution(
            effect=ToolEffect.OBSERVE,
            call_id="second",
            evidence_facts=(self._source_fact("second", "owner/two"),),
        )

        validation = self._validate(
            contract=self._source_contract(),
            executions=(first, second),
            visible_tools=(self._capability_tool("source.artifact"),),
        )

        self.assertTrue(validation.complete)
        self.assertEqual(validation.gate_decision.reason_code, "REQUIREMENTS_SATISFIED")

    def test_draft_contract_never_becomes_mandatory(self) -> None:
        validation = self._validate(contract=self._source_contract(ContractAuthority.DRAFT))

        self.assertTrue(validation.complete)
        self.assertEqual(validation.gate_decision.reason_code, "NO_MANDATORY_CONTRACT")

    def test_web_discovery_cannot_satisfy_source_requirement(self) -> None:
        discovery = EvidenceFact(
            "search:web.discovery",
            "web.discovery",
            "tool_call:search",
            {"candidates": [{"url": "https://example.test"}]},
            {"provider": "fixture"},
            True,
        )
        execution = self._execution(
            effect=ToolEffect.OBSERVE,
            name="web.search",
            evidence_facts=(discovery,),
        )

        validation = self._validate(
            contract=self._source_contract(),
            executions=(execution,),
            visible_tools=(self._capability_tool("source.artifact"),),
        )

        self.assertEqual(validation.gate_decision.resolution.value, "CONTINUE")

    def test_truncated_result_cannot_claim_complete_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "truncated tool result"):
            ToolResult(
                "truncated-call",
                True,
                truncated=True,
                evidence_facts=(self._source_fact("truncated", "owner/repo"),),
            )

    def test_contract_rejects_removed_gate_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "gates are not supported"):
            TaskContract.from_data({
                "objective": "prove source",
                "authority": "USER",
                "gates": [{"gate_id": "unused", "requirement_ids": []}],
            })

    def test_modify_requires_a_net_mutation(self) -> None:
        intent = TurnIntent(1, "turn-1", TurnIntentKind.MODIFY, (ToolEffect.MUTATE,))

        validation = self._validate(intent=intent)

        self.assertEqual([gap.code for gap in validation.gaps], ["NET_MUTATION_MISSING"])

    def test_runtime_anchor_requires_matching_inspection_evidence(self) -> None:
        intent = TurnIntent(1, "turn-1", TurnIntentKind.INVESTIGATE)

        validation = self._validate(intent=intent, goal="inspect task-id: task-target")

        self.assertEqual([gap.code for gap in validation.gaps], ["RUNTIME_ANCHOR_UNVERIFIED"])

    def test_source_dependency_requires_observe_evidence(self) -> None:
        intent = TurnIntent(
            1,
            "turn-1",
            TurnIntentKind.INVESTIGATE,
            dependencies=(IntentDependency(IntentDependencyKind.WORKSPACE_SOURCE, "src"),),
        )

        validation = self._validate(intent=intent)

        self.assertEqual([gap.code for gap in validation.gaps], ["SOURCE_OBSERVATION_MISSING"])

    def test_direct_response_without_anchor_or_effect_completes(self) -> None:
        intent = TurnIntent(1, "turn-1", TurnIntentKind.DIRECT_RESPONSE)

        validation = self._validate(intent=intent)

        self.assertTrue(validation.complete)
        self.assertEqual(validation.claim.claimed_effects, ())

    def test_direct_response_with_explicit_anchor_requires_inspection(self) -> None:
        intent = TurnIntent(1, "turn-1", TurnIntentKind.DIRECT_RESPONSE)

        validation = self._validate(intent=intent, goal="answer task-id: task-target")

        self.assertEqual([gap.code for gap in validation.gaps], ["RUNTIME_ANCHOR_UNVERIFIED"])

    def test_matching_runtime_inspection_closes_anchor_gap(self) -> None:
        intent = TurnIntent(1, "turn-1", TurnIntentKind.INVESTIGATE)
        execution = self._execution(
            effect=ToolEffect.OBSERVE,
            name="runtime.inspect_task",
            arguments={"task_id": "task-target"},
            data={"task_id": "task-target"},
            authority=ToolResultAuthority.RUNTIME_FACT,
        )

        validation = self._validate(
            intent=intent,
            goal="inspect task-id: task-target",
            executions=(execution,),
        )

        self.assertTrue(validation.complete)

    def test_verify_requires_successful_execute_evidence(self) -> None:
        intent = TurnIntent(1, "turn-1", TurnIntentKind.VERIFY, (ToolEffect.EXECUTE,))

        validation = self._validate(intent=intent)

        self.assertEqual([gap.code for gap in validation.gaps], ["SUCCESSFUL_EXECUTION_MISSING"])


if __name__ == "__main__":
    unittest.main()
