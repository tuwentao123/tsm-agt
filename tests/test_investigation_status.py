from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.rule_based_agent_progress import RuleBasedAgentProgressProjector
from tsm_agt.adapters.rule_based_evidence_level import RuleBasedEvidenceLevelEvaluator
from tsm_agt.adapters.rule_based_exploration_budget import RuleBasedExplorationBudgetPolicy
from tsm_agt.adapters.rule_based_investigation_status import (
    RuleBasedInvestigationStatusProjector,
)
from tsm_agt.adapters.rule_based_semantic_action import RuleBasedSemanticActionClassifier
from tsm_agt.adapters.rule_based_stop_or_pivot import RuleBasedStopOrPivotPolicy
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.adapters.structured_evidence import StructuredEvidenceDeltaEvaluator
from tsm_agt.bootstrap import (
    compose_fixture_application, compose_openai_compatible_readonly_application,
)
from tsm_agt.cli import _render_investigation_status
from tsm_agt.core import AgentTurnResult, TaskState, canonical_hash
from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, EvidenceQuestion, FinishReason, HealthState,
    HealthStatus, InvestigationStatusProjectorPort, InvestigationStatusSignals,
    Message, MessageRole, ModelRequest, ModelResponse, ProviderCapabilities,
    RuntimeStorePort, TextBlock, ToolCall, ToolCallBlock, ToolIdempotency, ToolResult,
    ToolResultBlock, ToolRisk, ToolSpec,
)


class StatusTool:
    descriptor = AdapterDescriptor(
        "fixture.status-tool", "1", "ToolProviderPort", "1"
    )

    async def start(self, context): pass
    async def stop(self, deadline): pass
    async def health(self): return HealthStatus(HealthState.HEALTHY)
    async def list_tools(self):
        return (ToolSpec(
            "fixture.inspect_status", "Inspect status fixture",
            {"type": "object", "properties": {},
             "additionalProperties": False},
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
        ),)
    async def invoke(self, call, context):
        return ToolResult(call.call_id, True, {
            "fact": "PrivateStatusFact", "path": "src/private-status.py"
        })


class StatusModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if any(
            isinstance(block, ToolResultBlock)
            for message in request.messages for block in message.content
        ):
            return ModelResponse(Message(
                "status-final", MessageRole.ASSISTANT, (TextBlock("done"),)
            ))
        return ModelResponse(Message(
            "status-call", MessageRole.ASSISTANT, (ToolCallBlock(ToolCall(
                "status-tool-call", "fixture.inspect_status", {},
                EvidenceQuestion("Q-private-status", "Private status question?"),
            )),),
        ), FinishReason.TOOL_CALL)


def compose_status_app(store):
    return compose_fixture_application(
        model_adapter=StatusModel(), tool_adapters=(StatusTool(),),
        store_adapter=store, require_evidence_questions=True,
        evidence_delta_evaluator_adapter=StructuredEvidenceDeltaEvaluator(),
        semantic_action_classifier_adapter=RuleBasedSemanticActionClassifier(),
        exploration_budget_policy_adapter=RuleBasedExplorationBudgetPolicy(),
        stop_or_pivot_policy_adapter=RuleBasedStopOrPivotPolicy(),
        agent_progress_projector_adapter=RuleBasedAgentProgressProjector(),
        evidence_level_evaluator_adapter=RuleBasedEvidenceLevelEvaluator(),
        investigation_status_projector_adapter=(
            RuleBasedInvestigationStatusProjector()
        ),
    )


async def run_task(app, root: Path, task_id: str):
    task = await app.kernel.create_task("status task", root, task_id)
    for state in (TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                  TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                  TaskState.EXECUTING):
        task = await app.kernel.transition_task(task.task_id, state, state.value)
    result = await app.kernel.run_agent_turn(task.task_id, "inspect status")
    assert isinstance(result, AgentTurnResult)
    return result


class InvestigationStatusProjectorTest(unittest.IsolatedAsyncioTestCase):
    async def test_projector_keeps_only_safe_taxonomy_and_counts(self):
        projector = RuleBasedInvestigationStatusProjector()
        await projector.start(AdapterContext(config={}, emit_event=lambda *_: None))
        try:
            status = await projector.project(InvestigationStatusSignals(
                question_ref="abcdef1234567890/private",
                evidence_counts={"new_facts": 2, "private_category": 99},
                consecutive_zero_delta=-1, scored_actions=3, budget_score=-20,
                value_band="low", low_value_streak=2,
                cumulative_tool_milliseconds=75, latest_decision="CHANGE_METHOD",
                model_calls=4, tool_calls=3,
            ))
            self.assertEqual(status.question_ref, "abcdef123456")
            self.assertEqual(status.evidence_total, 2)
            self.assertNotIn("private_category", status.evidence_counts)
            self.assertEqual(status.latest_decision, "CHANGE_METHOD")
            self.assertEqual(status.consecutive_zero_delta, 0)
        finally:
            await projector.stop(datetime.now())

    async def test_cli_renderer_is_readable_and_redacted(self):
        projector = RuleBasedInvestigationStatusProjector()
        await projector.start(AdapterContext(config={}, emit_event=lambda *_: None))
        try:
            status = await projector.project(InvestigationStatusSignals(
                question_ref="abcdef123456", evidence_counts={"new_facts": 2},
                consecutive_zero_delta=1, scored_actions=2, budget_score=-20,
                value_band="low", low_value_streak=1,
                cumulative_tool_milliseconds=80, latest_decision="CHANGE_METHOD",
            ))
            text = "\n".join(_render_investigation_status(status))
            self.assertIn("question=Q#abcdef123456", text)
            self.assertIn("事实 2", text)
            self.assertIn("低收益", text)
            self.assertIn("换一种方法", text)
            self.assertNotIn("query", text)
            self.assertNotIn("src/", text)
        finally:
            await projector.stop(datetime.now())


class InvestigationStatusKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_completed_task_rebuilds_readonly_status_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first = compose_status_app(SQLiteRuntimeStore(database))
            await first.registry.start_all()
            try:
                result = await run_task(first, root, "task-status")
                store = first.registry.require(RuntimeStorePort)
                before = await store.read_events(result.task_id)
                status = await first.kernel.get_investigation_status(result.task_id)
                after = await store.read_events(result.task_id)
                self.assertEqual(len(before), len(after))
                self.assertEqual(status.question_ref, canonical_hash("Q-private-status")[:12])
                self.assertGreater(status.evidence_total, 0)
                self.assertEqual(status.model_calls, 2)
                self.assertEqual(status.tool_calls, 1)
                self.assertEqual(status.latest_decision, "CONTINUE")
                encoded = str(status.to_data())
                self.assertNotIn("Private status question", encoded)
                self.assertNotIn("src/private-status.py", encoded)
                self.assertNotIn("Q-private-status", encoded)
            finally:
                await first.registry.stop_all()
            second = compose_status_app(SQLiteRuntimeStore(database))
            await second.registry.start_all()
            try:
                restarted = await second.kernel.get_investigation_status("task-status")
                self.assertEqual(restarted.to_data(), status.to_data())
            finally:
                await second.registry.stop_all()


class InvestigationStatusCompositionTest(unittest.TestCase):
    def test_fixture_optional_replaceable_and_real_default(self):
        self.assertEqual(
            compose_fixture_application().registry.all(
                InvestigationStatusProjectorPort
            ), ()
        )
        projector = RuleBasedInvestigationStatusProjector()
        fixture = compose_fixture_application(
            investigation_status_projector_adapter=projector
        )
        self.assertIs(
            fixture.registry.require(InvestigationStatusProjectorPort), projector
        )
        real = compose_openai_compatible_readonly_application(
            base_url="https://example.test/v1", model="model", api_key="key"
        )
        self.assertIsInstance(
            real.registry.require(InvestigationStatusProjectorPort),
            RuleBasedInvestigationStatusProjector,
        )


if __name__ == "__main__":
    unittest.main()
