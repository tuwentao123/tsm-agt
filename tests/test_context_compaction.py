from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    ContextWindowExceeded, ContextWindowManager, PromptTemplate, TaskState,
)
from tsm_agt.ports import (
    FinishReason, Message, MessageRole, ModelRequest, ModelResponse,
    ProviderCapabilities, RuntimeStorePort, TextBlock, ToolCall, ToolCallBlock,
    ToolResult, ToolResultBlock,
    ToolRisk, ToolSpec,
)


def text(message_id: str, role: MessageRole, value: str) -> Message:
    return Message(message_id, role, (TextBlock(value),))


def tool_pair(call_id: str, payload: str) -> tuple[Message, Message]:
    call = ToolCall(call_id, "fixture.echo", {"text": payload})
    return (
        Message(
            f"assistant-{call_id}", MessageRole.ASSISTANT,
            (ToolCallBlock(call),),
        ),
        Message(
            f"tool-{call_id}", MessageRole.TOOL,
            (ToolResultBlock(ToolResult(call_id, True, data={"text": payload})),),
        ),
    )


class ContextWindowManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.template = PromptTemplate.default()

    def conversation(self) -> tuple[Message, ...]:
        first = text("goal", MessageRole.USER, "keep the original goal")
        pair1 = tool_pair("call-1", "old observation " * 30)
        bridge = text("assistant-bridge", MessageRole.ASSISTANT, "continue")
        pair2 = tool_pair("call-2", "recent observation " * 20)
        final = text("assistant-final", MessageRole.ASSISTANT, "working")
        return (first, *pair1, bridge, *pair2, final)

    def test_compacts_complete_old_groups_and_preserves_recent_messages(self) -> None:
        manager = ContextWindowManager(trigger_ratio=0.70, recent_message_floor=3)
        conversation = self.conversation()
        prepared = manager.prepare(
            conversation=conversation, tools=(), prompt_template=self.template,
            context_window=1300, max_output_tokens=128,
        )

        self.assertIsNotNone(prepared.compaction)
        self.assertEqual(prepared.messages[0], conversation[0])
        self.assertTrue(prepared.messages[1].message_id.startswith("context-summary-"))
        self.assertEqual(prepared.messages[-3:], conversation[-3:])
        roles = [message.role for message in prepared.messages]
        self.assertNotIn(MessageRole.TOOL, roles[1:2])
        receipt = prepared.compaction
        assert receipt is not None
        self.assertLess(receipt.after_estimated_tokens, receipt.before_estimated_tokens)
        self.assertFalse(receipt.event_data()["summary_body_persisted"])
        rendered = json.dumps(receipt.event_data())
        self.assertNotIn("old observation", rendered)
        self.assertNotIn("recent observation", rendered)

    def test_does_not_split_multi_tool_call_and_result_group(self) -> None:
        first = text("goal", MessageRole.USER, "goal")
        call1 = ToolCall("call-a", "fixture.echo", {"text": "a"})
        call2 = ToolCall("call-b", "fixture.echo", {"text": "b"})
        assistant = Message(
            "assistant", MessageRole.ASSISTANT,
            (ToolCallBlock(call1), ToolCallBlock(call2)),
        )
        result1 = Message(
            "tool-a", MessageRole.TOOL,
            (ToolResultBlock(ToolResult("call-a", True, data={})),),
        )
        result2 = Message(
            "tool-b", MessageRole.TOOL,
            (ToolResultBlock(ToolResult("call-b", True, data={})),),
        )
        groups = ContextWindowManager._atomic_groups((
            first, assistant, result1, result2,
            text("final", MessageRole.ASSISTANT, "done"),
        ))

        self.assertEqual(groups[1], (assistant, result1, result2))

    def test_unknown_provider_window_disables_compaction_explicitly(self) -> None:
        manager = ContextWindowManager(recent_message_floor=1)
        prepared = manager.prepare(
            conversation=self.conversation(), tools=(), prompt_template=self.template,
            context_window=0, max_output_tokens=128,
        )

        self.assertIsNone(prepared.compaction)
        self.assertEqual(prepared.messages, self.conversation())
        self.assertEqual(
            prepared.budget.estimation_method, "disabled-provider-window-unknown"
        )

    def test_policy_validation_and_snapshot_are_explicit(self) -> None:
        with self.assertRaisesRegex(ValueError, "trigger_ratio"):
            ContextWindowManager(trigger_ratio=1.0)
        with self.assertRaisesRegex(ValueError, "recent_message_floor"):
            ContextWindowManager(recent_message_floor=0)
        with self.assertRaisesRegex(ValueError, "latency_soft_input_tokens"):
            ContextWindowManager(latency_soft_input_tokens=-1)
        data = ContextWindowManager().snapshot_data()
        self.assertEqual(data["trigger_ratio"], 0.80)
        self.assertEqual(data["latency_soft_input_tokens"], 0)
        self.assertTrue(data["preserve_tool_call_result_groups"])
        self.assertTrue(data["preserve_session_and_working_memory"])

    def test_large_provider_window_compacts_at_latency_soft_limit(self) -> None:
        conversation = (
            text("goal", MessageRole.USER, "current request"),
            *tool_pair("old", "large search result " * 500),
            text("bridge", MessageRole.ASSISTANT, "continue"),
            *tool_pair("recent", "recent evidence " * 10),
        )
        manager = ContextWindowManager(
            recent_message_floor=2, latency_soft_input_tokens=2_000
        )
        prepared = manager.prepare(
            conversation=conversation, tools=(), prompt_template=self.template,
            context_window=128_000, max_output_tokens=1_024,
        )
        self.assertIsNotNone(prepared.compaction)
        self.assertEqual(prepared.budget.trigger_tokens, 2_000)

    def test_default_large_window_uses_ratio_not_old_40k_soft_limit(self):
        manager = ContextWindowManager()
        prepared = manager.prepare(
            conversation=(text("goal", MessageRole.USER, "small"),),
            tools=(), prompt_template=self.template,
            context_window=128_000, max_output_tokens=1_024,
        )
        self.assertEqual(prepared.budget.trigger_tokens, 102_400)

    def test_summary_keeps_candidate_paths_but_not_source_text(self) -> None:
        call = ToolCall(
            "search-1", "core.search_text",
            {"query": "Target", "path": "."},
        )
        assistant = Message(
            "assistant-search", MessageRole.ASSISTANT, (ToolCallBlock(call),)
        )
        result = Message(
            "tool-search", MessageRole.TOOL,
            (ToolResultBlock(ToolResult(
                "search-1", True, {
                    "matches": [{
                        "path": "src/target.py", "line": 4,
                        "text": "SECRET_SOURCE_BODY",
                    }],
                },
            )),),
        )
        summary = ContextWindowManager._summary_message((assistant, result))
        self.assertIn("src/target.py", summary.text)
        self.assertIn("Target", summary.text)
        self.assertNotIn("SECRET_SOURCE_BODY", summary.text)

    def test_budget_reports_every_prompt_section_and_reserved_output(self) -> None:
        tools = (ToolSpec(
            "fixture.echo", "Echo one value",
            {"type": "object", "properties": {"text": {"type": "string"}}},
            ToolRisk.R0,
        ),)
        conversation = (
            text("goal", MessageRole.USER, "current request"),
            text("project-memory-context-a", MessageRole.USER, "project data"),
            text("session-context-2-a", MessageRole.USER, "session data"),
            text("working-memory-context-2-a", MessageRole.USER, "plan evidence"),
        )
        prepared = ContextWindowManager().prepare(
            conversation=conversation, tools=tools, prompt_template=self.template,
            context_window=8000, max_output_tokens=512,
            runtime_instruction="finish now",
        )

        report = prepared.budget.event_data()
        names = {item["name"] for item in report["allocations"]}
        self.assertEqual(names, {
            "static_system", "runtime_instruction", "project_context",
            "session_history", "working_memory",
            "current_turn_and_tool_protocol", "tool_schemas",
            "serialization_overhead",
        })
        self.assertEqual(report["reserved_output_tokens"], 512)
        self.assertEqual(
            report["estimated_total_reserved_tokens"],
            report["estimated_input_tokens"] + 512,
        )

    def test_compaction_never_replaces_session_or_working_memory(self) -> None:
        manager = ContextWindowManager(trigger_ratio=0.50, recent_message_floor=1)
        session = text(
            "session-context-8-a", MessageRole.USER,
            '{"working_state":{"goal":"ship","constraints":["safe"]}}',
        )
        working = text(
            "working-memory-context-4-a", MessageRole.USER,
            '{"goal":"ship","plan":["test"],"evidence":["event:7"]}',
        )
        conversation = (
            text("goal", MessageRole.USER, "current request"), session, working,
            *tool_pair("old-1", "old observation " * 60),
            text("bridge", MessageRole.ASSISTANT, "continue " * 20),
            *tool_pair("recent", "recent observation " * 10),
        )
        prepared = manager.prepare(
            conversation=conversation, tools=(), prompt_template=self.template,
            context_window=1500, max_output_tokens=128,
        )

        self.assertIsNotNone(prepared.compaction)
        self.assertIn(session, prepared.messages)
        self.assertIn(working, prepared.messages)
        assert prepared.compaction is not None
        self.assertIn(
            "task_goal_constraints_plan_evidence",
            prepared.compaction.protected_sections,
        )

    def test_session_compaction_preserves_all_task_indexes_and_artifacts(self) -> None:
        manager = ContextWindowManager(trigger_ratio=0.50, recent_message_floor=2)
        session_body = {
            "boundary": "session_conversation_projection",
            "work_state": {"goal": "continue design"},
            "task_index": [
                {
                    "task_id": f"task-{index}",
                    "goal": f"goal {index}",
                    "status": "SUCCEEDED",
                    "artifacts": [f"plan-{index}.md"],
                    "source_event_sequences": [index],
                }
                for index in range(1, 7)
            ],
            "recent_task_summaries": [
                {
                    "task_id": f"task-{index}",
                    "goal": "detail " * 100,
                }
                for index in range(1, 7)
            ],
            "historical_investigation": {
                "resources": [
                    {"source_task_id": f"task-{index}",
                     "canonical_path": f"plan-{index}.md"}
                    for index in range(1, 7)
                ],
                "questions": [],
            },
            "recent_messages": [
                {
                    "task_id": f"task-{index}", "role": "assistant",
                    "text": "large visible result " * 100,
                }
                for index in range(1, 7)
            ],
        }
        session = text(
            "session-context-12-a", MessageRole.USER,
            json.dumps(session_body),
        )
        prepared = manager.prepare(
            conversation=(
                text("goal", MessageRole.USER, "current request"), session,
            ),
            tools=(), prompt_template=self.template,
            context_window=3000, max_output_tokens=256,
        )

        self.assertIsNotNone(prepared.compaction)
        compacted_session = next(
            item for item in prepared.messages
            if item.message_id.startswith("session-context-")
        )
        body = json.loads(compacted_session.text)
        self.assertEqual(
            [item["task_id"] for item in body["task_index"]],
            [f"task-{index}" for index in range(1, 7)],
        )
        self.assertEqual(
            [item["artifacts"] for item in body["task_index"]],
            [[f"plan-{index}.md"] for index in range(1, 7)],
        )
        self.assertEqual(len(body["recent_messages"]), 2)
        self.assertEqual(
            [item["task_id"] for item in body["recent_task_summaries"]],
            ["task-5", "task-6"],
        )
        assert prepared.compaction is not None
        self.assertIn(
            "session_task_index", prepared.compaction.protected_sections
        )
        self.assertIn(
            "session_artifact_paths", prepared.compaction.protected_sections
        )

    def test_fails_when_protected_context_cannot_fit(self) -> None:
        manager = ContextWindowManager(recent_message_floor=12)
        with self.assertRaisesRegex(ContextWindowExceeded, "cannot fit"):
            manager.prepare(
                conversation=(text("goal", MessageRole.USER, "x" * 4000),),
                tools=(), prompt_template=self.template,
                context_window=600, max_output_tokens=128,
            )

    def test_oversized_tool_schemas_fail_with_budget_diagnosis(self) -> None:
        tools = tuple(ToolSpec(
            f"fixture.tool{index}", "large schema " * 40,
            {"type": "object", "properties": {
                f"value{index}": {"type": "string", "description": "x" * 500}
            }}, ToolRisk.R0,
        ) for index in range(5))
        with self.assertRaisesRegex(ContextWindowExceeded, "tool_schemas="):
            ContextWindowManager(recent_message_floor=1).prepare(
                conversation=(text("goal", MessageRole.USER, "small"),),
                tools=tools, prompt_template=self.template,
                context_window=900, max_output_tokens=128,
            )

    def test_repeated_compaction_flattens_prior_summary(self) -> None:
        manager = ContextWindowManager(trigger_ratio=0.70, recent_message_floor=1)
        first = manager.prepare(
            conversation=self.conversation(), tools=(), prompt_template=self.template,
            context_window=1100, max_output_tokens=128,
        )
        extended = first.messages + tool_pair("call-3", "new data " * 40)
        second = manager.prepare(
            conversation=extended, tools=(), prompt_template=self.template,
            context_window=1100, max_output_tokens=128,
        )

        summaries = [
            message for message in second.messages
            if message.message_id.startswith("context-summary-")
        ]
        self.assertEqual(len(summaries), 1)
        self.assertNotIn("context-summary-", summaries[0].text)


class GrowingContextModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=1800)

    def __init__(self) -> None:
        super().__init__()
        self.calls_made = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        calls = self.calls_made
        if calls < 4:
            self.calls_made += 1
            call = ToolCall(
                f"grow-{calls}", "fixture.echo",
                {"text": "large untrusted observation " * 25},
            )
            return ModelResponse(
                Message(
                    f"assistant-{calls}", MessageRole.ASSISTANT,
                    (ToolCallBlock(call),),
                ), FinishReason.TOOL_CALL,
            )
        return ModelResponse(
            text("assistant-final", MessageRole.ASSISTANT, "done"),
            FinishReason.STOP,
        )


class ContextCompactionKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_text_turn_persists_unified_budget_without_prompt_bodies(self) -> None:
        application = compose_fixture_application(tool_adapters=())
        await application.registry.start_all()
        temporary = tempfile.TemporaryDirectory()
        try:
            task = await application.kernel.create_task(
                "budget text turn", Path(temporary.name)
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await application.kernel.transition_task(
                    task.task_id, state, state.value
                )
            await application.kernel.run_text_turn(task.task_id, "hello")
            events = await application.registry.require(
                RuntimeStorePort
            ).read_events(task.task_id)
            completed = next(
                event for event in events if event.event_type == "llm.completed"
            )
            budget = completed.payload["context_budget"]
            self.assertEqual(budget["reserved_output_tokens"], 1024)
            self.assertIn("allocations", budget)
            encoded = json.dumps(budget)
            self.assertNotIn("hello", encoded)
            self.assertNotIn("You are tsm-agt", encoded)
        finally:
            temporary.cleanup()
            await application.registry.stop_all()

    async def test_agent_compaction_is_persisted_without_summary_body(self) -> None:
        application = compose_fixture_application(
            model_adapter=GrowingContextModel(),
            context_manager=ContextWindowManager(
                trigger_ratio=0.70, recent_message_floor=2
            ),
        )
        await application.registry.start_all()
        temporary = tempfile.TemporaryDirectory()
        try:
            task = await application.kernel.create_task(
                "grow context", Path(temporary.name)
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await application.kernel.transition_task(
                    task.task_id, state, state.value
                )
            result = await application.kernel.run_agent_turn(
                task.task_id, "inspect", max_output_tokens=128
            )
            self.assertEqual(result.assistant_message.text, "done")
            store = application.registry.require(RuntimeStorePort)
            events = await store.read_events(task.task_id)
            compacted = [
                event for event in events if event.event_type == "context.compacted"
            ]
            self.assertGreaterEqual(len(compacted), 1)
            for event in compacted:
                encoded = json.dumps(event.payload)
                self.assertNotIn("large untrusted observation", encoded)
                self.assertFalse(event.payload["summary_body_persisted"])
            projection = await application.kernel.get_flow_projection(task.task_id)
            node = next(
                node for node in projection.nodes
                if node.label == "Context compacted"
            )
            diagnostic = projection.inspect_node(node.node_id)
            fact = next(
                fact for fact in diagnostic.facts
                if fact.code == "context.compaction-summary"
            )
            values = dict(fact.values)
            self.assertGreater(
                values["before_estimated_tokens"],
                values["after_estimated_tokens"],
            )
        finally:
            temporary.cleanup()
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
