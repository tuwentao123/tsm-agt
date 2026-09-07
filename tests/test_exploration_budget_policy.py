from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.rule_based_exploration_budget import (
    RuleBasedExplorationBudgetPolicy,
)
from tsm_agt.adapters.rule_based_semantic_action import (
    RuleBasedSemanticActionClassifier,
)
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.adapters.structured_evidence import StructuredEvidenceDeltaEvaluator
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.exploration_configuration import (
    ExplorationBudgetConfiguration, load_exploration_budget_configuration,
)
from tsm_agt.core import AgentProgressKind, AgentTurnResult, TaskState
from tsm_agt.ports import (
    AdapterDescriptor, EvidenceDelta, EvidenceItem, EvidenceQuestion,
    ExplorationBudgetAction, ExplorationBudgetObservation,
    ExplorationBudgetPolicyPort, ExplorationBudgetProbe, ExplorationBudgetState,
    FinishReason, HealthState, HealthStatus, Message, MessageRole, ModelRequest,
    ModelResponse, ProviderCapabilities, RuntimeStorePort, TextBlock, ToolCall,
    ToolCallBlock, ToolIdempotency, ToolResult, ToolResultBlock, ToolRisk, ToolSpec,
)


def search_call(call_id: str) -> ToolCall:
    return ToolCall(
        call_id, "core.search_text",
        {"query": "BudgetTarget", "path": ".", "max_matches": 100},
        EvidenceQuestion("Q-budget", "Where is BudgetTarget used?"),
    )


class RuleBasedExplorationBudgetPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.policy = RuleBasedExplorationBudgetPolicy()
        self.classifier = RuleBasedSemanticActionClassifier()
        await self.policy.start(None)
        await self.classifier.start(None)

    async def asyncTearDown(self) -> None:
        await self.policy.stop(datetime.now())
        await self.classifier.stop(datetime.now())

    async def test_useful_evidence_resets_low_value_streak(self):
        call = search_call("useful")
        action = await self.classifier.classify(call)
        delta = EvidenceDelta(
            "Q-budget", (EvidenceItem("new_paths", "p1", "new path"),),
            "ok", 0,
        )
        update = await self.policy.after_result(
            call, ToolResult(call.call_id, True, {"matches": []}), action, delta,
            ExplorationBudgetObservation(400),
            ExplorationBudgetState(low_value_streak=1),
        )
        self.assertEqual(update.value_band, "useful")
        self.assertEqual(update.state.low_value_streak, 0)
        decision = await self.policy.before_call(
            search_call("next"), action, ExplorationBudgetProbe(10, 30, 15, 40),
            update.state,
        )
        self.assertEqual(decision.action, ExplorationBudgetAction.CONTINUE)

    async def test_two_repeated_zero_delta_actions_require_wrap_up(self):
        call = search_call("low")
        action = await self.classifier.classify(call)
        zero = EvidenceDelta("Q-budget", (), "ok", 1)
        state = ExplorationBudgetState()
        for index in range(2):
            update = await self.policy.after_result(
                call, ToolResult(call.call_id, True, {"matches": []}), action,
                zero, ExplorationBudgetObservation(11_000, broad_search=True),
                state,
            )
            state = update.state
            self.assertEqual(update.value_band, "low")
        decision = await self.policy.before_call(
            search_call("blocked"), action,
            ExplorationBudgetProbe(10, 30, 15, 40), state,
        )
        self.assertEqual(decision.action, ExplorationBudgetAction.WRAP_UP)
        self.assertEqual(decision.reason, "consecutive_low_value")

    async def test_large_result_list_has_diminishing_value(self):
        call = search_call("large-list")
        action = await self.classifier.classify(call)
        items = tuple(
            EvidenceItem("new_paths", f"p-{index}", f"path {index}")
            for index in range(100)
        )
        update = await self.policy.after_result(
            call, ToolResult(call.call_id, True, {"matches": []}), action,
            EvidenceDelta("Q-budget", items, "ok", 0),
            ExplorationBudgetObservation(400), ExplorationBudgetState(),
        )
        self.assertEqual(update.new_evidence, 100)
        self.assertEqual(update.score, 10)
        self.assertEqual(update.value_band, "useful")

    async def test_large_workspace_search_is_lead_generation_not_progress(self):
        call = search_call("broad-list")
        action = await self.classifier.classify(call)
        items = tuple(
            EvidenceItem("new_paths", f"p-{index}", f"path {index}")
            for index in range(80)
        )
        update = await self.policy.after_result(
            call, ToolResult(call.call_id, True, {
                "matches": [
                    {"path": f"src/file-{index}.py"} for index in range(80)
                ],
            }), action, EvidenceDelta("Q-budget", items, "ok", 0),
            ExplorationBudgetObservation(400, broad_search=True),
            ExplorationBudgetState(),
        )
        self.assertEqual(update.value_band, "low")
        self.assertLess(update.score, 0)

    async def test_targeted_read_is_allowed_after_two_low_value_searches(self):
        policy = RuleBasedExplorationBudgetPolicy(max_low_value_streak=2)
        await policy.start(None)
        try:
            read = ToolCall(
                "read-after-search", "core.read_file",
                {"path": "src/target.py"},
                EvidenceQuestion("Q-read", "What does the candidate contain?"),
            )
            action = await self.classifier.classify(read)
            decision = await policy.before_call(
                read, action, ExplorationBudgetProbe(10, 30, 15, 40),
                ExplorationBudgetState(scored_actions=2, low_value_streak=2),
            )
            self.assertEqual(decision.action, ExplorationBudgetAction.CONTINUE)
            self.assertEqual(decision.reason, "healthy_budget")
        finally:
            await policy.stop(datetime.now())

    async def test_exploration_action_limit_soft_lands_before_next_tool(self):
        policy = RuleBasedExplorationBudgetPolicy(max_scored_actions=3)
        await policy.start(None)
        try:
            action = await self.classifier.classify(search_call("limited"))
            decision = await policy.before_call(
                search_call("blocked"), action,
                ExplorationBudgetProbe(10, 30, 15, 40),
                ExplorationBudgetState(scored_actions=3),
            )
            self.assertEqual(decision.action, ExplorationBudgetAction.FOCUS)
            self.assertFalse(decision.focus_allows_call)
            self.assertEqual(decision.reason, "exploration_action_limit")
            self.assertEqual(decision.scored_actions, 3)
            self.assertEqual(decision.max_scored_actions, 3)
            self.assertEqual(decision.used_tool_calls, 10)
            self.assertEqual(decision.max_total_tool_calls, 24)
            self.assertEqual(decision.max_cumulative_tool_milliseconds, 120_000)
        finally:
            await policy.stop(datetime.now())

    async def test_total_tool_limit_counts_non_exploration_calls_in_turn(self):
        policy = RuleBasedExplorationBudgetPolicy(max_total_tool_calls=4)
        await policy.start(None)
        try:
            action = await self.classifier.classify(search_call("limited-total"))
            decision = await policy.before_call(
                search_call("blocked-total"), action,
                ExplorationBudgetProbe(10, 36, 15, 40),
                ExplorationBudgetState(scored_actions=2),
            )
            self.assertEqual(decision.action, ExplorationBudgetAction.FOCUS)
            self.assertFalse(decision.focus_allows_call)
            self.assertEqual(decision.reason, "total_tool_action_limit")
        finally:
            await policy.stop(datetime.now())


class RepeatedSearchTool:
    descriptor = AdapterDescriptor(
        "fixture.repeated-search", "1", "ToolProviderPort", "1"
    )

    def __init__(self) -> None:
        self.calls = 0

    async def start(self, context): pass
    async def stop(self, deadline): pass
    async def health(self): return HealthStatus(HealthState.HEALTHY)
    async def list_tools(self):
        return (ToolSpec(
            "core.search_text", "Search repeated fixture results",
            {"type": "object", "properties": {
                "query": {"type": "string"}, "path": {"type": "string"},
                "max_matches": {"type": "integer"},
            }, "required": ["query"], "additionalProperties": False},
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
        ),)

    async def invoke(self, call, context):
        self.calls += 1
        return ToolResult(call.call_id, True, {
            "path": ".",
            "matches": [
                {"path": f"src/file-{index}.py", "line": index + 1}
                for index in range(8)
            ],
            "scanned_files": 8,
        })


class FocusSequenceTool:
    """Prove that focus blocks broad search but permits a known-file read."""

    descriptor = AdapterDescriptor(
        "fixture.focus-sequence", "1", "ToolProviderPort", "1"
    )

    def __init__(self) -> None:
        self.invoked: list[str] = []

    async def start(self, context): pass
    async def stop(self, deadline): pass
    async def health(self): return HealthStatus(HealthState.HEALTHY)
    async def list_tools(self):
        return (
            ToolSpec(
                "core.search_text", "Search fixture",
                {"type": "object", "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "max_matches": {"type": "integer"},
                }, "required": ["query"], "additionalProperties": False},
                ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
            ),
            ToolSpec(
                "core.read_file", "Read known fixture",
                {"type": "object", "properties": {
                    "path": {"type": "string"},
                }, "required": ["path"], "additionalProperties": False},
                ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
            ),
        )

    async def invoke(self, call, context):
        self.invoked.append(call.call_id)
        if call.name == "core.search_text":
            return ToolResult(call.call_id, True, {
                "matches": [{"path": "src/target.txt", "line": 1}],
                "scanned_files": 1,
            })
        return ToolResult(call.call_id, True, {
            "path": "src/target.txt", "sha256": "fixture-hash",
            "start_line": 1, "end_line": 1, "content": "direct fact",
        })


class FocusSequenceModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result for message in request.messages for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        if not results:
            call = ToolCall(
                "focus-broad-1", "core.search_text",
                {"query": "Target", "path": ".", "max_matches": 100},
                EvidenceQuestion("Q-focus", "Where is Target?"),
            )
        elif results[-1].call_id == "focus-broad-1":
            call = ToolCall(
                "focus-broad-2", "core.search_text",
                {"query": "Target", "path": ".", "max_matches": 100},
                EvidenceQuestion("Q-focus", "Where is Target?"),
            )
        elif results[-1].error_code == "EXPLORATION_FOCUS_REQUIRED":
            call = ToolCall(
                "focus-read", "core.read_file",
                {"path": "src/target.txt"},
                EvidenceQuestion("Q-focus", "What direct fact answers the goal?"),
            )
        else:
            return ModelResponse(Message(
                "focus-final", MessageRole.ASSISTANT,
                (TextBlock("confirmed from the known file"),),
            ))
        return ModelResponse(Message(
            call.call_id, MessageRole.ASSISTANT, (ToolCallBlock(call),)
        ), FinishReason.TOOL_CALL)


class BudgetSequenceModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result for message in request.messages for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        if results and results[-1].error_code == "EXPLORATION_BUDGET_WRAP_UP":
            return ModelResponse(Message(
                "budget-final", MessageRole.ASSISTANT,
                (TextBlock("budget wrapped up"),),
            ))
        call = search_call(f"budget-search-{len(results) + 1}")
        return ModelResponse(Message(
            call.call_id, MessageRole.ASSISTANT, (ToolCallBlock(call),)
        ), FinishReason.TOOL_CALL)


class ExplorationBudgetKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_focus_blocks_broad_search_then_allows_known_file_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = FocusSequenceTool()
            policy = RuleBasedExplorationBudgetPolicy(
                max_scored_actions=1, minimum_scored_actions=1,
            )
            app = compose_fixture_application(
                model_adapter=FocusSequenceModel(), tool_adapters=(tool,),
                require_evidence_questions=True,
                evidence_delta_evaluator_adapter=StructuredEvidenceDeltaEvaluator(),
                semantic_action_classifier_adapter=RuleBasedSemanticActionClassifier(),
                exploration_budget_policy_adapter=policy,
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("focus test", root)
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                progress = []
                result = await app.kernel.run_agent_turn(
                    task.task_id, "find then confirm", max_model_calls=8,
                    max_tool_calls=8, on_progress=progress.append,
                )
                self.assertEqual(
                    result.assistant_message.text,
                    "confirmed from the known file",
                )
                self.assertEqual(tool.invoked, ["focus-broad-1", "focus-read"])
                rejected = [
                    block.result for message in result.messages
                    for block in message.content
                    if isinstance(block, ToolResultBlock)
                    and block.result.call_id == "focus-broad-2"
                ]
                self.assertEqual(
                    rejected[0].error_code, "EXPLORATION_FOCUS_REQUIRED"
                )
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                self.assertEqual(
                    sum(event.event_type == "exploration_budget.focus_entered"
                        for event in events),
                    1,
                )
                self.assertFalse(any(
                    event.event_type == "exploration_budget.wrap_up_required"
                    and event.payload.get("reason") == "exploration_action_limit"
                    for event in events
                ))
                self.assertTrue(any(
                    item.kind is AgentProgressKind.FOCUS for item in progress
                ))
            finally:
                await app.registry.stop_all()

    async def test_kernel_soft_lands_before_fourth_repeated_search(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            tool = RepeatedSearchTool()
            app = compose_fixture_application(
                model_adapter=BudgetSequenceModel(), tool_adapters=(tool,),
                store_adapter=SQLiteRuntimeStore(database),
                require_evidence_questions=True,
                evidence_delta_evaluator_adapter=StructuredEvidenceDeltaEvaluator(),
                semantic_action_classifier_adapter=RuleBasedSemanticActionClassifier(),
                exploration_budget_policy_adapter=RuleBasedExplorationBudgetPolicy(),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "budget test", root, "task-exploration-budget"
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                progress = []
                result = await app.kernel.run_agent_turn(
                    task.task_id, "search until budget wrap-up",
                    on_progress=progress.append,
                )
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(result.assistant_message.text, "budget wrapped up")
                self.assertEqual(tool.calls, 3)
                self.assertEqual(result.tool_calls, 3)
                tool_results = [
                    block.result for message in result.messages
                    for block in message.content if isinstance(block, ToolResultBlock)
                ]
                self.assertEqual(
                    tool_results[-1].error_code, "EXPLORATION_BUDGET_WRAP_UP"
                )
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                scores = [
                    event.payload for event in events
                    if event.event_type == "exploration_budget.action_scored"
                ]
                self.assertEqual(len(scores), 3)
                self.assertEqual([item["value_band"] for item in scores], [
                    "useful", "low", "low",
                ])
                self.assertTrue(any(
                    event.event_type == "exploration_budget.wrap_up_required"
                    for event in events
                ))
                wrap_up = next(
                    item for item in progress
                    if item.kind is AgentProgressKind.WRAP_UP
                )
                self.assertEqual(wrap_up.goal, "budget test")
                self.assertEqual(wrap_up.budget_reason, "consecutive_low_value")
                self.assertEqual(wrap_up.exploration_actions, 3)
                self.assertEqual(wrap_up.max_exploration_actions, 24)
                self.assertEqual(wrap_up.exploration_tool_calls, 3)
                self.assertEqual(wrap_up.max_exploration_tool_calls, 24)
                self.assertEqual(wrap_up.low_value_streak, 2)
                self.assertEqual(wrap_up.max_low_value_streak, 2)
                payload = str([
                    event.payload for event in events
                    if event.event_type.startswith("exploration_budget.")
                ])
                self.assertNotIn("BudgetTarget", payload)
                self.assertNotIn("src/file-", payload)
            finally:
                await app.registry.stop_all()

            store = SQLiteRuntimeStore(database)
            await store.start(None)
            try:
                events = await store.read_events("task-exploration-budget")
                self.assertTrue(any(
                    event.event_type == "exploration_budget.wrap_up_required"
                    for event in events
                ))
            finally:
                await store.stop(datetime.now())


class ExplorationBudgetCompositionTest(unittest.TestCase):
    def test_fixture_policy_is_optional_and_replaceable(self):
        without = compose_fixture_application()
        self.assertEqual(without.registry.all(ExplorationBudgetPolicyPort), ())
        policy = RuleBasedExplorationBudgetPolicy()
        app = compose_fixture_application(exploration_budget_policy_adapter=policy)
        self.assertIs(app.registry.require(ExplorationBudgetPolicyPort), policy)

    def test_default_budget_is_not_the_old_twelve_call_experiment(self):
        configuration = ExplorationBudgetConfiguration()
        self.assertEqual(configuration.agent_max_model_calls, 15)
        self.assertEqual(configuration.finalization_model_calls, 2)
        self.assertEqual(configuration.max_tool_calls, 24)
        self.assertEqual(configuration.max_actions, 24)

    def test_workspace_budget_is_configurable_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "TSM_AGT_EXPLORATION_PROFILE=legacy\n"
                "TSM_AGT_EXPLORATION_MAX_TOOL_CALLS=30\n"
                "TSM_AGT_EXPLORATION_MAX_ACTIONS=28\n"
                "TSM_AGT_AGENT_FINALIZATION_MODEL_CALLS=3\n",
                encoding="utf-8",
            )
            environment = {"TSM_AGT_EXPLORATION_MAX_TOOL_CALLS": "36"}
            configuration = load_exploration_budget_configuration(
                env_file, environment
            )
            self.assertEqual(configuration.max_tool_calls, 36)
            self.assertEqual(configuration.max_actions, 28)
            self.assertEqual(configuration.finalization_model_calls, 3)
            self.assertEqual(configuration.profile, "legacy")
            self.assertEqual(
                configuration.sources["exploration_budget.profile"],
                "env_file",
            )
            self.assertEqual(
                configuration.sources["exploration_budget.max_tool_calls"],
                "environment",
            )
            self.assertEqual(
                configuration.sources["exploration_budget.max_actions"],
                "env_file",
            )
            self.assertEqual(
                configuration.sources[
                    "exploration_budget.finalization_model_calls"
                ],
                "env_file",
            )
            self.assertEqual(configuration.agent_max_tool_calls, 40)

    def test_invalid_budget_fails_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "TSM_AGT_EXPLORATION_MAX_TOOL_CALLS=not-a-number\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "TSM_AGT_EXPLORATION_MAX_TOOL_CALLS"
            ):
                load_exploration_budget_configuration(env_file, {})

    def test_invalid_exploration_profile_fails_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "TSM_AGT_EXPLORATION_PROFILE=unknown\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "balanced.*legacy"):
                load_exploration_budget_configuration(env_file, {})

    def test_soft_tool_limit_cannot_exceed_configured_hard_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "TSM_AGT_AGENT_MAX_TOOL_CALLS=20\n"
                "TSM_AGT_EXPLORATION_MAX_TOOL_CALLS=21\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "TSM_AGT_AGENT_MAX_TOOL_CALLS"
            ):
                load_exploration_budget_configuration(env_file, {})

    def test_finalization_reserve_must_be_below_model_hard_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "TSM_AGT_AGENT_MAX_MODEL_CALLS=3\n"
                "TSM_AGT_AGENT_FINALIZATION_MODEL_CALLS=3\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "TSM_AGT_AGENT_FINALIZATION_MODEL_CALLS"
            ):
                load_exploration_budget_configuration(env_file, {})


if __name__ == "__main__":
    unittest.main()
