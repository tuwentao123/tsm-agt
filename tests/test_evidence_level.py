from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.rule_based_evidence_level import (
    RuleBasedEvidenceLevelEvaluator,
)
from tsm_agt.adapters.structured_evidence import StructuredEvidenceDeltaEvaluator
from tsm_agt.bootstrap import (
    compose_fixture_application, compose_openai_compatible_readonly_application,
)
from tsm_agt.cli import _render_evidence_level
from tsm_agt.core import (
    AgentTurnResult, TaskAcceptanceCriterion, TaskCriterionKind, TaskState,
)
from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, EvidenceLevel, EvidenceLevelEvaluatorPort,
    EvidenceLevelSignals, FinishReason, HealthState, HealthStatus, Message,
    MessageRole, ModelRequest, ModelResponse, ProviderCapabilities, RuntimeStorePort,
    TextBlock, ToolCall, ToolCallBlock, ToolIdempotency, ToolResult, ToolResultBlock,
    ToolRisk, ToolSpec, EvidenceQuestion,
)


class EvidenceFixtureTool:
    descriptor = AdapterDescriptor(
        "fixture.evidence-level-tool", "1", "ToolProviderPort", "1"
    )

    async def start(self, context): pass
    async def stop(self, deadline): pass
    async def health(self): return HealthStatus(HealthState.HEALTHY)
    async def list_tools(self):
        return (ToolSpec(
            "fixture.inspect", "Inspect structured fact",
            {"type": "object", "properties": {},
             "additionalProperties": False},
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
        ),)
    async def invoke(self, call, context):
        return ToolResult(call.call_id, True, {"answer": 42})


class EvidenceFixtureModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if any(
            isinstance(block, ToolResultBlock)
            for message in request.messages for block in message.content
        ):
            return ModelResponse(Message(
                "evidence-final", MessageRole.ASSISTANT,
                (TextBlock("answer from evidence"),),
            ))
        return ModelResponse(Message(
            "evidence-call", MessageRole.ASSISTANT,
            (ToolCallBlock(ToolCall(
                "inspect-evidence", "fixture.inspect", {},
                EvidenceQuestion("Q-evidence-level", "What was observed?"),
            )),),
        ), FinishReason.TOOL_CALL)


async def execute_turn(app, root: Path, task_id: str):
    task = await app.kernel.create_task("inspect evidence", root, task_id)
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await app.kernel.transition_task(task.task_id, state, state.value)
    result = await app.kernel.run_agent_turn(task.task_id, "inspect")
    assert isinstance(result, AgentTurnResult)
    return task, result


class RuleBasedEvidenceLevelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.evaluator = RuleBasedEvidenceLevelEvaluator()
        await self.evaluator.start(AdapterContext(config={}, emit_event=lambda *_: None))

    async def asyncTearDown(self):
        from datetime import datetime
        await self.evaluator.stop(datetime.now())

    async def test_conservative_level_order(self):
        cases = (
            (EvidenceLevelSignals({}, 0, "passed"), EvidenceLevel.NONE),
            (EvidenceLevelSignals({"new_paths": 2}, 1, "passed"), EvidenceLevel.INDIRECT),
            (EvidenceLevelSignals({"new_facts": 1}, 1, "passed"), EvidenceLevel.DIRECT),
            (EvidenceLevelSignals({"new_facts": 1}, 1, "passed", 1), EvidenceLevel.VERIFIED),
            (EvidenceLevelSignals({"new_facts": 1}, 1, "blocked", blocked_criteria=1), EvidenceLevel.BLOCKED),
        )
        for signals, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual((await self.evaluator.assess(signals)).level, expected)

    async def test_cli_renderer_is_explicit(self):
        assessment = await self.evaluator.assess(EvidenceLevelSignals(
            {"new_facts": 3}, 1, "passed"
        ))
        rendered = _render_evidence_level(assessment)
        self.assertIn("evidence: direct", rendered)
        self.assertIn("直接证据", rendered)
        self.assertIn("items=3", rendered)


class EvidenceLevelKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_fact_is_direct_and_persisted_without_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=EvidenceFixtureModel(),
                tool_adapters=(EvidenceFixtureTool(),),
                require_evidence_questions=True,
                evidence_delta_evaluator_adapter=StructuredEvidenceDeltaEvaluator(),
                evidence_level_evaluator_adapter=RuleBasedEvidenceLevelEvaluator(),
            )
            await app.registry.start_all()
            try:
                task, _ = await execute_turn(app, Path(directory), "task-level-direct")
                await app.kernel.transition_task(task.task_id, TaskState.VERIFYING, "verify")
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(verification.evidence_level.level, EvidenceLevel.DIRECT)
                events = await app.registry.require(RuntimeStorePort).read_events(task.task_id)
                levels = [event.payload for event in events if event.event_type == "evidence.level_assessed"]
                self.assertEqual(levels[0]["level"], "direct")
                self.assertNotIn("answer", str(levels))
                self.assertNotIn("Q-evidence-level", str(levels))
            finally:
                await app.registry.stop_all()

    async def test_trusted_evidence_reference_upgrades_to_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=EvidenceFixtureModel(),
                tool_adapters=(EvidenceFixtureTool(),),
                require_evidence_questions=True,
                evidence_delta_evaluator_adapter=StructuredEvidenceDeltaEvaluator(),
                evidence_level_evaluator_adapter=RuleBasedEvidenceLevelEvaluator(),
            )
            await app.registry.start_all()
            try:
                task, _ = await execute_turn(app, Path(directory), "task-level-verified")
                criterion = TaskAcceptanceCriterion(
                    "fact-proof", "successful observation exists",
                    TaskCriterionKind.EVIDENCE_REFERENCE,
                    "tool_call:inspect-evidence",
                )
                await app.kernel.revise_task_spec(
                    task.task_id, 1, scope=(), constraints=(),
                    acceptance_criteria=(criterion,), operation_id="level-spec",
                    writer="test",
                )
                await app.kernel.transition_task(task.task_id, TaskState.VERIFYING, "verify")
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(verification.evidence_level.level, EvidenceLevel.VERIFIED)
                self.assertEqual(verification.evidence_level.verified_criteria, 1)
            finally:
                await app.registry.stop_all()

    async def test_missing_reference_is_blocked_not_direct(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                tool_adapters=(),
                evidence_level_evaluator_adapter=RuleBasedEvidenceLevelEvaluator(),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("missing evidence", Path(directory))
                for state in (TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                              TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                              TaskState.EXECUTING):
                    task = await app.kernel.transition_task(task.task_id, state, state.value)
                criterion = TaskAcceptanceCriterion(
                    "missing", "required evidence exists",
                    TaskCriterionKind.EVIDENCE_REFERENCE, "event:999999",
                )
                await app.kernel.revise_task_spec(
                    task.task_id, 1, scope=(), constraints=(),
                    acceptance_criteria=(criterion,), operation_id="missing-level",
                    writer="test",
                )
                await app.kernel.transition_task(task.task_id, TaskState.VERIFYING, "verify")
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(verification.evidence_level.level, EvidenceLevel.BLOCKED)
            finally:
                await app.registry.stop_all()


class EvidenceLevelCompositionTest(unittest.TestCase):
    def test_fixture_is_optional_replaceable_and_real_composition_has_default(self):
        self.assertEqual(
            compose_fixture_application().registry.all(EvidenceLevelEvaluatorPort), ()
        )
        evaluator = RuleBasedEvidenceLevelEvaluator()
        fixture = compose_fixture_application(evidence_level_evaluator_adapter=evaluator)
        self.assertIs(fixture.registry.require(EvidenceLevelEvaluatorPort), evaluator)
        real = compose_openai_compatible_readonly_application(
            base_url="https://example.test/v1", model="model", api_key="key"
        )
        self.assertIsInstance(
            real.registry.require(EvidenceLevelEvaluatorPort),
            RuleBasedEvidenceLevelEvaluator,
        )


if __name__ == "__main__":
    unittest.main()
