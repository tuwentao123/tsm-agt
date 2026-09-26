from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tsm_agt.core.execution import ToolCommitState, ToolExecutionRecord
from tsm_agt.core.execution_facts import (
    EffectStatus,
    FactResolution,
    project_execution_fact,
    project_execution_facts,
    project_unresolved_failures,
)
from tsm_agt.ports import (
    ToolCall, ToolEffect, ToolIdempotency, ToolResult, ToolResultAuthority,
    ToolRisk, ToolSpec,
)


def _record(
    call_id: str = "call-1",
    *,
    name: str = "core.run_command",
    turn_id: str = "turn-1",
    payload_hash: str = "hash-1",
    state: ToolCommitState = ToolCommitState.COMMITTED,
    result: ToolResult | None = None,
    effect: ToolEffect = ToolEffect.UNSPECIFIED,
    authority: ToolResultAuthority = ToolResultAuthority.UNSPECIFIED,
    updated_at: datetime | None = None,
) -> ToolExecutionRecord:
    record = ToolExecutionRecord.start(
        task_id="task-1", turn_id=turn_id, invocation_id=f"inv-{call_id}",
        call=ToolCall(call_id, name, {}), payload_hash=payload_hash,
        policy_decision_id="policy-1", effective_risk=ToolRisk.R0,
        approval_request_id=None, idempotency=ToolIdempotency.IDEMPOTENT,
        effect=effect, result_authority=authority,
    )
    if result is not None:
        record = record.finish(state, result)
    elif state is not ToolCommitState.PREPARED:
        record = record.finish(state, ToolResult(call_id, True))
    if updated_at is not None:
        record = record.__class__.from_data(
            {**record.to_data(), "updated_at": updated_at.isoformat()}
        )
    return record


class ExecutionFactProjectionTest(unittest.TestCase):
    def test_observe_effect_produces_no_fact(self) -> None:
        record = _record(
            result=ToolResult("call-1", True),
            effect=ToolEffect.OBSERVE,
        )
        self.assertIsNone(project_execution_fact(record))

    def test_internal_effect_produces_no_fact(self) -> None:
        record = _record(
            result=ToolResult("call-1", True),
            effect=ToolEffect.INTERNAL,
        )
        self.assertIsNone(project_execution_fact(record))

    def test_process_success_is_succeeded(self) -> None:
        record = _record(
            result=ToolResult("call-1", True, data={
                "status": "exited", "succeeded": True, "exit_code": 0,
            }),
            effect=ToolEffect.EXECUTE,
            authority=ToolResultAuthority.PROCESS_FACT,
        )
        fact = project_execution_fact(record)
        self.assertIsNotNone(fact)
        self.assertIs(fact.effect_status, EffectStatus.SUCCEEDED)

    def test_process_business_failure_is_failed(self) -> None:
        # The exact defect shape: ok=True while the process failed.
        record = _record(
            result=ToolResult("call-1", True, data={
                "status": "exited", "succeeded": False,
                "failure_code": "PROCESS_EXIT_NON_ZERO",
            }),
            effect=ToolEffect.EXECUTE,
            authority=ToolResultAuthority.PROCESS_FACT,
        )
        fact = project_execution_fact(record)
        self.assertIs(fact.effect_status, EffectStatus.FAILED)
        self.assertEqual(fact.failure_code, "PROCESS_EXIT_NON_ZERO")
        self.assertTrue(fact.is_blocking_failure)

    def test_cancelled_process_is_unknown_and_not_blocking(self) -> None:
        record = _record(
            result=ToolResult("call-1", False, error_code="CANCELLED", data={
                "status": "cancelled", "succeeded": False,
                "failure_code": "PROCESS_CANCELLED",
            }),
            effect=ToolEffect.EXECUTE,
            authority=ToolResultAuthority.PROCESS_FACT,
        )
        fact = project_execution_fact(record)
        self.assertIs(fact.effect_status, EffectStatus.UNKNOWN)
        self.assertFalse(fact.is_blocking_failure)

    def test_mutation_without_a_recorded_mutation_is_unknown(self) -> None:
        record = _record(
            result=ToolResult("call-1", True, data={"path": "a.py"}),
            effect=ToolEffect.MUTATE,
            authority=ToolResultAuthority.MUTATION_FACT,
        )
        fact = project_execution_fact(record, known_mutation_ids=frozenset())
        self.assertIs(fact.effect_status, EffectStatus.UNKNOWN)
        self.assertTrue(fact.is_blocking_failure)

    def test_recorded_mutation_is_succeeded(self) -> None:
        record = _record(
            result=ToolResult("call-1", True, data={"mutation_id": "m-1"}),
            effect=ToolEffect.MUTATE,
            authority=ToolResultAuthority.MUTATION_FACT,
        )
        fact = project_execution_fact(
            record, known_mutation_ids=frozenset({"m-1"})
        )
        self.assertIs(fact.effect_status, EffectStatus.SUCCEEDED)

    def test_tool_failure_is_failed_regardless_of_authority(self) -> None:
        record = _record(
            result=ToolResult("call-1", False, error_code="TOOL_FAILED"),
            effect=ToolEffect.EXECUTE,
            authority=ToolResultAuthority.PROCESS_FACT,
        )
        fact = project_execution_fact(record)
        self.assertIs(fact.effect_status, EffectStatus.FAILED)

    def test_unknown_outcome_is_unknown_and_blocking(self) -> None:
        record = _record(
            state=ToolCommitState.UNKNOWN_OUTCOME,
            result=ToolResult("call-1", True),
            effect=ToolEffect.EXECUTE,
            authority=ToolResultAuthority.PROCESS_FACT,
        )
        fact = project_execution_fact(record)
        self.assertIs(fact.effect_status, EffectStatus.UNKNOWN)
        self.assertEqual(fact.failure_code, "UNKNOWN_OUTCOME")
        self.assertTrue(fact.is_blocking_failure)

    def test_cancelled_commit_state_is_unknown(self) -> None:
        record = _record(
            state=ToolCommitState.CANCELLED,
            result=ToolResult("call-1", True),
            effect=ToolEffect.EXECUTE,
            authority=ToolResultAuthority.PROCESS_FACT,
        )
        fact = project_execution_fact(record)
        self.assertIs(fact.effect_status, EffectStatus.UNKNOWN)
        self.assertFalse(fact.is_blocking_failure)

    def test_non_terminal_execution_produces_no_fact(self) -> None:
        record = _record(
            state=ToolCommitState.PREPARED,
            effect=ToolEffect.EXECUTE,
            authority=ToolResultAuthority.PROCESS_FACT,
        )
        self.assertIsNone(project_execution_fact(record))

    def test_spec_is_used_when_the_record_has_no_frozen_semantics(self) -> None:
        record = _record(result=ToolResult("call-1", True, data={
            "status": "exited", "succeeded": False, "exit_code": 2,
        }))
        spec = ToolSpec(
            "core.run_command", "run a command",
            {"type": "object", "properties": {}}, ToolRisk.R0, False, False,
            ToolIdempotency.IDEMPOTENT, effect=ToolEffect.EXECUTE,
            result_authority=ToolResultAuthority.PROCESS_FACT,
        )
        fact = project_execution_fact(record, spec)
        self.assertIs(fact.effect_status, EffectStatus.FAILED)


class ExecutionFactResolutionTest(unittest.TestCase):
    def _spec(self) -> dict[str, ToolSpec]:
        return {
            "core.run_command": ToolSpec(
                "core.run_command", "run a command",
                {"type": "object", "properties": {}}, ToolRisk.R0, False,
                False, ToolIdempotency.IDEMPOTENT,
                effect=ToolEffect.EXECUTE,
                result_authority=ToolResultAuthority.PROCESS_FACT,
            ),
        }

    def _failed(self, call_id: str, *, at: datetime, payload: str = "hash-1"):
        return _record(
            call_id, payload_hash=payload, updated_at=at,
            result=ToolResult(call_id, True, data={
                "status": "exited", "succeeded": False,
                "failure_code": "PROCESS_EXIT_NON_ZERO",
            }),
        )

    def _succeeded(self, call_id: str, *, at: datetime, payload: str = "hash-1"):
        return _record(
            call_id, payload_hash=payload, updated_at=at,
            result=ToolResult(call_id, True, data={
                "status": "exited", "succeeded": True, "exit_code": 0,
            }),
        )

    def test_no_later_action_leaves_the_failure_unresolved(self) -> None:
        base = datetime(2026, 9, 26, tzinfo=timezone.utc)
        facts = project_unresolved_failures(
            [self._failed("c1", at=base)], self._spec()
        )
        self.assertEqual(len(facts), 1)
        self.assertIs(facts[0].resolution, FactResolution.UNRESOLVED)

    def test_same_signature_later_success_is_retried(self) -> None:
        base = datetime(2026, 9, 26, tzinfo=timezone.utc)
        facts = project_unresolved_failures(
            [
                self._failed("c1", at=base),
                self._succeeded("c2", at=base + timedelta(minutes=1)),
            ],
            self._spec(),
        )
        self.assertEqual(facts, ())
        all_facts = project_execution_facts(
            [
                self._failed("c1", at=base),
                self._succeeded("c2", at=base + timedelta(minutes=1)),
            ],
            self._spec(),
        )
        self.assertIs(all_facts[0].resolution, FactResolution.RETRIED)

    def test_different_action_of_same_effect_is_superseded(self) -> None:
        base = datetime(2026, 9, 26, tzinfo=timezone.utc)
        specs = self._spec()
        specs["core.process_signal"] = ToolSpec(
            "core.process_signal", "signal a process",
            {"type": "object", "properties": {}}, ToolRisk.R0, False, False,
            ToolIdempotency.IDEMPOTENT, effect=ToolEffect.EXECUTE,
            result_authority=ToolResultAuthority.PROCESS_FACT,
        )
        facts = project_execution_facts(
            [
                self._failed("c1", at=base),
                _record(
                    "c2", name="core.process_signal", payload_hash="hash-2",
                    updated_at=base + timedelta(minutes=1),
                    result=ToolResult("c2", True, data={
                        "status": "exited", "succeeded": True, "exit_code": 0,
                    }),
                ),
            ],
            specs,
        )
        self.assertEqual(len(facts), 1)
        self.assertIs(facts[0].resolution, FactResolution.SUPERSEDED)

    def test_ordering_is_deterministic_by_timestamp_then_id(self) -> None:
        base = datetime(2026, 9, 26, tzinfo=timezone.utc)
        first = project_unresolved_failures(
            [
                self._failed("c2", at=base),
                self._failed("c1", at=base),
            ],
            self._spec(),
        )
        second = project_unresolved_failures(
            [
                self._failed("c1", at=base),
                self._failed("c2", at=base),
            ],
            self._spec(),
        )
        self.assertEqual(
            [fact.execution_id for fact in first],
            [fact.execution_id for fact in second],
        )


if __name__ == "__main__":
    unittest.main()
