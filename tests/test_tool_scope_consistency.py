from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.rule_based_exploration_budget import (
    RuleBasedExplorationBudgetPolicy,
)
from tsm_agt.adapters.rule_based_scope_consistency import (
    RuleBasedToolScopeConsistencyPolicy,
)
from tsm_agt.adapters.rule_based_semantic_action import (
    RuleBasedSemanticActionClassifier,
)
from tsm_agt.adapters.structured_evidence import StructuredEvidenceDeltaEvaluator
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AgentTurnResult, ApprovalDecision, ApprovalKind, ApprovalRequired,
    EvidenceQuestionStatus, TaskState,
)
from tsm_agt.ports import (
    EvidenceQuestion, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ModelUsage, ProviderCapabilities, RuntimeStorePort, TextBlock,
    ToolCall, ToolCallBlock, ToolResultBlock, WorkspacePathPort,
)


class ScopeCorrectionModel(EchoModelProvider):
    """First uses the wrong root, then replans from Runtime location facts."""

    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    def __init__(self, intended: Path) -> None:
        super().__init__()
        self.intended = intended

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result for message in request.messages for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        if not results:
            path = "."
            call_id = "wrong-scope"
        elif results[-1].error_code == "TOOL_SCOPE_MISMATCH":
            path = str(self.intended)
            call_id = "corrected-scope"
        else:
            return ModelResponse(
                Message(
                    "scope-final", MessageRole.ASSISTANT,
                    (TextBlock("confirmed from intended scope"),),
                ),
                FinishReason.STOP, ModelUsage(1, 1),
            )
        return ModelResponse(
            Message(
                call_id, MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    call_id, "core.find_files",
                    {"path": path, "pattern": "service.py"},
                    EvidenceQuestion(
                        "Q-scope", "Where is service.py in the intended scope?",
                        expected_scope=str(self.intended),
                    ),
                )),),
            ),
            FinishReason.TOOL_CALL, ModelUsage(1, 1),
        )


class ToolScopeConsistencyKernelTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _executing_task(app, workspace: Path, task_id: str):
        task = await app.kernel.create_task(
            "inspect intended scope", workspace, task_id
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await app.kernel.transition_task(
                task.task_id, state, state.value
            )
        return task

    async def test_wrong_scope_becomes_replan_then_correct_scope_resolves(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            intended = workspace / "related"
            intended.mkdir()
            (intended / "service.py").write_text(
                "class Service: pass\n", encoding="utf-8"
            )
            app = compose_fixture_application(
                model_adapter=ScopeCorrectionModel(intended),
                tool_adapters=(CoreReadOnlyToolProvider(),),
                require_evidence_questions=True,
                evidence_delta_evaluator_adapter=StructuredEvidenceDeltaEvaluator(),
                semantic_action_classifier_adapter=RuleBasedSemanticActionClassifier(),
                exploration_budget_policy_adapter=RuleBasedExplorationBudgetPolicy(),
                tool_scope_consistency_policy_adapter=(
                    RuleBasedToolScopeConsistencyPolicy()
                ),
            )
            await app.registry.start_all()
            try:
                task = await self._executing_task(
                    app, workspace, "task-scope-correction"
                )
                result = await app.kernel.run_agent_turn(
                    task.task_id, "continue in the intended scope"
                )
                self.assertIsInstance(result, AgentTurnResult)
                assert isinstance(result, AgentTurnResult)
                tool_results = [
                    block.result for message in result.messages
                    for block in message.content if isinstance(block, ToolResultBlock)
                ]
                self.assertEqual(
                    [item.error_code for item in tool_results],
                    ["TOOL_SCOPE_MISMATCH", None],
                )
                mismatch = tool_results[0]
                self.assertEqual(mismatch.data["expected_scope"], str(intended))
                canonical_workspace = app.registry.require(
                    WorkspacePathPort
                ).normalize_workspace(workspace)
                self.assertEqual(
                    mismatch.data["resolved_root"], str(canonical_workspace)
                )
                self.assertTrue(mismatch.meta["recoverable_input"])

                questions = await app.kernel.get_evidence_questions(task.task_id)
                self.assertEqual(
                    questions.get("Q-scope").status, EvidenceQuestionStatus.RESOLVED
                )
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                scope_events = [
                    event for event in events
                    if event.event_type == "tool.scope_consistency_evaluated"
                ]
                self.assertEqual(
                    [event.payload["relation"] for event in scope_events],
                    ["MISMATCH", "MATCH"],
                )
                question_events = [
                    event for event in events
                    if event.event_type == "evidence.question_state_changed"
                ]
                self.assertEqual(
                    [event.payload["next_status"] for event in question_events],
                    ["OPEN", "RESOLVED"],
                )
                self.assertEqual(
                    question_events[0].payload["blocking_reason"],
                    "TOOL_SCOPE_MISMATCH",
                )
                mismatch_delta = next(
                    event.payload for event in events
                    if event.event_type == "evidence.delta_evaluated"
                    and event.payload["tool_call_id"] == "wrong-scope"
                )
                self.assertEqual(mismatch_delta["total_new"], 0)
                self.assertEqual(mismatch_delta["consecutive_zero_delta"], 0)
                mismatch_budget = next(
                    event.payload for event in events
                    if event.event_type == "exploration_budget.action_scored"
                    and event.payload["tool_call_id"] == "wrong-scope"
                )
                self.assertEqual(mismatch_budget["scored_actions"], 0)
                self.assertEqual(mismatch_budget["value_band"], "recoverable_input")
            finally:
                await app.registry.stop_all()

    async def test_matching_external_scope_still_requires_current_task_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            external = root / "external"
            workspace.mkdir()
            external.mkdir()
            target = external / "service.py"
            target.write_text("external\n", encoding="utf-8")
            app = compose_fixture_application(
                tool_adapters=(CoreReadOnlyToolProvider(),),
                tool_scope_consistency_policy_adapter=(
                    RuleBasedToolScopeConsistencyPolicy()
                ),
            )
            await app.registry.start_all()
            try:
                task = await self._executing_task(
                    app, workspace, "task-external-scope"
                )
                call = ToolCall(
                    "external-match", "core.read_file", {"path": str(target)},
                    EvidenceQuestion(
                        "Q-external", "Read the external service",
                        expected_scope=str(external),
                    ),
                )
                with self.assertRaises(ApprovalRequired) as caught:
                    await app.kernel.invoke_tool(task.task_id, "turn-1", call)
                self.assertEqual(
                    caught.exception.request.kind, ApprovalKind.WORKSPACE_READ
                )
                approved = await app.kernel.resolve_approval(
                    task.task_id, caught.exception.request.request_id,
                    caught.exception.request.payload_hash, ApprovalDecision.APPROVE,
                    "allow this Task to read external scope",
                )
                self.assertTrue(approved.ok)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                scope = next(
                    event for event in events
                    if event.event_type == "tool.scope_consistency_evaluated"
                )
                self.assertEqual(scope.payload["relation"], "MATCH")
                self.assertEqual(scope.payload["action"], "ALLOW")
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
