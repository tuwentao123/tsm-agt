from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.rule_based_semantic_action import (
    RuleBasedSemanticActionClassifier,
)
from tsm_agt.adapters.structured_evidence import StructuredEvidenceDeltaEvaluator
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskState
from tsm_agt.ports import (
    AdapterDescriptor, EvidenceQuestion, FinishReason, HealthState, HealthStatus,
    Message, MessageRole, ModelRequest, ModelResponse, ProviderCapabilities,
    RuntimeStorePort, TextBlock, ToolCall, ToolCallBlock, ToolIdempotency,
    ToolResult, ToolRisk, ToolSpec,
)


class OneSearchModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if any(message.role is MessageRole.TOOL for message in request.messages):
            return ModelResponse(Message(
                "final", MessageRole.ASSISTANT, (TextBlock("done"),)
            ))
        return ModelResponse(
            Message(
                "search", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "search-call", "fixture.search",
                    {"query": "CustomerName", "path": "private/module"},
                    EvidenceQuestion("E1", "Where is CustomerName defined?"),
                )),),
            ), FinishReason.TOOL_CALL,
        )


class SearchTool:
    descriptor = AdapterDescriptor(
        "fixture.search-tool", "1", "ToolProviderPort", "1"
    )

    async def start(self, context):
        pass

    async def stop(self, deadline):
        pass

    async def health(self):
        return HealthStatus(HealthState.HEALTHY)

    async def list_tools(self):
        return (ToolSpec(
            "fixture.search", "Search fixture data",
            {"type": "object", "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
            }, "required": ["query"], "additionalProperties": False},
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
        ),)

    async def invoke(self, call, context):
        return ToolResult(call.call_id, True, {"matches": []})


class SemanticActionKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_kernel_persists_redacted_semantic_action_before_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=OneSearchModel(), tool_adapters=(SearchTool(),),
                require_evidence_questions=True,
                semantic_action_classifier_adapter=RuleBasedSemanticActionClassifier(),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "find private symbol", Path(directory), "task-semantic"
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                await app.kernel.run_agent_turn(task.task_id, "inspect")
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                types = [event.event_type for event in events]
                semantic_index = types.index("semantic.action_classified")
                self.assertLess(semantic_index, types.index("tool.requested"))
                payload = events[semantic_index].payload
                self.assertEqual(payload["family"], "USE_TOOL")
                self.assertIn("semantic_signature", payload)
                self.assertNotIn("CustomerName", str(payload))
                self.assertNotIn("private/module", str(payload))
                plan = next(
                    event for event in events
                    if event.event_type == "plan.action_evaluated"
                )
                self.assertEqual(
                    plan.payload["semantic_signature"],
                    payload["semantic_signature"],
                )
            finally:
                await app.registry.stop_all()


class ScopedSearchModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if not request.allow_tool_calls:
            return ModelResponse(Message(
                "stopped", MessageRole.ASSISTANT,
                (TextBlock("Stopped after semantic no progress."),),
            ))
        paths = (
            ".", "modules", "modules/im", "modules/im/ui",
            "modules/im/ui/dialog",
        )
        path = paths[min(self.calls - 1, len(paths) - 1)]
        return ModelResponse(
            Message(
                f"search-{self.calls}", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    f"search-{self.calls}", "core.search_text",
                    {"query": "MissingDialog", "path": path},
                    EvidenceQuestion(
                        "E-missing", "Where is MissingDialog defined?"
                    ),
                )),),
            ), FinishReason.TOOL_CALL,
        )


class EmptySearchTool:
    descriptor = AdapterDescriptor(
        "fixture.empty-search-tool", "1", "ToolProviderPort", "1"
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
            "core.search_text", "Search fixture data",
            {"type": "object", "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
            }, "required": ["query"], "additionalProperties": False},
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
        ),)

    async def invoke(self, call, context):
        self.calls += 1
        # The result deliberately omits scope-specific metadata, so only the first
        # observation adds evidence; later scopes are semantic repetition.
        return ToolResult(call.call_id, True, {"observation": "unchanged"})


class SemanticNoProgressKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_changed_scopes_same_semantic_action_stop_after_three_zero_deltas(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = EmptySearchTool()
            app = compose_fixture_application(
                model_adapter=ScopedSearchModel(), tool_adapters=(tool,),
                require_evidence_questions=True,
                evidence_delta_evaluator_adapter=StructuredEvidenceDeltaEvaluator(),
                semantic_action_classifier_adapter=RuleBasedSemanticActionClassifier(),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "find missing symbol", Path(directory), "task-semantic-stop"
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
                    task.task_id, "inspect", max_model_calls=10, max_tool_calls=10
                )
                self.assertEqual(tool.calls, 4)
                self.assertEqual(result.tool_calls, 4)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                classified = [
                    event.payload for event in events
                    if event.event_type == "semantic.action_classified"
                ]
                self.assertEqual(
                    len({item["semantic_signature"] for item in classified}), 1
                )
                self.assertEqual(len({item["scope_hash"] for item in classified}), 5)
                stopped = next(
                    event for event in events
                    if event.event_type == "plan.no_progress_stopped"
                )
                self.assertEqual(stopped.payload["consecutive_no_progress"], 3)
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
