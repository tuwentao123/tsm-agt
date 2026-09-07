from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    ActionProgressState, AgentTurnResult, PlanGuard, TaskState,
    WorkingMemorySnapshot, WorkingPlanStep, WorkingPlanStepStatus,
)
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, HealthState, HealthStatus, Message,
    MessageRole, ModelRequest, ModelResponse, ProviderCapabilities, TextBlock,
    SemanticAction, SemanticActionFamily, SemanticScopeKind, ToolCall,
    ToolCallBlock, ToolIdempotency, ToolResult, ToolRisk, ToolSpec,
)


class PlanGuardUnitTest(unittest.TestCase):
    def test_selects_active_then_pending_and_implicit_goal(self) -> None:
        guard = PlanGuard()
        active = WorkingMemorySnapshot(
            "task", 1, "ship", plan=(
                WorkingPlanStep("one", "first", WorkingPlanStepStatus.COMPLETED, "done"),
                WorkingPlanStep("two", "second", WorkingPlanStepStatus.IN_PROGRESS, "tested"),
                WorkingPlanStep("three", "third", WorkingPlanStepStatus.PENDING, "reviewed"),
            ),
        )
        self.assertEqual(guard.current_slice(active).step_id, "two")
        pending = WorkingMemorySnapshot(
            "task", 1, "ship", plan=(
                WorkingPlanStep("one", "first", WorkingPlanStepStatus.PENDING, "done"),
            ),
        )
        self.assertEqual(guard.current_slice(pending).step_id, "one")
        implicit = WorkingMemorySnapshot.initial("task", "answer question")
        self.assertTrue(guard.current_slice(implicit).implicit)

    def test_repeated_unchanged_action_stops_and_state_change_resets(self) -> None:
        guard = PlanGuard(no_progress_limit=3)
        memory = WorkingMemorySnapshot.initial("task", "inspect")
        call = ToolCall("call-1", "fixture.echo", {"text": "same"})
        previous = ActionProgressState()
        decisions = []
        for _ in range(4):
            decision = guard.evaluate(call, memory, "workspace-a", previous)
            decisions.append(decision)
            previous = ActionProgressState(
                decision.action_signature, decision.relevant_state_hash,
                decision.consecutive_no_progress,
            )
        self.assertEqual(
            [item.consecutive_no_progress for item in decisions], [0, 1, 2, 3]
        )
        self.assertTrue(decisions[-1].should_stop)
        changed = guard.evaluate(call, memory, "workspace-b", previous)
        self.assertEqual(changed.consecutive_no_progress, 0)
        self.assertFalse(changed.should_stop)

    def test_rejects_multiple_active_steps_and_finished_plan_stops(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most one"):
            WorkingMemorySnapshot(
                "task", 1, "goal", plan=(
                    WorkingPlanStep("a", "a", WorkingPlanStepStatus.IN_PROGRESS, "a done"),
                    WorkingPlanStep("b", "b", WorkingPlanStepStatus.IN_PROGRESS, "b done"),
                ),
            )
        finished = WorkingMemorySnapshot(
            "task", 1, "goal", plan=(
                WorkingPlanStep("a", "a", WorkingPlanStepStatus.COMPLETED, "done"),
            ),
        )
        decision = PlanGuard().evaluate(
            ToolCall("call", "fixture.echo", {}), finished, "workspace",
            ActionProgressState(),
        )
        self.assertTrue(decision.goal_slice.plan_finished)
        self.assertTrue(decision.should_stop)

    def test_evidence_aware_zero_delta_survives_changed_tool_arguments(self) -> None:
        guard = PlanGuard(no_progress_limit=3)
        memory = WorkingMemorySnapshot.initial("task", "inspect")
        previous = ActionProgressState(
            "old-signature", "old-state", 3, True
        )

        decision = guard.evaluate(
            ToolCall("new-call", "fixture.echo", {"text": "different"}),
            memory, "workspace-new", previous,
        )

        self.assertEqual(decision.consecutive_no_progress, 3)
        self.assertTrue(decision.should_stop)

    def test_zero_delta_continues_only_for_same_semantic_action(self) -> None:
        guard = PlanGuard(no_progress_limit=3)
        memory = WorkingMemorySnapshot.initial("task", "inspect")
        previous = ActionProgressState(
            "exact-old", "state-old", 2, True,
            "semantic-definition", "SEARCH_DEFINITION",
        )
        same_semantic = SemanticAction(
            SemanticActionFamily.SEARCH_DEFINITION, "concept", "target",
            SemanticScopeKind.DIRECTORY, "scope-new", "semantic-definition",
            "test", "1", 1.0,
        )
        changed_method = SemanticAction(
            SemanticActionFamily.SEARCH_REFERENCES, "concept", "target",
            SemanticScopeKind.DIRECTORY, "scope-new", "semantic-references",
            "test", "1", 1.0,
        )

        repeated = guard.evaluate(
            ToolCall("new-scope", "fixture.echo", {"path": "narrower"}),
            memory, "workspace", previous, same_semantic,
        )
        pivoted = guard.evaluate(
            ToolCall("references", "fixture.echo", {"path": "narrower"}),
            memory, "workspace", previous, changed_method,
        )

        self.assertEqual(repeated.consecutive_no_progress, 2)
        self.assertTrue(repeated.semantic_repeat)
        self.assertEqual(pivoted.consecutive_no_progress, 0)
        self.assertFalse(pivoted.semantic_repeat)


class RepeatingModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requests.append(request)
        if request.allow_tool_calls:
            return ModelResponse(Message(
                f"repeat-{self.calls}", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    f"repeat-call-{self.calls}", "fixture.read_same",
                    {"path": "same.txt"},
                )),),
            ), FinishReason.TOOL_CALL)
        return ModelResponse(Message(
            "repeat-final", MessageRole.ASSISTANT,
            (TextBlock("Stopped after repeated actions; verification is incomplete."),),
        ), FinishReason.STOP)


class CountingReadTool:
    descriptor = AdapterDescriptor(
        "fixture.counting-read", "1.0", "ToolProviderPort", "1.0"
    )

    def __init__(self) -> None:
        self.calls = 0

    async def start(self, context):
        pass

    async def stop(self, deadline):
        pass

    async def health(self):
        return HealthStatus(HealthState.HEALTHY)

    async def list_tools(self):
        return (ToolSpec(
            "fixture.read_same", "Return the same observation",
            {"type": "object", "properties": {"path": {"type": "string"}},
             "required": ["path"], "additionalProperties": False},
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
        ),)

    async def invoke(self, call, context):
        self.calls += 1
        return ToolResult(call.call_id, True, {"observation": "unchanged"})


class PlanGuardKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_action_stops_before_fourth_tool_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = RepeatingModel()
            tool = CountingReadTool()
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=(tool,)
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("inspect same file", Path(directory))
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(task.task_id, state, state.value)
                result = await app.kernel.run_agent_turn(
                    task.task_id, "inspect", max_model_calls=10, max_tool_calls=10
                )
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(tool.calls, 3)
                self.assertEqual(result.tool_calls, 3)
                self.assertIn("verification is incomplete", result.assistant_message.text)
                self.assertFalse(model.requests[-1].allow_tool_calls)
                events = await app.kernel.dependencies.store.read_events(task.task_id)
                stopped = [
                    event for event in events
                    if event.event_type == "plan.no_progress_stopped"
                ]
                self.assertEqual(len(stopped), 1)
                self.assertEqual(stopped[0].payload["consecutive_no_progress"], 3)
                projection = await app.kernel.get_flow_projection(task.task_id)
                node = next(
                    node for node in projection.nodes
                    if node.label == "Plan stopped: no progress"
                )
                diagnostic = projection.inspect_node(node.node_id)
                self.assertEqual(
                    dict(diagnostic.facts[0].values)["consecutive_no_progress"], 3
                )
                exported = json.dumps(
                    __import__("tsm_agt.core", fromlist=["build_flow_export_document"])
                    .build_flow_export_document(projection), ensure_ascii=False,
                )
                self.assertNotIn("same.txt", exported)
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
