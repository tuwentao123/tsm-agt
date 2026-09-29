import json
import unittest
from collections.abc import Mapping
from typing import Any

from tsm_agt.adapters.anthropic_messages import AnthropicMessagesModelProvider
from tsm_agt.adapters.openai_compatible import OpenAICompatibleProviderError
from tsm_agt.ports import (
    AdapterContext,
    EvidenceQuestion,
    FinishReason,
    ImageBlock,
    Message,
    MessageRole,
    ModelRequest,
    ModelStreamCompleted,
    ModelTextDelta,
    TextBlock,
    ToolCall,
    ToolCallBlock,
    ToolEffect,
    ToolIdempotency,
    ToolResult,
    ToolResultBlock,
    ToolRisk,
    ToolSpec,
)


class RecordingTransport:
    def __init__(self, responses: list[Mapping[str, Any]] | None = None, events: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.events = list(events or [])
        self.requests: list[dict[str, Any]] = []

    async def post_json(self, url, headers, payload, timeout_seconds):
        self.requests.append({"url": url, "headers": dict(headers), "payload": dict(payload)})
        return self.responses.pop(0)

    async def stream_sse(self, url, headers, payload, timeout_seconds, cancellation_scope=None):
        self.requests.append({"url": url, "headers": dict(headers), "payload": dict(payload)})
        for event in self.events:
            yield event


class AnthropicMessagesModelProviderTest(unittest.IsolatedAsyncioTestCase):
    async def _provider(self, transport: RecordingTransport, *, streaming: bool = False):
        provider = AnthropicMessagesModelProvider(
            "https://api.anthropic.example", "claude-test", "secret",
            streaming=streaming, transport=transport,
        )
        await provider.start(AdapterContext({}, lambda _kind, _payload: None))
        return provider

    @staticmethod
    def _tool() -> ToolSpec:
        return ToolSpec(
            name="core.read_file",
            description="Read a file.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            risk=ToolRisk.R0,
            is_read_only=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=ToolEffect.OBSERVE,
        )

    async def test_posts_native_body_headers_system_image_and_tools(self):
        transport = RecordingTransport([{
            "id": "msg-1", "model": "claude-served", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "OK"}],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }])
        provider = await self._provider(transport)
        response = await provider.complete(ModelRequest(
            "turn-1",
            (
                Message("system", MessageRole.SYSTEM, (TextBlock("be precise"),)),
                Message("user", MessageRole.USER, (
                    TextBlock("look"), ImageBlock("data:image/png;base64,AAAA"),
                )),
            ),
            max_output_tokens=77,
            tools=(self._tool(),),
        ))
        request = transport.requests[0]
        self.assertEqual(request["url"], "https://api.anthropic.example/v1/messages")
        self.assertEqual(request["headers"]["x-api-key"], "secret")
        self.assertEqual(request["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(request["payload"]["max_tokens"], 77)
        self.assertNotIn("max_completion_tokens", request["payload"])
        # The Anthropic Messages gateway this adapter targets documents `system`
        # as a string, so multiple internal system messages are ordered and then
        # joined into one string instead of being sent as content blocks.
        self.assertEqual(request["payload"]["system"], "be precise")
        self.assertEqual(request["payload"]["messages"][0]["content"][1], {
            "type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
        })
        self.assertEqual(request["payload"]["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(request["payload"]["tool_choice"], {"type": "auto"})
        self.assertEqual(response.message.text, "OK")
        self.assertEqual(response.finish_reason, FinishReason.STOP)
        self.assertEqual(response.usage.output_tokens, 1)

    async def test_round_trips_tool_use_and_tool_result(self):
        transport = RecordingTransport([{
            "id": "msg-tool", "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "toolu-1", "name": "core__read_file", "input": {"path": "README.md"}}],
            "usage": {},
        }])
        provider = await self._provider(transport)
        response = await provider.complete(ModelRequest(
            "turn-tool", (Message("user", MessageRole.USER, (TextBlock("read"),)),), tools=(self._tool(),),
        ))
        self.assertEqual(response.finish_reason, FinishReason.TOOL_CALL)
        self.assertEqual(response.message.content[0].call, ToolCall("toolu-1", "core.read_file", {"path": "README.md"}))

        continuation = RecordingTransport([{
            "id": "msg-final", "stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}], "usage": {},
        }])
        provider = await self._provider(continuation)
        call = ToolCall("toolu-1", "core.read_file", {"path": "README.md"})
        result = ToolResult("toolu-1", True, data={"content": "hello"})
        await provider.complete(ModelRequest("turn-result", (
            Message("user", MessageRole.USER, (TextBlock("read"),)),
            Message("assistant", MessageRole.ASSISTANT, (ToolCallBlock(call),)),
            Message("tool", MessageRole.TOOL, (ToolResultBlock(result),)),
        ), tools=(self._tool(),)))
        tool_result = continuation.requests[0]["payload"]["messages"][2]
        self.assertEqual(tool_result["role"], "user")
        self.assertEqual(tool_result["content"][0]["type"], "tool_result")
        self.assertEqual(tool_result["content"][0]["tool_use_id"], "toolu-1")
        self.assertIn('"ok":true', tool_result["content"][0]["content"])

    async def test_streams_anthropic_text_and_tool_input_json(self):
        transport = RecordingTransport(events=[
            json.dumps({"type": "message_start", "message": {"id": "msg-stream", "model": "claude", "usage": {"input_tokens": 3, "output_tokens": 0}}}),
            json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello "}}),
            json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "world"}}),
            json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
            json.dumps({"type": "message_stop"}),
        ])
        provider = await self._provider(transport, streaming=True)
        events = [event async for event in provider.stream_complete(ModelRequest(
            "turn-stream", (Message("user", MessageRole.USER, (TextBlock("hello"),)),),
        ))]
        self.assertEqual([event.text for event in events if isinstance(event, ModelTextDelta)], ["hello ", "world"])
        final = next(event.response for event in events if isinstance(event, ModelStreamCompleted))
        self.assertEqual(final.message.text, "hello world")
        self.assertEqual(final.usage.input_tokens, 3)
        self.assertEqual(final.usage.output_tokens, 2)

    async def test_rejects_openai_choices_shape_instead_of_accepting_empty_text(self):
        transport = RecordingTransport([{
            "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
        }])
        provider = await self._provider(transport)
        with self.assertRaisesRegex(OpenAICompatibleProviderError, "Anthropic response content"):
            await provider.complete(ModelRequest(
                "turn-invalid", (Message("user", MessageRole.USER, (TextBlock("hello"),)),),
            ))

    async def test_evidence_envelope_round_trips_through_tool_input(self):
        transport = RecordingTransport([{
            "id": "msg-evidence", "stop_reason": "tool_use", "usage": {},
            "content": [{
                "type": "tool_use", "id": "toolu-evidence", "name": "core__read_file",
                "input": {"evidence_question": {"question_id": "Q1", "question": "Where?", "expected_scope": "README.md"}, "tool_arguments": {"path": "README.md"}},
            }],
        }])
        provider = await self._provider(transport)
        response = await provider.complete(ModelRequest(
            "turn-evidence", (Message("user", MessageRole.USER, (TextBlock("read"),)),),
            tools=(self._tool(),), require_evidence_questions=True,
        ))
        call = response.message.content[0].call
        self.assertEqual(call.arguments, {"path": "README.md"})
        self.assertEqual(
            call.evidence_question,
            EvidenceQuestion("Q1", "Where?", expected_scope="README.md"),
        )
