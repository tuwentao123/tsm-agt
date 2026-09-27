"""A contract that promises work cannot be closed by prose.

The Runtime derives one ``required-effect-delivery`` criterion from the Task
contract's declared side effects. These tests cover the criterion's authoring
(it is Runtime-owned and survives revision), the per-turn readiness gap it
raises, and its final-acceptance judgement for both ``mutate`` and ``execute``.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    TASK_SPEC_PROPOSAL_SCHEMA_V1,
    AcceptanceStatus,
    ApprovalDecision,
    ApprovalRequired,
    MutationOperation,
    MutationRecord,
    ProjectTrustLevel,
    TaskAcceptanceCriterion,
    TaskCriterionKind,
    TaskSpecProposal,
    TaskSpecSnapshot,
    TaskSnapshot,
    TaskState,
    ToolCommitState,
    ToolExecutionRecord,
)
from tsm_agt.core.kernel import (
    _DELIVERABLE_EFFECTS,
    _missing_effect_deliveries,
    _recorded_effect_deliveries,
    _required_effect_deliveries,
    _runtime_authored_criteria,
)
from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
    SandboxDecision, SandboxRequest, ToolCall, ToolEffect, ToolIdempotency,
    ToolResult, ToolRisk,
)


class _AllowProcessSandbox:
    """Permit the real command inside this test only."""

    descriptor = AdapterDescriptor(
        "fixture.completion-claim-sandbox", "1.0", "SandboxPort", "1.0",
        frozenset({"workspace-process"}),
    )

    async def start(self, context: AdapterContext) -> None:
        pass

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY)

    async def stop(self, deadline) -> None:
        pass

    async def authorize(self, request: SandboxRequest) -> SandboxDecision:
        return SandboxDecision(True, "fixture permits exact command")


def _proposal(goal: str, *, effects, kind="WORKSPACE_DELIVERY", criteria=None):
    return {
        "schema_version": 1,
        "goal": goal,
        "scope": ["."],
        "constraints": [],
        "acceptance_criteria": list(criteria) if criteria else [{
            "criterion_id": "ac-authored",
            "description": "the requested change exists",
            "verification_kind": "workspace_integrity",
        }],
        "outcomes": [{
            "outcome_id": "delivery",
            "description": "deliver the requested result",
            "kind": kind,
            "required_effects": list(effects),
            "required": True,
        }],
        "continuation_policy": {"mode": "NONE"},
    }


def _spec_snapshot(proposal):
    return TaskSpecSnapshot.from_proposal(
        "t", 1, TaskSpecProposal.from_data(proposal),
    )


class _Planner:
    """Fixture TaskSpecPlannerPort returning a fixed contract."""

    descriptor = AdapterDescriptor(
        "fixture.completion-claim-planner", "1.0", "TaskSpecPlannerPort", "1.0",
    )

    def __init__(self, proposal) -> None:
        self._proposal = proposal

    async def start(self, context) -> None:
        pass

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY, "ready")

    async def stop(self, deadline) -> None:
        pass

    async def propose_task_spec(self, goal, context):
        return {**self._proposal, "goal": goal}


async def _executing_task(app, root: Path, task_id: str, goal: str = "implement"):
    task = await app.kernel.create_task(goal, root, task_id)
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await app.kernel.transition_task(task.task_id, state, state.value)
    return task


async def _persist_final_answer(app, task_id: str, text: str = "Done.") -> None:
    await app.kernel._append_events(task_id, (
        ("llm.completed", {
            "turn_id": "turn-final",
            "message": {
                "message_id": "answer-final", "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
            "finish_reason": "stop",
        }),
        ("turn.completed", {"turn_id": "turn-final"}),
    ))


def _mutation(mutation_id, path, before, after, *, minute):
    return MutationRecord(
        mutation_id, path, MutationOperation.CREATE, before, after,
        f"step-{mutation_id}", None,
        datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc),
    )


def _execution(execution_id, effect, *, committed, ok, minute):
    call = ToolCall(f"call-{execution_id}", "core.run_command", {})
    return ToolExecutionRecord(
        execution_id=f"exec-{execution_id}", turn_id="turn-1",
        invocation_id=f"inv-{execution_id}", call=call, payload_hash="hash",
        policy_decision_id="policy", effective_risk=ToolRisk.R0,
        approval_request_id=None, idempotency=ToolIdempotency.IDEMPOTENT,
        idempotency_key=None,
        state=(ToolCommitState.COMMITTED if committed
               else ToolCommitState.RUNNING),
        result=ToolResult(
            call.call_id, ok, data={},
            error_code=None if ok else "EXIT_NONZERO",
        ),
        started_at=datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc),
        effect=effect,
    )


def _task(*, mutations=(), executions=()):
    return TaskSnapshot.from_data({
        "task_id": "t", "goal": "g", "workspace": "/tmp/w",
        "state": "EXECUTING",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "mutation_journal": [item.to_data() for item in mutations],
        "tool_executions": {
            item.execution_id: item.to_data() for item in executions
        },
    })


class DeliveryRecordTest(unittest.TestCase):
    """What counts as a durable record of a performed side effect."""

    def test_only_mutate_and_execute_are_deliverable_effects(self) -> None:
        self.assertEqual(
            _DELIVERABLE_EFFECTS,
            frozenset({ToolEffect.MUTATE, ToolEffect.EXECUTE}),
        )

    def test_observe_only_contract_requires_no_delivery(self) -> None:
        spec = _spec_snapshot(
            _proposal("explain", effects=["observe"], kind="EVIDENCE")
        )
        self.assertEqual(_required_effect_deliveries(spec), frozenset())

    def test_write_followed_by_rollback_delivers_nothing(self) -> None:
        task = _task(mutations=(
            _mutation("m1", "a.py", None, "hash-1", minute=0),
            _mutation("m2", "a.py", "hash-1", None, minute=1),
        ))
        self.assertEqual(_recorded_effect_deliveries(task), frozenset())

    def test_surviving_write_is_a_mutate_delivery(self) -> None:
        task = _task(mutations=(
            _mutation("m1", "a.py", None, "hash-1", minute=0),
        ))
        self.assertEqual(
            _recorded_effect_deliveries(task), frozenset({ToolEffect.MUTATE})
        )

    def test_committed_successful_run_is_an_execute_delivery(self) -> None:
        task = _task(executions=(
            _execution("run", ToolEffect.EXECUTE, committed=True, ok=True,
                       minute=0),
        ))
        self.assertEqual(
            _recorded_effect_deliveries(task), frozenset({ToolEffect.EXECUTE})
        )

    def test_failed_run_is_not_an_execute_delivery(self) -> None:
        task = _task(executions=(
            _execution("run", ToolEffect.EXECUTE, committed=True, ok=False,
                       minute=0),
        ))
        self.assertEqual(_recorded_effect_deliveries(task), frozenset())

    def test_uncommitted_run_is_not_an_execute_delivery(self) -> None:
        task = _task(executions=(
            _execution("run", ToolEffect.EXECUTE, committed=False, ok=True,
                       minute=0),
        ))
        self.assertEqual(_recorded_effect_deliveries(task), frozenset())

    def test_missing_deliveries_name_every_unmet_effect(self) -> None:
        spec = _spec_snapshot(_proposal("g", effects=["mutate", "execute"]))
        self.assertEqual(
            _missing_effect_deliveries(_task(), spec),
            (ToolEffect.EXECUTE, ToolEffect.MUTATE),
        )

    def test_runtime_criteria_are_derived_from_the_contract(self) -> None:
        proposal = TaskSpecProposal.from_data(
            _proposal("g", effects=["mutate", "execute"])
        )
        self.assertEqual(
            [item.verification_kind
             for item in _runtime_authored_criteria(proposal.outcomes, "g")],
            [TaskCriterionKind.REQUIRED_EFFECT],
        )


class DeliveryCriterionAuthoringTest(unittest.IsolatedAsyncioTestCase):
    """The Runtime gives itself the criterion; the Planner cannot touch it."""

    async def _planned(self, proposal, task_id, **kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        app = compose_fixture_application(
            task_spec_planner_adapter=_Planner(proposal), **kwargs
        )
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing_task(app, Path(temporary.name), task_id)
        spec = await app.kernel.plan_task_spec(task.task_id)
        return app, task, spec

    async def test_delivery_criterion_is_planned_for_declared_effects(self) -> None:
        _, _, spec = await self._planned(
            _proposal("implement", effects=["mutate", "execute"]), "gate-plan",
        )
        by_id = {item.criterion_id: item for item in spec.acceptance_criteria}
        self.assertIn("required-effect-delivery", by_id)
        criterion = by_id["required-effect-delivery"]
        self.assertIs(
            criterion.verification_kind, TaskCriterionKind.REQUIRED_EFFECT
        )
        self.assertIn("mutate", criterion.description)
        self.assertIn("execute", criterion.description)
        self.assertIn("ac-authored", by_id)

    async def test_observe_only_contract_gains_no_delivery_criterion(self) -> None:
        _, _, spec = await self._planned(
            _proposal("explain", effects=["observe"], kind="EVIDENCE"),
            "gate-observe",
        )
        self.assertFalse(any(
            item.verification_kind is TaskCriterionKind.REQUIRED_EFFECT
            for item in spec.acceptance_criteria
        ))

    async def test_planner_cannot_author_the_runtime_criterion(self) -> None:
        smuggled = [
            {
                "criterion_id": "required-effect-delivery",
                "description": "trust me, the work is done",
                "verification_kind": "required_effect",
            },
            {
                "criterion_id": "ac-authored",
                "description": "the requested change exists",
                "verification_kind": "workspace_integrity",
            },
        ]
        _, _, spec = await self._planned(
            _proposal("implement", effects=["mutate"], criteria=smuggled),
            "gate-smuggled",
        )
        delivery = [
            item for item in spec.acceptance_criteria
            if item.verification_kind is TaskCriterionKind.REQUIRED_EFFECT
        ]
        self.assertEqual(len(delivery), 1)
        self.assertNotIn("trust me", delivery[0].description)

    async def test_revision_rederives_the_runtime_criterion(self) -> None:
        app, task, _ = await self._planned(
            _proposal("implement", effects=["mutate"]), "gate-revise",
        )
        current = await app.kernel.get_task_spec(task.task_id)
        revised = await app.kernel.revise_task_spec(
            task.task_id, current.revision,
            scope=(".",), constraints=(),
            acceptance_criteria=(TaskAcceptanceCriterion(
                "ac-replacement", "a model-authored check",
                TaskCriterionKind.WORKSPACE_INTEGRITY,
            ),),
            operation_id="op-revise", writer="model",
        )
        ids = {item.criterion_id for item in revised.acceptance_criteria}
        self.assertIn("required-effect-delivery", ids)
        self.assertIn("ac-replacement", ids)

    async def test_revision_rejects_more_than_the_authored_budget(self) -> None:
        app, task, _ = await self._planned(
            _proposal("implement", effects=["mutate"]), "gate-budget",
        )
        current = await app.kernel.get_task_spec(task.task_id)
        criteria = tuple(
            TaskAcceptanceCriterion(
                f"ac-{index}", "check", TaskCriterionKind.WORKSPACE_INTEGRITY,
            )
            for index in range(30)
        )
        with self.assertRaises(ValueError):
            await app.kernel.revise_task_spec(
                task.task_id, current.revision,
                scope=(".",), constraints=(), acceptance_criteria=criteria,
                operation_id="op-too-many", writer="model",
            )

    def test_runtime_kinds_are_absent_from_the_planner_schema(self) -> None:
        enum = TASK_SPEC_PROPOSAL_SCHEMA_V1["properties"][
            "acceptance_criteria"
        ]["items"]["properties"]["verification_kind"]["enum"]
        self.assertNotIn("required_effect", enum)
        self.assertNotIn("goal_alignment", enum)
        self.assertIn("workspace_integrity", enum)

    def test_authored_budget_reserves_both_runtime_criteria(self) -> None:
        criteria = [
            {
                "criterion_id": f"ac-{index}",
                "description": "check",
                "verification_kind": "workspace_integrity",
            }
            for index in range(29)
        ]
        with self.assertRaises(ValueError):
            TaskSpecProposal.from_data(
                _proposal("g", effects=["mutate"], criteria=criteria)
            )

    async def test_answer_and_delivery_criteria_coexist(self) -> None:
        proposal = _proposal("implement and explain", effects=["mutate"])
        proposal["outcomes"].append({
            "outcome_id": "answer",
            "description": "explain the change",
            "kind": "ANSWER",
            "required_effects": [],
            "required": True,
        })
        _, _, spec = await self._planned(proposal, "gate-answer-delivery")
        kinds = [item.verification_kind for item in spec.acceptance_criteria]
        self.assertIn(TaskCriterionKind.GOAL_ALIGNMENT, kinds)
        self.assertIn(TaskCriterionKind.REQUIRED_EFFECT, kinds)
        self.assertLessEqual(len(spec.acceptance_criteria), 30)


class DeliveryGapTest(unittest.IsolatedAsyncioTestCase):
    """The per-turn gate keeps working instead of wrapping up on a claim."""

    async def _gaps(self, proposal, task_id):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        app = compose_fixture_application(
            tool_adapters=(), task_spec_planner_adapter=_Planner(proposal),
        )
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing_task(app, Path(temporary.name), task_id)
        await app.kernel.plan_task_spec(task.task_id)
        return await app.kernel._completion_readiness_gaps(task.task_id, ())

    async def test_mutate_contract_without_change_is_a_required_gap(self) -> None:
        gaps = await self._gaps(
            _proposal("implement", effects=["mutate"]), "gate-gap-mutate",
        )
        delivery = [
            gap for gap in gaps if gap.kind == "REQUIRED_DELIVERY_UNSATISFIED"
        ]
        self.assertEqual(len(delivery), 1)
        self.assertTrue(delivery[0].required)
        self.assertEqual(delivery[0].required_effects, (ToolEffect.MUTATE,))

    async def test_execute_contract_without_a_run_is_a_required_gap(self) -> None:
        gaps = await self._gaps(
            _proposal("run the tests", effects=["execute"]), "gate-gap-execute",
        )
        delivery = [
            gap for gap in gaps if gap.kind == "REQUIRED_DELIVERY_UNSATISFIED"
        ]
        self.assertEqual(len(delivery), 1)
        self.assertEqual(delivery[0].required_effects, (ToolEffect.EXECUTE,))

    async def test_observe_only_contract_is_not_gated(self) -> None:
        gaps = await self._gaps(
            _proposal("explain", effects=["observe"], kind="EVIDENCE"),
            "gate-gap-observe",
        )
        self.assertFalse(any(
            gap.kind == "REQUIRED_DELIVERY_UNSATISFIED" for gap in gaps
        ))


class DeliveryAcceptanceTest(unittest.IsolatedAsyncioTestCase):
    """Final acceptance: the claim fails, honest criteria keep their meaning."""

    async def _app(self, proposal, **kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        app = compose_fixture_application(
            task_spec_planner_adapter=_Planner(proposal), **kwargs
        )
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        root = Path(temporary.name)
        await app.kernel.set_project_trust(root, ProjectTrustLevel.TRUSTED_BUILD)
        return app, root

    async def _verify(self, task_id, app, root):
        await _persist_final_answer(app, task_id)
        await app.kernel.transition_task(task_id, TaskState.VERIFYING, "verify")
        return await app.kernel.verify_task_acceptance(task_id)

    async def test_missing_workspace_delivery_fails_acceptance(self) -> None:
        app, root = await self._app(_proposal("implement", effects=["mutate"]))
        task = await _executing_task(app, root, "gate-fail-mutate")
        await app.kernel.plan_task_spec(task.task_id)
        verification = await self._verify(task.task_id, app, root)
        by_id = {item.criterion_id: item for item in verification.criteria}
        self.assertEqual(
            by_id["required-effect-delivery"].status, AcceptanceStatus.FAILED
        )
        # Integrity keeps its own honest meaning: an empty journal matches an
        # unchanged disk, so it is not the criterion that fails.
        self.assertEqual(by_id["ac-authored"].status, AcceptanceStatus.PASSED)
        self.assertEqual(verification.status, AcceptanceStatus.FAILED)

    async def test_missing_command_delivery_fails_acceptance(self) -> None:
        app, root = await self._app(_proposal("run the tests", effects=["execute"]))
        task = await _executing_task(app, root, "gate-fail-execute")
        await app.kernel.plan_task_spec(task.task_id)
        verification = await self._verify(task.task_id, app, root)
        by_id = {item.criterion_id: item for item in verification.criteria}
        self.assertEqual(
            by_id["required-effect-delivery"].status, AcceptanceStatus.FAILED
        )
        self.assertEqual(verification.status, AcceptanceStatus.FAILED)

    async def test_executed_command_satisfies_the_delivery_criterion(self) -> None:
        app, root = await self._app(
            _proposal("run the tests", effects=["execute"]),
            tool_adapters=(CoreProcessToolProvider(),),
            sandbox_adapter=_AllowProcessSandbox(),
        )
        task = await _executing_task(app, root, "gate-pass-execute")
        await app.kernel.plan_task_spec(task.task_id)
        try:
            result = await app.kernel.invoke_tool(
                task.task_id, "turn-direct",
                ToolCall("call-direct", "core.run_command", {
                    "argv": [sys.executable, "-c", "print('ok')"],
                }),
            )
        except ApprovalRequired as suspended:
            request = suspended.request
            result = await app.kernel.resolve_approval(
                task.task_id, request.request_id, request.payload_hash,
                ApprovalDecision.APPROVE, "approve exact direct command",
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["exit_code"], 0)

        verification = await self._verify(task.task_id, app, root)
        by_id = {item.criterion_id: item for item in verification.criteria}
        self.assertEqual(
            by_id["required-effect-delivery"].status, AcceptanceStatus.PASSED
        )
        self.assertEqual(verification.status, AcceptanceStatus.PASSED)
