import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider, EchoToolProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import AgentTurnResult, TaskState
from tsm_agt.ports import (
    AdapterDescriptor,
    FinishReason,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    TextBlock,
    ToolCall,
    ToolCallBlock,
    ToolIdempotency,
    ToolInvocationContext,
    ToolResult,
    ToolResultBlock,
    ToolRisk,
    ToolSpec,
)


class SensitiveEchoToolProvider(EchoToolProvider):
    _spec = ToolSpec(
        name="fixture.echo",
        description="Return a governed result to the model.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R0,
        is_read_only=True,
        idempotency=ToolIdempotency.IDEMPOTENT,
        max_result_tokens=16,
    )

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={"token": "prompt-secret", "payload": "x" * 400},
            message="Bearer prompt-secret",
            meta={"cookie": "prompt-secret"},
        )


class CapturingToolResultModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=16384)
    descriptor = AdapterDescriptor(
        adapter_id="fixture.capturing-tool-result-model",
        adapter_version="0.1.0",
        port_name="ModelProviderPort",
        port_version="1.0",
        capabilities=frozenset({"text", "tools"}),
    )

    def __init__(self) -> None:
        super().__init__()
        self.result_seen_by_model: ToolResult | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        tool_result = next(
            (
                block.result
                for message in request.messages
                for block in message.content
                if isinstance(block, ToolResultBlock)
            ),
            None,
        )
        if tool_result is not None:
            self.result_seen_by_model = tool_result
            return ModelResponse(
                Message(
                    "assistant-final", MessageRole.ASSISTANT, (TextBlock("governed"),)
                ),
                FinishReason.STOP,
                ModelUsage(1, 1),
            )
        return ModelResponse(
            Message(
                "assistant-tool",
                MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall("call-1", "fixture.echo", {"text": "inspect"})),),
            ),
            FinishReason.TOOL_CALL,
            ModelUsage(1, 1),
        )


class ToolResultGovernorAgentLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_next_model_call_receives_governed_tool_result_block(self) -> None:
        model = CapturingToolResultModel()
        application = compose_fixture_application(
            model_adapter=model, tool_adapters=(SensitiveEchoToolProvider(),)
        )
        await application.registry.start_all()
        temporary_directory = tempfile.TemporaryDirectory()
        try:
            task = await application.kernel.create_task(
                "govern tool result", Path(temporary_directory.name), task_id="task-governor"
            )
            for state in (
                TaskState.INTAKE,
                TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS,
                TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await application.kernel.transition_task(
                    task.task_id, state, f"move to {state.value}"
                )

            turn = await application.kernel.run_agent_turn(task.task_id, "inspect")

            self.assertIsInstance(turn, AgentTurnResult)
            self.assertIsNotNone(model.result_seen_by_model)
            observed = model.result_seen_by_model
            assert observed is not None
            self.assertTrue(observed.meta["result_governance"]["redacted"])
            self.assertFalse(observed.truncated)
            self.assertIsInstance(observed.data, dict)
            self.assertEqual(observed.data["token"], "[REDACTED]")
            self.assertEqual(
                observed.data["payload"], "x" * 400,
                "the model must receive the payload, not a compacted prefix",
            )
            self.assertTrue(observed.meta["result_governance"]["redacted"])
            self.assertNotIn("prompt-secret", json.dumps(observed.to_data()))
        finally:
            temporary_directory.cleanup()
            await application.registry.stop_all()
