import json
import asyncio

import pytest

from tsm_agt.adapters.openai_compatible.responses import OpenAIResponsesModelProvider
from tsm_agt.adapters.openai_compatible.model import OpenAICompatibleProviderError
from tsm_agt.adapters.resilient_model import ResilientModelProvider
from tsm_agt.adapters.rule_based_model_recovery import RuleBasedModelRecoveryPolicy
from tsm_agt.ports import (AdapterContext, Message, MessageRole, ModelRequest,
                           ModelReasoningDelta, ModelStreamCompleted, TextBlock,
                           ToolEffect, ToolIdempotency, ToolResult, ToolResultBlock,
                           ToolRisk, ToolSpec)
from tsm_agt.ports import ModelFailureCategory, ModelRecoveryExhausted, ModelRetrySafety
from tsm_agt.ports.model import ReasoningBlock


class Transport:
    def __init__(self, events):
        self.events = events
        self.payloads = []

    async def stream_sse(self, url, headers, payload, timeout_seconds, cancellation_scope=None):
        self.payloads.append(payload)
        for event in self.events:
            yield json.dumps(event)


async def _responses_reasoning_and_tool_result_roundtrip():
    reasoning = {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "opaque"}
    transport = Transport([
        {"type": "response.reasoning_summary_text.delta", "delta": "thinking"},
        {"type": "response.output_item.done", "item": reasoning},
        {"type": "response.completed", "response": {"status": "completed", "id": "resp_1", "output": [{"id": "rs_1", "type": "reasoning", "summary": []}, {"type": "function_call", "call_id": "call_1", "name": "core__read_file", "arguments": '{"path":"a"}'}], "usage": {"input_tokens": 1, "output_tokens": 2}}},
    ])
    provider = OpenAIResponsesModelProvider("https://example.test/v1", "model", "secret", transport=transport)
    await provider.start(AdapterContext({}, lambda *_: None))
    tool = ToolSpec(name="core.read_file", description="read", parameters={"type":"object", "properties":{"path":{"type":"string"}}, "required":["path"]}, risk=ToolRisk.R0, is_read_only=True, idempotency=ToolIdempotency.IDEMPOTENT, effect=ToolEffect.OBSERVE)
    user = Message("u", MessageRole.USER, (TextBlock("read a"),))
    request = ModelRequest("turn", (user,), tools=(tool,))
    events = [event async for event in provider.stream_complete(request)]
    assert isinstance(events[0], ModelReasoningDelta)
    assert isinstance(events[-1], ModelStreamCompleted)
    message = events[-1].response.message
    assert isinstance(message.content[0], ReasoningBlock)
    result = ToolResult("call_1", True, {"text": "x"})
    continuation = ModelRequest("turn", (user, message, Message("t", MessageRole.TOOL, (ToolResultBlock(result),))), tools=(tool,))
    payload, _ = provider._responses_payload(continuation)
    assert payload["reasoning"]["summary"] == "auto"
    assert reasoning in payload["input"]
    assert any(item.get("type") == "function_call" for item in payload["input"])
    assert any(item.get("type") == "function_call_output" and item["call_id"] == "call_1" for item in payload["input"])


async def _responses_incomplete_stream_is_not_success():
    transport = Transport([{"type": "response.reasoning_summary_text.delta", "delta": "partial"}])
    provider = OpenAIResponsesModelProvider("https://example.test/v1", "model", "secret", transport=transport)
    await provider.start(AdapterContext({}, lambda *_: None))
    with pytest.raises(Exception, match="before completion"):
        _ = [event async for event in provider.stream_complete(ModelRequest("turn", (Message("u", MessageRole.USER, (TextBlock("hi"),)),)))]


def test_responses_reasoning_and_tool_result_roundtrip():
    asyncio.run(_responses_reasoning_and_tool_result_roundtrip())


def test_responses_incomplete_stream_is_not_success():
    asyncio.run(_responses_incomplete_stream_is_not_success())


def test_gateway_parameter_error_preserves_detail_without_identical_retries():
    async def run():
        # The gateway accepted HTTP, then reported the upstream HTTP 400 as SSE.
        transport = Transport([{
            "type": "error", "code": "http_400", "param": None,
            "message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead.",
        }])
        physical = OpenAIResponsesModelProvider(
            "https://example.test/v1", "model", "secret", transport=transport,
        )
        policy = RuleBasedModelRecoveryPolicy(0)
        await policy.start(AdapterContext({}, lambda *_: None))
        provider = ResilientModelProvider(physical, policy, max_provider_attempts=3)
        await provider.start(AdapterContext({}, lambda *_: None))
        updates = []
        request = ModelRequest("turn", (), on_transport_progress=updates.append)
        with pytest.raises(ModelRecoveryExhausted) as caught:
            _ = [event async for event in provider.stream_complete(request)]
        assert len(transport.payloads) == 1
        assert caught.value.failure.category is ModelFailureCategory.CONFIGURATION
        assert caught.value.failure.diagnostic_code == "http_400"
        assert "max_completion_tokens" in str(caught.value)
        failed = next(item for item in updates if item.kind == "attempt_failed")
        assert "max_tokens" in failed.diagnostic_detail
    asyncio.run(run())


def test_response_failed_preserves_nested_provider_error_and_retry_class():
    async def run():
        transport = Transport([{
            "type": "response.failed",
            "response": {"status": "failed", "error": {
                "code": "server_error", "message": "upstream overloaded",
            }},
        }])
        provider = OpenAIResponsesModelProvider(
            "https://example.test/v1", "model", "secret", transport=transport,
        )
        await provider.start(AdapterContext({}, lambda *_: None))
        with pytest.raises(OpenAICompatibleProviderError) as caught:
            _ = [event async for event in provider.stream_complete(ModelRequest("turn", ()))]
        assert "upstream overloaded" in str(caught.value)
        assert caught.value.failure.category is ModelFailureCategory.TRANSIENT_PROVIDER
        assert caught.value.failure.retry_safety is ModelRetrySafety.SAFE_SAME_REQUEST
    asyncio.run(run())


def test_non_streaming_failed_response_preserves_error():
    provider = OpenAIResponsesModelProvider("https://example.test/v1", "model", "secret")
    with pytest.raises(OpenAICompatibleProviderError, match="model unavailable") as caught:
        provider._normalize_responses(ModelRequest("turn", ()), {
            "status": "failed", "error": {"code": "model_not_found", "message": "model unavailable"},
        }, {})
    assert caught.value.failure.retry_safety is ModelRetrySafety.NEVER
