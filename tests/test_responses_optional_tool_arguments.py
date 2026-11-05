"""Keep optional tool arguments optional across the Responses API boundary."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.openai_compatible.responses import OpenAIResponsesModelProvider
from tsm_agt.core.tool import InvalidToolArguments, validate_tool_arguments
from tsm_agt.ports import (
    AdapterContext, Message, MessageRole, ModelRequest, ModelStreamCompleted,
    TextBlock, ToolCall, ToolCallBlock, ToolInvocationContext, ToolRecoveryKind,
)


class ToolResponseTransport:
    def __init__(self):
        self.payloads = []
        self.response = {
            "id": "response-read", "status": "completed",
            "output": [{"type": "function_call", "call_id": "call-read",
                        "name": "core__read_file", "arguments": '{"path":"sample.txt"}'}],
        }

    async def post_json(self, url, headers, payload, timeout_seconds):
        self.payloads.append(payload)
        return self.response

    async def stream_sse(self, url, headers, payload, timeout_seconds, cancellation_scope=None):
        self.payloads.append(payload)
        yield json.dumps({"type": "response.completed", "response": self.response})


@pytest.mark.parametrize("streaming", [False, True])
def test_non_strict_responses_explicitly_opt_out_for_optional_file_arguments(streaming):
    async def run():
        tool = next(tool for tool in await CoreReadOnlyToolProvider().list_tools()
                    if tool.name == "core.read_file")
        transport = ToolResponseTransport()
        model = OpenAIResponsesModelProvider(
            "https://example.test/v1", "fixture-model", "test-key",
            strict_tool_schema=False, transport=transport,
        )
        await model.start(AdapterContext({}, lambda *_: None))
        request = ModelRequest("turn-read", (Message("user", MessageRole.USER,
                               (TextBlock("Read sample.txt"),)),), tools=(tool,))
        if streaming:
            events = [event async for event in model.stream_complete(request)]
            assert isinstance(events[-1], ModelStreamCompleted)
            response = events[-1].response
        else:
            response = await model.complete(request)
        advertised = transport.payloads[0]["tools"][0]
        # Omitting this field permits the API to normalize optional fields into
        # strict, required fields. False must survive configuration → wire.
        assert advertised.get("strict") is False
        assert advertised["parameters"] == tool.parameters
        call = next(block.call for block in response.message.content if isinstance(block, ToolCallBlock))
        assert call.arguments == {"path": "sample.txt"}
        validate_tool_arguments(tool, call.arguments)

    asyncio.run(run())


@pytest.mark.parametrize("arguments", [
    {}, {"path": ""}, {"resource_ref": ""},
    {"path": "sample.txt", "resource_ref": ""},
    {"path": "", "resource_ref": "resource-example"},
    {"path": "sample.txt", "resource_ref": "resource-example"},
])
def test_read_target_invalid_combinations_fail_before_dispatch(arguments):
    async def run():
        tool = next(tool for tool in await CoreReadOnlyToolProvider().list_tools()
                    if tool.name == "core.read_file")
        with pytest.raises(InvalidToolArguments):
            validate_tool_arguments(tool, arguments)
    asyncio.run(run())


@pytest.mark.parametrize("arguments", [
    {"path": "sample.txt"}, {"resource_ref": "resource-example"},
])
def test_read_target_accepts_either_single_target(arguments):
    async def run():
        tool = next(tool for tool in await CoreReadOnlyToolProvider().list_tools()
                    if tool.name == "core.read_file")
        validate_tool_arguments(tool, arguments)
    asyncio.run(run())


def test_provider_argument_error_reports_correctable_arguments():
    async def run():
        provider = CoreReadOnlyToolProvider()
        await provider.start(AdapterContext({}, lambda *_: None))
        context = ToolInvocationContext(
            "invoke", "task", "turn", Path("."),
            datetime.now(timezone.utc) + timedelta(seconds=5),
        )
        result = await provider.invoke(ToolCall("read", "core.read_file", {
            "path": "sample.txt", "resource_ref": "",
        }), context)
        assert not result.ok and result.error_code == "INVALID_PARAM"
        assert result.recovery_kind is ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS
        assert result.retryable
    asyncio.run(run())
