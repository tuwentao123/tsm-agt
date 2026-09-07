from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.rule_based_exploration_budget import RuleBasedExplorationBudgetPolicy
from tsm_agt.adapters.rule_based_semantic_action import RuleBasedSemanticActionClassifier
from tsm_agt.adapters.rule_based_stop_or_pivot import RuleBasedStopOrPivotPolicy
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.adapters.structured_evidence import StructuredEvidenceDeltaEvaluator
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import AgentTurnResult, TaskState
from tsm_agt.ports import (
    AdapterDescriptor, EvidenceQuestion, FinishReason, HealthState, HealthStatus,
    Message, MessageRole, ModelRequest, ModelResponse, ProviderCapabilities,
    RuntimeStorePort, StopOrPivotAction, StopOrPivotPolicyPort,
    StopOrPivotSignals, StopOrPivotState, TextBlock, ToolCall, ToolCallBlock,
    ToolIdempotency, ToolResult, ToolResultBlock, ToolRisk, ToolSpec,
)


def call(call_id: str, name: str = "core.search_text") -> ToolCall:
    args = (
        {"query": "PivotTarget", "path": ".", "max_matches": 100}
        if name == "core.search_text" else {"path": "src/pivot.py"}
    )
    return ToolCall(
        call_id, name, args, EvidenceQuestion("Q-pivot", "Where is PivotTarget?")
    )


class RuleBasedStopOrPivotPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.policy = RuleBasedStopOrPivotPolicy()
        await self.policy.start(None)

    async def asyncTearDown(self) -> None:
        await self.policy.stop(datetime.now())

    async def _action(self, **values):
        update = await self.policy.decide(
            call("decision"), StopOrPivotSignals(**values), StopOrPivotState()
        )
        return update.decision.action

    async def test_selects_read_narrow_ask_summarize_stop_and_continue(self):
        self.assertEqual(
            await self._action(read_hits_required=True, candidate_count=2),
            StopOrPivotAction.READ_HITS,
        )
        self.assertEqual(
            await self._action(scope_reason_required=True),
            StopOrPivotAction.NARROW_SCOPE,
        )
        self.assertEqual(
            await self._action(semantic_family="REQUEST_CLARIFICATION"),
            StopOrPivotAction.ASK_USER,
        )
        self.assertEqual(
            await self._action(plan_finished=True),
            StopOrPivotAction.SUMMARIZE_WITH_EVIDENCE,
        )
        self.assertEqual(
            await self._action(plan_should_stop=True),
            StopOrPivotAction.STOP_NO_PROGRESS,
        )
        self.assertEqual(await self._action(), StopOrPivotAction.CONTINUE)

    async def test_low_value_requires_method_change_then_stops_same_method(self):
        signals = StopOrPivotSignals(
            semantic_family="SEARCH_CONCEPT", semantic_signature="same-search",
            budget_wrap_up=True, budget_reason="consecutive_low_value",
            low_value_streak=2,
        )
        first = await self.policy.decide(call("first"), signals, StopOrPivotState())
        self.assertEqual(first.decision.action, StopOrPivotAction.CHANGE_METHOD)
        repeated = await self.policy.decide(call("same"), signals, first.state)
        self.assertEqual(repeated.decision.action, StopOrPivotAction.STOP_NO_PROGRESS)
        changed = await self.policy.decide(
            call("changed", "core.read_file"),
            StopOrPivotSignals(
                semantic_family="READ_ARTIFACT", semantic_signature="read-file",
                budget_wrap_up=True, budget_reason="consecutive_low_value",
                low_value_streak=2,
            ), first.state,
        )
        self.assertEqual(changed.decision.action, StopOrPivotAction.CONTINUE)


class PivotFixtureTool:
    descriptor = AdapterDescriptor(
        "fixture.pivot-tool", "1", "ToolProviderPort", "1"
    )

    def __init__(self) -> None:
        self.calls = []

    async def start(self, context): pass
    async def stop(self, deadline): pass
    async def health(self): return HealthStatus(HealthState.HEALTHY)

    async def list_tools(self):
        common = (ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT)
        return (
            ToolSpec(
                "core.search_text", "Search repeated results",
                {"type": "object", "properties": {
                    "query": {"type": "string"}, "path": {"type": "string"},
                    "max_matches": {"type": "integer"},
                }, "required": ["query"], "additionalProperties": False},
                *common,
            ),
            ToolSpec(
                "core.read_file", "Read a located result",
                {"type": "object", "properties": {
                    "path": {"type": "string"},
                }, "required": ["path"], "additionalProperties": False},
                *common,
            ),
        )

    async def invoke(self, requested, context):
        self.calls.append(requested.name)
        if requested.name == "core.read_file":
            return ToolResult(requested.call_id, True, {
                "path": "src/pivot.py", "sha256": "a" * 64,
                "start_line": 1, "end_line": 1, "total_lines": 1,
                "content": "class PivotTarget: pass",
            })
        return ToolResult(requested.call_id, True, {
            "path": ".",
            "matches": [
                {"path": f"src/file-{i}.py", "line": i + 1}
                for i in range(8)
            ],
            "scanned_files": 8,
        })


class ChangeMethodModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result for message in request.messages for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        if results and results[-1].error_code == "EXPLORATION_CHANGE_METHOD_REQUIRED":
            requested = call("pivot-read", "core.read_file")
        elif results and results[-1].call_id == "pivot-read":
            return ModelResponse(Message(
                "final", MessageRole.ASSISTANT, (TextBlock("changed method"),)
            ))
        else:
            requested = call(f"search-{len(results) + 1}")
        return ModelResponse(Message(
            requested.call_id, MessageRole.ASSISTANT,
            (ToolCallBlock(requested),),
        ), FinishReason.TOOL_CALL)


class StopOrPivotKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_kernel_requests_method_change_and_allows_different_method(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src/pivot.py").write_text(
                "class PivotTarget: pass\n", encoding="utf-8"
            )
            database = root / "runtime.db"
            tool = PivotFixtureTool()
            app = compose_fixture_application(
                model_adapter=ChangeMethodModel(), tool_adapters=(tool,),
                store_adapter=SQLiteRuntimeStore(database),
                require_evidence_questions=True,
                evidence_delta_evaluator_adapter=StructuredEvidenceDeltaEvaluator(),
                semantic_action_classifier_adapter=RuleBasedSemanticActionClassifier(),
                exploration_budget_policy_adapter=RuleBasedExplorationBudgetPolicy(),
                stop_or_pivot_policy_adapter=RuleBasedStopOrPivotPolicy(),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "pivot test", root, "task-stop-or-pivot"
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                result = await app.kernel.run_agent_turn(
                    task.task_id, "force a method pivot"
                )
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(result.assistant_message.text, "changed method")
                self.assertEqual(
                    tool.calls, ["core.search_text"] * 3 + ["core.read_file"]
                )
                results = [
                    block.result for message in result.messages
                    for block in message.content if isinstance(block, ToolResultBlock)
                ]
                self.assertIn(
                    "EXPLORATION_CHANGE_METHOD_REQUIRED",
                    [item.error_code for item in results],
                )
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                decisions = [
                    event.payload["action"] for event in events
                    if event.event_type == "stop_or_pivot.decision_made"
                ]
                self.assertIn("CHANGE_METHOD", decisions)
                self.assertIn("CONTINUE", decisions)
                payload = str([
                    event.payload for event in events
                    if event.event_type.startswith("stop_or_pivot.")
                ])
                self.assertNotIn("PivotTarget", payload)
                self.assertNotIn("src/pivot.py", payload)
            finally:
                await app.registry.stop_all()

            store = SQLiteRuntimeStore(database)
            await store.start(None)
            try:
                events = await store.read_events("task-stop-or-pivot")
                self.assertTrue(any(
                    event.event_type == "stop_or_pivot.decision_made"
                    for event in events
                ))
            finally:
                await store.stop(datetime.now())


class StopOrPivotCompositionTest(unittest.TestCase):
    def test_fixture_policy_is_optional_and_replaceable(self):
        self.assertEqual(
            compose_fixture_application().registry.all(StopOrPivotPolicyPort), ()
        )
        policy = RuleBasedStopOrPivotPolicy()
        app = compose_fixture_application(stop_or_pivot_policy_adapter=policy)
        self.assertIs(app.registry.require(StopOrPivotPolicyPort), policy)


if __name__ == "__main__":
    unittest.main()
