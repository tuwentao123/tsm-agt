from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.rule_based_agent_progress import RuleBasedAgentProgressProjector
from tsm_agt.adapters.rule_based_exploration_budget import RuleBasedExplorationBudgetPolicy
from tsm_agt.adapters.rule_based_semantic_action import RuleBasedSemanticActionClassifier
from tsm_agt.adapters.rule_based_stop_or_pivot import RuleBasedStopOrPivotPolicy
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.adapters.structured_evidence import StructuredEvidenceDeltaEvaluator
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.cli import (
    _render_agent_progress_lines,
    _render_exploration_progress,
)
from tsm_agt.core import AgentProgress, AgentProgressKind, AgentTurnResult, TaskState
from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, AgentProgressProjectorPort,
    AgentProgressSignals, EvidenceQuestion, FinishReason, HealthState,
    HealthStatus, Message, MessageRole, ModelRequest, ModelResponse,
    ProviderCapabilities, RuntimeStorePort, TextBlock, ToolCall, ToolCallBlock,
    ToolIdempotency, ToolResult, ToolResultBlock, ToolRisk, ToolSpec,
)


class AgentProgressProjectorTest(unittest.IsolatedAsyncioTestCase):
    async def test_projector_exposes_only_redacted_taxonomy(self):
        projector = RuleBasedAgentProgressProjector()
        await projector.start(AdapterContext(config={}, emit_event=lambda *_: None))
        try:
            projection = await projector.project(AgentProgressSignals(
                phase="tool_result", semantic_family="SEARCH_DEFINITION",
                question_ref="1234567890abcdef", scope_kind="directory",
                scope_relation="narrowed", scope_depth=4, evidence_delta=2,
                budget_score=15, value_band="useful", next_action="CONTINUE",
            ))
            self.assertEqual(projection.activity, "search_definition")
            self.assertEqual(projection.question_ref, "1234567890ab")
            self.assertEqual(projection.scope, "directory")
            self.assertEqual(projection.scope_change, "narrowed")
            rendered = str(projection)
            self.assertNotIn("secret query", rendered)
            self.assertNotIn("src/private/file.py", rendered)
        finally:
            await projector.stop(datetime.now())

    def test_cli_renderer_is_readable_and_contains_no_tool_parameters(self):
        lines = _render_exploration_progress(AgentProgress(
            AgentProgressKind.EXPLORATION, phase="tool_result",
            activity="search_definition", question_ref="abc123def456",
            scope="directory", scope_change="narrowed", evidence_delta=0,
            consecutive_zero_delta=2, budget_score=-55, value_band="low",
            next_action="CHANGE_METHOD",
        ))
        text = "\n".join(lines)
        self.assertIn("问题：Q#abc123def456", text)
        self.assertIn("查找定义", text)
        self.assertIn("目录（已收窄）", text)
        self.assertIn("新增证据 0 项", text)
        self.assertIn("价值评分 -55（低收益）", text)
        self.assertIn("换一种方法", text)
        self.assertNotIn("query", text)
        self.assertNotIn("src/", text)

    def test_cli_live_renderer_shows_concrete_goal_question_scope_and_budget(self):
        lines = _render_exploration_progress(AgentProgress(
            AgentProgressKind.EXPLORATION, phase="tool_result",
            activity="search_definition", scope="directory",
            scope_change="narrowed", evidence_delta=1,
            goal="修复登录页崩溃",
            question="LoginViewModel 在哪里创建？",
            operation="core.search_text",
            operation_arguments={
                "query": "LoginViewModel", "path": "app/src"
            },
            scope_target="app/src",
            exploration_actions=3, max_exploration_actions=24,
            exploration_tool_calls=4, max_exploration_tool_calls=24,
            exploration_elapsed_seconds=7.5,
            max_exploration_elapsed_seconds=120,
            low_value_streak=1, max_low_value_streak=2,
            next_action="CONTINUE",
        ))
        text = "\n".join(lines)
        self.assertNotIn("用户目标：修复登录页崩溃", text)
        self.assertIn("当前要确认：LoginViewModel 在哪里创建？", text)
        self.assertIn("范围：app/src（已收窄）", text)
        self.assertNotIn("core.search_text", text)
        self.assertNotIn('\"query\":\"LoginViewModel\"', text)
        self.assertIn("探索动作 3/24", text)
        self.assertIn("探索工具调用 4/24", text)
        self.assertIn("探索工具耗时 7.5/120 秒", text)
        self.assertIn("连续低收益 1/2", text)
        self.assertNotIn("下一步：继续当前路线", text)

    def test_cli_renderer_suppresses_pre_execution_duplicate_projection(self):
        lines = _render_exploration_progress(AgentProgress(
            AgentProgressKind.EXPLORATION, phase="planned",
            activity="search_concept", goal="分析弹窗",
            question="标题来自哪里？", operation="core.search_text",
            operation_arguments={"query": "title", "path": "src"},
        ))
        self.assertEqual(lines, ())

    def test_compact_progress_has_one_start_and_one_completion_line(self):
        started = _render_agent_progress_lines(AgentProgress(
            AgentProgressKind.TOOL_STARTED, tool_call=3, max_tool_calls=40,
            tool_name="core.search_text",
            operation_presentation='{"path":"src","query":"LoginViewModel"}',
        ))
        completed = _render_agent_progress_lines(AgentProgress(
            AgentProgressKind.TOOL_COMPLETED, tool_call=3,
            tool_name="core.search_text", ok=True, elapsed_seconds=1.2,
            evidence_delta=2,
        ))
        self.assertEqual(len(started), 1)
        self.assertEqual(len(completed), 1)
        self.assertIn("[操作 3] core.search_text", started[0])
        self.assertIn("LoginViewModel", started[0])
        self.assertNotIn("/40", started[0])
        self.assertIn("[完成 3] core.search_text", completed[0])
        self.assertIn("新增证据 2 项", completed[0])

    def test_compact_progress_hides_routine_exploration_projection(self):
        routine = _render_agent_progress_lines(AgentProgress(
            AgentProgressKind.EXPLORATION, phase="tool_result",
            activity="search_definition", scope_change="narrowed",
            next_action="CONTINUE", evidence_delta=1,
        ))
        changed = _render_agent_progress_lines(AgentProgress(
            AgentProgressKind.EXPLORATION, phase="tool_result",
            next_action="CHANGE_METHOD",
        ))
        self.assertEqual(routine, ())
        self.assertEqual(changed, ("[路线调整] 当前路线收益低，换一种查法。",))

    def test_verbose_progress_keeps_diagnostic_detail(self):
        lines = _render_agent_progress_lines(AgentProgress(
            AgentProgressKind.TOOL_STARTED, tool_call=3, max_tool_calls=40,
            tool_name="core.search_text",
            operation_presentation='{"path":"src","query":"LoginViewModel"}',
        ), verbose=True)
        self.assertIn("[操作 3/40]", lines[0])


class ProgressTool:
    descriptor = AdapterDescriptor(
        "fixture.progress-search", "1", "ToolProviderPort", "1"
    )

    async def start(self, context): pass
    async def stop(self, deadline): pass
    async def health(self): return HealthStatus(HealthState.HEALTHY)
    async def list_tools(self):
        return (ToolSpec(
            "core.search_text", "Search progress fixture",
            {"type": "object", "properties": {
                "query": {"type": "string"}, "path": {"type": "string"},
                "max_matches": {"type": "integer"},
            }, "required": ["query"], "additionalProperties": False},
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
        ),)
    async def invoke(self, call, context):
        return ToolResult(call.call_id, True, {
            "path": "src/private-module",
            "matches": [{"path": "src/private-module/secret.py", "line": 1}],
            "scanned_files": 1,
        })


class ProgressModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        result = next((
            block.result for message in request.messages for block in message.content
            if isinstance(block, ToolResultBlock)
        ), None)
        if result is not None:
            return ModelResponse(Message(
                "final", MessageRole.ASSISTANT, (TextBlock("progress visible"),)
            ))
        call = ToolCall(
            "progress-search", "core.search_text",
            {"query": "PrivateSymbol", "path": "src/private-module"},
            EvidenceQuestion("Q-private-progress", "Where is PrivateSymbol?"),
        )
        return ModelResponse(Message(
            "progress-call", MessageRole.ASSISTANT, (ToolCallBlock(call),)
        ), FinishReason.TOOL_CALL)


class AgentProgressKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_kernel_emits_callback_and_redacted_sqlite_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src/private-module").mkdir(parents=True)
            database = root / "runtime.db"
            app = compose_fixture_application(
                model_adapter=ProgressModel(), tool_adapters=(ProgressTool(),),
                store_adapter=SQLiteRuntimeStore(database),
                require_evidence_questions=True,
                evidence_delta_evaluator_adapter=StructuredEvidenceDeltaEvaluator(),
                semantic_action_classifier_adapter=RuleBasedSemanticActionClassifier(),
                exploration_budget_policy_adapter=RuleBasedExplorationBudgetPolicy(),
                stop_or_pivot_policy_adapter=RuleBasedStopOrPivotPolicy(),
                agent_progress_projector_adapter=RuleBasedAgentProgressProjector(),
            )
            await app.registry.start_all()
            progress = []
            try:
                task = await app.kernel.create_task(
                    "progress test", root, "task-agent-progress"
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
                    task.task_id, "show progress", on_progress=progress.append
                )
                self.assertIsInstance(result, AgentTurnResult)
                projected = [
                    item for item in progress
                    if item.kind is AgentProgressKind.EXPLORATION
                ]
                self.assertGreaterEqual(len(projected), 2)
                completed = next(item for item in projected if item.phase == "tool_result")
                self.assertEqual(completed.activity, "search_definition")
                self.assertEqual(completed.scope, "directory")
                self.assertEqual(completed.evidence_delta, 3)
                self.assertEqual(completed.next_action, "CONTINUE")
                self.assertEqual(completed.goal, "progress test")
                self.assertEqual(
                    completed.question, "Where is PrivateSymbol?"
                )
                self.assertEqual(completed.scope_target, "src/private-module")
                self.assertEqual(
                    completed.operation_arguments["query"], "PrivateSymbol"
                )
                self.assertEqual(completed.exploration_actions, 1)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                payloads = [
                    event.payload for event in events
                    if event.event_type == "agent_progress.projected"
                ]
                self.assertGreaterEqual(len(payloads), 2)
                encoded = str(payloads)
                self.assertNotIn("PrivateSymbol", encoded)
                self.assertNotIn("src/private-module", encoded)
                self.assertNotIn("Q-private-progress", encoded)
            finally:
                await app.registry.stop_all()

            store = SQLiteRuntimeStore(database)
            await store.start(None)
            try:
                events = await store.read_events("task-agent-progress")
                self.assertTrue(any(
                    event.event_type == "agent_progress.projected"
                    for event in events
                ))
            finally:
                await store.stop(datetime.now())


class AgentProgressCompositionTest(unittest.TestCase):
    def test_fixture_projector_is_optional_and_replaceable(self):
        self.assertEqual(
            compose_fixture_application().registry.all(AgentProgressProjectorPort), ()
        )
        projector = RuleBasedAgentProgressProjector()
        app = compose_fixture_application(agent_progress_projector_adapter=projector)
        self.assertIs(app.registry.require(AgentProgressProjectorPort), projector)


if __name__ == "__main__":
    unittest.main()
