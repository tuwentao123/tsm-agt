from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.rule_based_read_hits import RuleBasedReadHitsPolicy
from tsm_agt.adapters.rule_based_semantic_action import (
    RuleBasedSemanticActionClassifier,
)
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskState
from tsm_agt.ports import (
    EvidenceQuestion, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ProviderCapabilities, ReadHitsAction, ReadHitsPolicyPort,
    ReadHitsState, RuntimeStorePort, TextBlock, ToolCall, ToolCallBlock,
    ToolResult, ToolResultBlock,
)
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore


class RuleBasedReadHitsPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.policy = RuleBasedReadHitsPolicy()
        await self.policy.start(None)
        self.classifier = RuleBasedSemanticActionClassifier()
        await self.classifier.start(None)

    async def asyncTearDown(self) -> None:
        from datetime import datetime
        await self.policy.stop(datetime.now())
        await self.classifier.stop(datetime.now())

    @staticmethod
    def call(call_id: str, name: str, arguments: dict, question_id: str = "Q1"):
        return ToolCall(
            call_id, name, arguments,
            EvidenceQuestion(question_id, "Where is the target defined?"),
        )

    async def test_bounded_search_hits_require_candidate_read_before_research(self):
        first = self.call("s1", "core.search_text", {"query": "Target"})
        action = await self.classifier.classify(first)
        update = await self.policy.after_result(
            first, ToolResult(first.call_id, True, {
                "matches": [
                    {"path": "src/a.py", "line": 1},
                    {"path": "src/a.py", "line": 5},
                    {"path": "src/b.py", "line": 2},
                ]
            }), action, ReadHitsState(),
        )
        self.assertTrue(update.state.active)
        self.assertEqual(update.candidate_count, 2)
        second = self.call("s2", "code.definition", {"symbol": "Target"})
        decision = await self.policy.before_call(
            second, await self.classifier.classify(second), update.state
        )
        self.assertEqual(decision.action, ReadHitsAction.REQUIRE_READ)

        read = self.call("r1", "core.read_file", {"path": "src/a.py"})
        allowed = await self.policy.before_call(
            read, await self.classifier.classify(read), update.state
        )
        self.assertEqual(allowed.action, ReadHitsAction.ALLOW)
        consumed = await self.policy.after_result(
            read, ToolResult(read.call_id, True, {"path": "src/a.py"}),
            await self.classifier.classify(read), update.state,
        )
        self.assertFalse(consumed.state.active)

    async def test_zero_many_truncated_and_different_question_do_not_block(self):
        call = self.call("s1", "core.search_text", {"query": "Target"})
        action = await self.classifier.classify(call)
        for result in (
            ToolResult(call.call_id, True, {"matches": []}),
            ToolResult(call.call_id, True, {"matches": [
                {"path": f"src/{index}.py"} for index in range(6)
            ]}),
            ToolResult(call.call_id, True, {"matches": [{"path": "a.py"}]},
                       truncated=True),
        ):
            update = await self.policy.after_result(
                call, result, action, ReadHitsState()
            )
            self.assertFalse(update.state.active)

        bounded = await self.policy.after_result(
            call, ToolResult(call.call_id, True, {
                "matches": [{"path": "src/a.py"}]
            }), action, ReadHitsState(),
        )
        different = self.call(
            "s2", "core.search_text", {"query": "Other"}, "Q2"
        )
        decision = await self.policy.before_call(
            different, await self.classifier.classify(different), bounded.state
        )
        self.assertEqual(decision.action, ReadHitsAction.ALLOW)


class BlindThenReadModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result for message in request.messages
            for block in message.content if isinstance(block, ToolResultBlock)
        ]
        question = EvidenceQuestion("Q-target", "Where is Target defined?")
        if not results:
            call = ToolCall(
                "search-1", "core.search_text",
                {"query": "class Target", "path": "src"}, question,
            )
        elif len(results) == 1:
            call = ToolCall(
                "search-2", "core.search_text",
                {"query": "Target", "path": "."}, question,
            )
        elif results[-1].error_code == "READ_HITS_REQUIRED":
            call = ToolCall(
                "read-1", "core.read_file",
                {"path": "src/target.py", "max_lines": 20}, question,
            )
        else:
            return ModelResponse(Message(
                "final", MessageRole.ASSISTANT, (TextBlock("read target"),)
            ))
        return ModelResponse(Message(
            call.call_id, MessageRole.ASSISTANT, (ToolCallBlock(call),)
        ), FinishReason.TOOL_CALL)


class ReadHitsKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_kernel_blocks_blind_search_then_allows_candidate_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "target.py").write_text(
                "class Target:\n    pass\n", encoding="utf-8"
            )
            app = compose_fixture_application(
                model_adapter=BlindThenReadModel(),
                tool_adapters=(CoreReadOnlyToolProvider(),),
                require_evidence_questions=True,
                semantic_action_classifier_adapter=RuleBasedSemanticActionClassifier(),
                read_hits_policy_adapter=RuleBasedReadHitsPolicy(),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "find Target", root, "task-read-hits"
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
                    task.task_id, "locate Target", max_model_calls=8,
                    max_tool_calls=8,
                )
                self.assertEqual(result.assistant_message.text, "read target")
                self.assertEqual(result.tool_calls, 2)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                event_types = [event.event_type for event in events]
                self.assertEqual(event_types.count("read_hits.required"), 1)
                self.assertEqual(event_types.count("tool.requested"), 2)
                transitions = [
                    event.payload["transition"] for event in events
                    if event.event_type == "read_hits.state_changed"
                ]
                self.assertIn("candidates_detected", transitions)
                self.assertIn("consumed", transitions)
                required = next(
                    event.payload for event in events
                    if event.event_type == "read_hits.required"
                )
                self.assertNotIn("target.py", str(required))
                self.assertNotIn("Target", str(required))
            finally:
                await app.registry.stop_all()

    async def test_sqlite_restart_keeps_redacted_guard_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            (root / "src").mkdir()
            (root / "src" / "target.py").write_text(
                "class Target:\n    pass\n", encoding="utf-8"
            )
            first = compose_fixture_application(
                model_adapter=BlindThenReadModel(),
                tool_adapters=(CoreReadOnlyToolProvider(),),
                store_adapter=SQLiteRuntimeStore(database),
                require_evidence_questions=True,
                semantic_action_classifier_adapter=RuleBasedSemanticActionClassifier(),
                read_hits_policy_adapter=RuleBasedReadHitsPolicy(),
            )
            await first.registry.start_all()
            task = await first.kernel.create_task(
                "find Target", root, "task-read-hits-sqlite"
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await first.kernel.transition_task(
                    task.task_id, state, state.value
                )
            await first.kernel.run_agent_turn(task.task_id, "locate Target")
            await first.registry.stop_all()

            second_store = SQLiteRuntimeStore(database)
            await second_store.start(None)
            try:
                events = await second_store.read_events(task.task_id)
                required = next(
                    event for event in events
                    if event.event_type == "read_hits.required"
                )
                self.assertEqual(required.payload["candidate_count"], 1)
                self.assertNotIn("Target", str(required.payload))
                self.assertNotIn("target.py", str(required.payload))
                transitions = [
                    event.payload["transition"] for event in events
                    if event.event_type == "read_hits.state_changed"
                ]
                self.assertEqual(
                    transitions, ["candidates_detected", "consumed"]
                )
            finally:
                from datetime import datetime
                await second_store.stop(datetime.now())


class ReadHitsCompositionTest(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_policy_is_optional_and_replaceable(self):
        without = compose_fixture_application()
        self.assertEqual(without.registry.all(ReadHitsPolicyPort), ())
        policy = RuleBasedReadHitsPolicy()
        with_policy = compose_fixture_application(read_hits_policy_adapter=policy)
        self.assertIs(with_policy.registry.require(ReadHitsPolicyPort), policy)


if __name__ == "__main__":
    unittest.main()
