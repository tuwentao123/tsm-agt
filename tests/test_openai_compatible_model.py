from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from typing import Any

from tsm_agt.adapters.openai_compatible import (
    OpenAICompatibleModelProvider,
    OpenAICompatibleProviderError,
)
from tsm_agt.ports import (
    AdapterContext,
    EvidenceQuestion,
    FinishReason,
    Message,
    MessageRole,
    ModelRequest,
    ModelStreamCompleted,
    ModelTextDelta,
    RecoverableToolProtocolError,
    TextBlock,
    ToolCall,
    ToolCallBlock,
    ToolEffect,
    ToolIdempotency,
    ToolProtocol,
    ToolResult,
    ToolResultAuthority,
    ToolResultBlock,
    ToolRisk,
    ToolSpec,
)


class RecordingTransport:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    async def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        self.requests.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.responses.pop(0)


class AttemptJsonTransport(RecordingTransport):
    def __init__(self, attempts: list[Mapping[str, Any] | Exception]) -> None:
        super().__init__([])
        self.attempts = attempts

    async def post_json(
        self, url, headers, payload, timeout_seconds,
    ) -> Mapping[str, Any]:
        self.requests.append({
            "url": url, "headers": dict(headers),
            "payload": dict(payload), "timeout_seconds": timeout_seconds,
        })
        attempt = self.attempts.pop(0)
        if isinstance(attempt, Exception):
            raise attempt
        return attempt


class StreamingTransport(RecordingTransport):
    def __init__(self, events: list[str]) -> None:
        super().__init__([])
        self.events = events

    async def stream_sse(
        self, url, headers, payload, timeout_seconds,
    ):
        self.requests.append({
            "url": url, "headers": dict(headers),
            "payload": dict(payload), "timeout_seconds": timeout_seconds,
        })
        for event in self.events:
            yield event


class AttemptStreamingTransport(RecordingTransport):
    def __init__(self, attempts: list[list[str] | Exception]) -> None:
        super().__init__([])
        self.attempts = attempts

    async def stream_sse(self, url, headers, payload, timeout_seconds):
        self.requests.append({
            "url": url, "headers": dict(headers),
            "payload": dict(payload), "timeout_seconds": timeout_seconds,
        })
        attempt = self.attempts.pop(0)
        if isinstance(attempt, Exception):
            raise attempt
        for event in attempt:
            yield event


class OpenAICompatibleModelProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_non_streaming_classifies_retryable_transport_failure_once(self):
        transport = AttemptJsonTransport([
            OpenAICompatibleProviderError(
                "timed out", retryable=True, reason_code="read_timeout"
            ),
            {
                "id": "chat-retried-json",
                "choices": [{
                    "message": {"role": "assistant", "content": "recovered"},
                    "finish_reason": "stop",
                }],
                "usage": {},
            },
        ])
        provider = OpenAICompatibleModelProvider(
            "https://models.example.test/v1", "test-model", "secret",
            max_retries=2, retry_backoff_seconds=0, streaming=False,
            transport=transport,
        )
        await provider.start(AdapterContext(
            config={}, emit_event=lambda _type, _payload: None
        ))
        with self.assertRaises(OpenAICompatibleProviderError) as caught:
            await provider.complete(ModelRequest(
                "turn-json-retry",
                (Message("user", MessageRole.USER, (TextBlock("hello"),)),),
            ))
        self.assertEqual(caught.exception.reason_code, "read_timeout")
        self.assertEqual(len(transport.requests), 1)

    async def test_non_streaming_does_not_retry_permanent_provider_failure(self):
        transport = AttemptJsonTransport([
            OpenAICompatibleProviderError(
                "unauthorized", retryable=False, reason_code="http_401"
            ),
        ])
        provider = OpenAICompatibleModelProvider(
            "https://models.example.test/v1", "test-model", "secret",
            max_retries=2, retry_backoff_seconds=0, streaming=False,
            transport=transport,
        )
        await provider.start(AdapterContext(
            config={}, emit_event=lambda _type, _payload: None
        ))
        with self.assertRaises(OpenAICompatibleProviderError):
            await provider.complete(ModelRequest(
                "turn-json-no-retry",
                (Message("user", MessageRole.USER, (TextBlock("hello"),)),),
            ))
        self.assertEqual(len(transport.requests), 1)

    async def test_non_streaming_classifies_empty_message_for_resample(self):
        transport = AttemptJsonTransport([
            {
                "id": "chat-empty",
                "choices": [{
                    "message": {"role": "assistant", "content": None},
                    "finish_reason": "stop",
                }],
                "usage": {},
            },
            {
                "id": "chat-recovered",
                "choices": [{
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }],
                "usage": {},
            },
        ])
        provider = OpenAICompatibleModelProvider(
            "https://models.example.test/v1", "test-model", "secret",
            max_retries=1, retry_backoff_seconds=0, streaming=False,
            transport=transport,
        )
        await provider.start(AdapterContext(
            config={}, emit_event=lambda _type, _payload: None
        ))
        with self.assertRaises(OpenAICompatibleProviderError) as caught:
            await provider.complete(ModelRequest(
                "turn-empty-recovery",
                (Message("user", MessageRole.USER, (TextBlock("finish"),)),),
            ))
        self.assertEqual(caught.exception.reason_code, "empty_message")
        self.assertEqual(caught.exception.retry_safety.value, "SAFE_RESAMPLE")
        self.assertEqual(len(transport.requests), 1)

    async def test_non_streaming_provider_does_not_own_attempt_limit(self):
        empty = {
            "id": "chat-empty",
            "choices": [{
                "message": {"role": "assistant", "content": None},
                "finish_reason": "stop",
            }],
            "usage": {},
        }
        transport = AttemptJsonTransport([empty, empty])
        provider = OpenAICompatibleModelProvider(
            "https://models.example.test/v1", "test-model", "secret",
            max_retries=1, retry_backoff_seconds=0, streaming=False,
            transport=transport,
        )
        await provider.start(AdapterContext(
            config={}, emit_event=lambda _type, _payload: None
        ))
        with self.assertRaises(OpenAICompatibleProviderError) as caught:
            await provider.complete(ModelRequest(
                "turn-empty-failure",
                (Message("user", MessageRole.USER, (TextBlock("finish"),)),),
            ))
        self.assertEqual(caught.exception.reason_code, "empty_message")
        self.assertEqual(len(transport.requests), 1)

    async def _provider(self, responses: list[Mapping[str, Any]]):
        transport = RecordingTransport(responses)
        provider = OpenAICompatibleModelProvider(
            "https://models.example.test/v1/",
            "test-model",
            "test-secret",
            timeout_seconds=12.0,
            transport=transport,
        )
        await provider.start(
            AdapterContext(config={}, emit_event=lambda _type, _payload: None)
        )
        return provider, transport

    async def _streaming_provider(self, events: list[str]):
        transport = StreamingTransport(events)
        provider = OpenAICompatibleModelProvider(
            "https://models.example.test/v1/", "test-model",
            "test-secret", transport=transport,
        )
        await provider.start(
            AdapterContext(config={}, emit_event=lambda _type, _payload: None)
        )
        return provider, transport

    async def test_stream_aggregates_text_usage_and_done(self) -> None:
        provider, transport = await self._streaming_provider([
            '{"id":"chat-1","choices":[{"delta":{"content":"hello "},"finish_reason":null}]}',
            '{"id":"chat-1","choices":[{"delta":{"content":"world"},"finish_reason":"stop"}]}',
            '{"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}',
            "[DONE]",
        ])
        request = ModelRequest("turn-stream", (Message(
            "user-1", MessageRole.USER, (TextBlock("say hello"),)
        ),))
        events = [event async for event in provider.stream_complete(request)]
        self.assertEqual([event.text for event in events[:-1]], ["hello ", "world"])
        response = events[-1].response
        self.assertEqual(response.message.text, "hello world")
        self.assertEqual(response.usage.input_tokens, 7)
        self.assertEqual(response.usage.output_tokens, 2)
        diagnostics = response.diagnostics
        self.assertEqual(diagnostics["transport"]["kind"], "sse")
        self.assertEqual(diagnostics["transport"]["chunk_count"], 3)
        self.assertTrue(diagnostics["transport"]["saw_finish_reason"])
        self.assertTrue(diagnostics["transport"]["saw_done"])
        self.assertEqual(
            diagnostics["transport"]["text"]["sha256"],
            diagnostics["adapter"]["text"]["sha256"],
        )
        self.assertTrue(transport.requests[0]["payload"]["stream"])

    async def test_non_streaming_records_json_fingerprint_without_body(self) -> None:
        answer = "private provider answer must not be copied into diagnostics"
        provider, _ = await self._provider([{
            "id": "chat-json-diagnostics",
            "choices": [{
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }])

        response = await provider.complete(ModelRequest(
            "turn-json-diagnostics",
            (Message("user-1", MessageRole.USER, (TextBlock("hello"),)),),
        ))

        transport = response.diagnostics["transport"]
        self.assertEqual(transport["kind"], "json")
        self.assertEqual(transport["chunk_count"], 1)
        self.assertTrue(transport["saw_finish_reason"])
        self.assertIsNone(transport["saw_done"])
        self.assertEqual(
            transport["text"]["sha256"],
            response.diagnostics["adapter"]["text"]["sha256"],
        )
        serialized = json.dumps(response.diagnostics, ensure_ascii=False)
        self.assertNotIn(answer, serialized)
        self.assertNotIn("body", serialized.lower())
        self.assertNotIn("tail", serialized.lower())

    async def test_output_token_parameter_is_explicitly_replaceable(self) -> None:
        transport = RecordingTransport([{
            "id": "chat-token-parameter",
            "choices": [{
                "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }])
        provider = OpenAICompatibleModelProvider(
            "https://models.example.test/v1", "gpt-5-like", "secret",
            output_token_parameter="max_completion_tokens", transport=transport,
        )
        await provider.start(AdapterContext(
            config={}, emit_event=lambda _type, _payload: None
        ))
        await provider.complete(ModelRequest(
            "turn-token-parameter",
            (Message("user", MessageRole.USER, (TextBlock("hello"),)),),
            max_output_tokens=17,
        ))
        payload = transport.requests[0]["payload"]
        self.assertEqual(payload["max_completion_tokens"], 17)
        self.assertNotIn("max_tokens", payload)

    def test_unknown_output_token_parameter_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "output_token_parameter"):
            OpenAICompatibleModelProvider(
                "https://models.example.test/v1", "model", "secret",
                output_token_parameter="token_budget",
            )

    async def test_strict_tool_schema_can_be_disabled_for_gateway_compatibility(
        self,
    ) -> None:
        transport = StreamingTransport([
            '{"id":"chat-tool","choices":[{"delta":{'
            '"tool_calls":[{"index":0,"id":"call-1",'
            '"function":{"name":"core__read_file",'
            '"arguments":"{\\"path\\":\\"README.md\\"}"}}]},'
            '"finish_reason":"tool_calls"}]}',
            "[DONE]",
        ])
        provider = OpenAICompatibleModelProvider(
            "https://models.example.test/v1", "compatible-model", "secret",
            strict_tool_schema=False, transport=transport,
        )
        await provider.start(AdapterContext(
            config={}, emit_event=lambda _type, _payload: None
        ))
        request = ModelRequest(
            "turn-loose-schema",
            (Message("user", MessageRole.USER, (TextBlock("read"),)),),
            tools=(self._tool_spec(),),
        )
        _ = [event async for event in provider.stream_complete(request)]
        function = transport.requests[0]["payload"]["tools"][0]["function"]
        self.assertNotIn("strict", function)
        self.assertFalse(provider.capabilities.strict_json_schema)
        self.assertNotIn("strict-json-schema", provider.descriptor.capabilities)

    async def test_stream_classifies_transient_failure_without_retrying(self):
        transport = AttemptStreamingTransport([
            OpenAICompatibleProviderError(
                "timed out", retryable=True, reason_code="stream_failed"
            ),
            [
                '{"id":"chat-retry","choices":[{"delta":'
                '{"content":"recovered"},"finish_reason":"stop"}]}',
            ],
        ])
        provider = OpenAICompatibleModelProvider(
            "https://models.example.test/v1", "test-model", "test-secret",
            max_retries=2, retry_backoff_seconds=0, transport=transport,
        )
        await provider.start(AdapterContext(
            config={}, emit_event=lambda _type, _payload: None
        ))
        request = ModelRequest(
            "turn-retry",
            (Message("user-1", MessageRole.USER, (TextBlock("hello"),)),),
        )
        with self.assertRaises(OpenAICompatibleProviderError) as caught:
            _ = [event async for event in provider.stream_complete(request)]
        self.assertEqual(caught.exception.reason_code, "stream_failed")
        self.assertEqual(len(transport.requests), 1)

    async def test_stream_does_not_retry_after_user_visible_text(self):
        transport = AttemptStreamingTransport([[
            '{"choices":[{"delta":{"content":"visible"},'
            '"finish_reason":null}]}',
        ]])
        provider = OpenAICompatibleModelProvider(
            "https://models.example.test/v1", "test-model", "test-secret",
            max_retries=2, retry_backoff_seconds=0, transport=transport,
        )
        await provider.start(AdapterContext(
            config={}, emit_event=lambda _type, _payload: None
        ))
        request = ModelRequest(
            "turn-partial",
            (Message("user-1", MessageRole.USER, (TextBlock("hello"),)),),
        )
        with self.assertRaisesRegex(
            OpenAICompatibleProviderError, "without finish_reason"
        ):
            _ = [event async for event in provider.stream_complete(request)]
        self.assertEqual(len(transport.requests), 1)

    async def test_streaming_can_be_disabled_for_gateway_compatibility(self):
        transport = RecordingTransport([{
            "id": "chat-non-streaming",
            "choices": [{
                "message": {"role": "assistant", "content": "complete"},
                "finish_reason": "stop",
            }],
            "usage": {},
        }])
        provider = OpenAICompatibleModelProvider(
            "https://models.example.test/v1", "test-model", "test-secret",
            streaming=False, transport=transport,
        )
        await provider.start(AdapterContext(
            config={}, emit_event=lambda _type, _payload: None
        ))
        events = [event async for event in provider.stream_complete(ModelRequest(
            "turn-non-streaming",
            (Message("user", MessageRole.USER, (TextBlock("hello"),)),),
        ))]
        self.assertEqual(events[-1].response.message.text, "complete")
        self.assertNotIn("stream", transport.requests[0]["payload"])
        self.assertFalse(provider.capabilities.stream_cancel)
        self.assertNotIn("streaming", provider.descriptor.capabilities)

    async def test_wrap_up_request_keeps_tool_schema_but_disables_tool_calls(self) -> None:
        provider, transport = await self._streaming_provider([
            '{"id":"chat-final","choices":[{"delta":{"content":"final"},"finish_reason":"stop"}]}',
            "[DONE]",
        ])
        request = ModelRequest(
            "turn-final",
            (Message("user-1", MessageRole.USER, (TextBlock("finish"),)),),
            tools=(self._tool_spec(),), allow_tool_calls=False,
        )
        events = [event async for event in provider.stream_complete(request)]
        self.assertEqual(events[-1].response.message.text, "final")
        self.assertEqual(transport.requests[0]["payload"]["tool_choice"], "none")

    async def test_stream_aggregates_fragmented_tool_call(self) -> None:
        provider, _ = await self._streaming_provider([
            '{"id":"chat-tool","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"core__read_","arguments":"{\\"path\\":"}}]},"finish_reason":null}]}',
            '{"id":"chat-tool","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"file","arguments":"\\"README.md\\"}"}}]},"finish_reason":"tool_calls"}]}',
            "[DONE]",
        ])
        request = ModelRequest(
            "turn-tool",
            (Message("user-1", MessageRole.USER, (TextBlock("read"),)),),
            tools=(self._tool_spec(),),
        )
        events = [event async for event in provider.stream_complete(request)]
        self.assertEqual(len(events), 1)
        call = events[0].response.message.content[0].call
        self.assertEqual(call.name, "core.read_file")
        self.assertEqual(call.arguments, {"path": "README.md"})

    async def test_stream_accepts_complete_finish_without_done_and_classifies_incomplete_data(self) -> None:
        request = ModelRequest("turn-bad", (Message(
            "user-1", MessageRole.USER, (TextBlock("hello"),)
        ),))
        provider, _ = await self._streaming_provider(["not-json", "[DONE]"])
        with self.assertRaisesRegex(OpenAICompatibleProviderError, "invalid SSE JSON"):
            _ = [event async for event in provider.stream_complete(request)]

        provider, _ = await self._streaming_provider([
            '{"choices":[{"delta":{"content":"partial"},"finish_reason":"stop"}]}'
        ])
        events = [event async for event in provider.stream_complete(request)]
        self.assertEqual(events[-1].response.message.text, "partial")

        provider, _ = await self._streaming_provider([
            '{"choices":[{"delta":{},"finish_reason":null}]}'
        ])
        provider._transport.responses.append({
            "id": "chat-fallback",
            "choices": [{
                "message": {"role": "assistant", "content": "fallback"},
                "finish_reason": "stop",
            }],
            "usage": {},
        })
        with self.assertRaises(OpenAICompatibleProviderError) as caught:
            _ = [event async for event in provider.stream_complete(request)]
        self.assertEqual(caught.exception.reason_code, "incomplete_stream")
        self.assertEqual(
            caught.exception.retry_safety.value, "SAFE_FALLBACK_TRANSPORT"
        )
        self.assertEqual(len(provider._transport.requests), 1)
        self.assertTrue(provider._transport.requests[0]["payload"]["stream"])

    @staticmethod
    def _tool_spec() -> ToolSpec:
        return ToolSpec(
            name="core.read_file",
            description="Read one workspace text file.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
        )

    async def test_normalizes_text_and_usage(self) -> None:
        provider, transport = await self._provider(
            [
                {
                    "id": "chatcmpl-1",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                }
            ]
        )

        result = await provider.complete(
            ModelRequest(
                "turn-1",
                (Message("user-1", MessageRole.USER, (TextBlock("hi"),)),),
                512,
            )
        )

        self.assertEqual(result.message.text, "hello")
        self.assertEqual(result.message.message_id, "chatcmpl-1")
        self.assertEqual(result.finish_reason, FinishReason.STOP)
        self.assertEqual(result.usage.input_tokens, 4)
        request = transport.requests[0]
        self.assertEqual(request["url"], "https://models.example.test/v1/chat/completions")
        self.assertEqual(request["payload"]["model"], "test-model")
        self.assertEqual(request["payload"]["messages"][0], {"role": "user", "content": "hi"})
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-secret")

    async def test_normalizes_tool_call_and_advertises_strict_schema(self) -> None:
        provider, transport = await self._provider(
            [
                {
                    "id": "chatcmpl-tool",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I will inspect it.",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "core__read_file",
                                            "arguments": '{"path":"README.md"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {},
                }
            ]
        )

        result = await provider.complete(
            ModelRequest(
                "turn-1",
                (Message("user-1", MessageRole.USER, (TextBlock("inspect"),)),),
                tools=(self._tool_spec(),),
            )
        )

        self.assertEqual(result.finish_reason, FinishReason.TOOL_CALL)
        self.assertEqual(result.message.text, "I will inspect it.")
        call = result.message.content[1].call
        self.assertEqual(call, ToolCall("call-1", "core.read_file", {"path": "README.md"}))
        provider_tool = transport.requests[0]["payload"]["tools"][0]["function"]
        self.assertTrue(provider_tool["strict"])
        self.assertEqual(provider_tool["name"], "core__read_file")
        self.assertEqual(provider_tool["parameters"]["type"], "object")

    async def test_agent_mode_wraps_tool_schema_and_extracts_evidence_question(self) -> None:
        provider, transport = await self._provider([{
            "id": "chatcmpl-evidence",
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "call-evidence",
                        "type": "function",
                        "function": {
                            "name": "core__read_file",
                            "arguments": (
                                '{"evidence_question":{'
                                '"question_id":"E1",'
                                '"question":"Where is the entry point?"},'
                                '"tool_arguments":{"path":"README.md"}}'
                            ),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        }])

        response = await provider.complete(ModelRequest(
            "turn-evidence",
            (Message("user-1", MessageRole.USER, (TextBlock("inspect"),)),),
            tools=(self._tool_spec(),), require_evidence_questions=True,
        ))

        call = response.message.content[0].call
        self.assertEqual(call.arguments, {"path": "README.md"})
        self.assertEqual(
            call.evidence_question,
            EvidenceQuestion("E1", "Where is the entry point?"),
        )
        schema = transport.requests[0]["payload"]["tools"][0]["function"][
            "parameters"
        ]
        self.assertEqual(
            schema["required"], ["evidence_question", "tool_arguments"]
        )
        self.assertEqual(
            schema["properties"]["tool_arguments"],
            dict(self._tool_spec().parameters),
        )
        self.assertIn(
            "scope_expansion_reason",
            schema["properties"]["evidence_question"]["properties"],
        )
        self.assertIn(
            "expected_scope",
            schema["properties"]["evidence_question"]["properties"],
        )
        self.assertIn(
            "expected_scope",
            schema["properties"]["evidence_question"]["required"],
        )
        self.assertIn(
            '"tool_arguments":{...}',
            transport.requests[0]["payload"]["tools"][0]["function"][
                "description"
            ],
        )

    async def test_mixed_tools_use_per_tool_evidence_protocol(self) -> None:
        provider, transport = await self._provider([{
            "id": "chatcmpl-mixed-tools",
            "choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }],
            "usage": {},
        }])
        interaction = ToolSpec(
            name="core.request_input",
            description="Ask the user one material question.",
            parameters={
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            effect=ToolEffect.INTERACT,
            result_authority=ToolResultAuthority.USER_INTENT,
            protocol=ToolProtocol.WAIT_USER,
        )

        await provider.complete(ModelRequest(
            "turn-mixed-tools", (),
            tools=(self._tool_spec(), interaction),
            require_evidence_questions=True,
        ))

        functions = {
            item["function"]["name"]: item["function"]
            for item in transport.requests[0]["payload"]["tools"]
        }
        read_schema = functions["core__read_file"]["parameters"]
        interaction_schema = functions["core__request_input"]["parameters"]
        self.assertEqual(
            read_schema["required"], ["evidence_question", "tool_arguments"]
        )
        self.assertEqual(interaction_schema, dict(interaction.parameters))
        self.assertNotIn("evidence question", functions[
            "core__request_input"
        ]["description"].casefold())

    async def test_missing_evidence_envelope_gets_auditable_default(self) -> None:
        provider, _ = await self._provider([{
            "id": "chatcmpl-missing-envelope",
            "choices": [{
                "message": {"tool_calls": [{
                    "id": "call-missing-envelope",
                    "type": "function",
                    "function": {
                        "name": "core__read_file",
                        "arguments": '{"path":"README.md"}',
                    },
                }]},
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        }])

        response = await provider.complete(ModelRequest(
            "turn-missing-envelope",
            (Message("user-1", MessageRole.USER, (TextBlock("inspect"),)),),
            tools=(self._tool_spec(),), require_evidence_questions=True,
        ))
        call = response.message.content[0].call
        self.assertEqual(call.arguments, {"path": "README.md"})
        assert call.evidence_question is not None
        self.assertTrue(call.evidence_question.question_id.startswith("Q-auto-"))
        self.assertIn("README.md", call.evidence_question.question)
        self.assertEqual(call.evidence_question.expected_scope, "README.md")

    async def test_partial_evidence_envelope_is_marked_recoverable(self) -> None:
        provider, _ = await self._provider([{
            "id": "chatcmpl-partial-envelope",
            "choices": [{
                "message": {"tool_calls": [{
                    "id": "call-partial-envelope",
                    "type": "function",
                    "function": {
                        "name": "core__read_file",
                        "arguments": (
                            '{"tool_arguments":{"path":"README.md"}}'
                        ),
                    },
                }]},
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        }])
        with self.assertRaises(RecoverableToolProtocolError) as raised:
            await provider.complete(ModelRequest(
                "turn-partial-envelope", (), tools=(self._tool_spec(),),
                require_evidence_questions=True,
            ))
        self.assertEqual(
            raised.exception.reason_code, "missing_evidence_question_envelope"
        )

    async def test_serializes_assistant_tool_call_and_tool_result_for_continuation(self) -> None:
        provider, transport = await self._provider(
            [
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "done"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {},
                }
            ]
        )
        call = ToolCall("call-1", "core.read_file", {"path": "README.md"})
        result = ToolResult("call-1", True, data={"content": "hello"})
        messages = (
            Message("user-1", MessageRole.USER, (TextBlock("inspect"),)),
            Message("assistant-1", MessageRole.ASSISTANT, (ToolCallBlock(call),)),
            Message("tool-1", MessageRole.TOOL, (ToolResultBlock(result),)),
        )

        await provider.complete(ModelRequest("turn-1", messages, tools=(self._tool_spec(),)))

        provider_messages = transport.requests[0]["payload"]["messages"]
        self.assertEqual(provider_messages[1]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(
            provider_messages[1]["tool_calls"][0]["function"]["name"],
            "core__read_file",
        )
        self.assertEqual(provider_messages[2]["role"], "tool")
        self.assertEqual(provider_messages[2]["tool_call_id"], "call-1")
        self.assertIn('\"ok\":true', provider_messages[2]["content"])

    async def test_recovers_exact_text_tool_blocks_from_compatible_gateway(self) -> None:
        body = (
            '<tool_use id="call-text-1" name="core__read_file">\n'
            '{"evidence_question":{"question_id":"E-text",'
            '"question":"What does the file establish?"},'
            '"tool_arguments":{"path":"README.md"}}\n'
            '</tool_use>'
        )
        provider, _ = await self._provider([{
            "id": "chatcmpl-text-tool",
            "choices": [{
                "message": {"role": "assistant", "content": body},
                "finish_reason": "stop",
            }],
            "usage": {},
        }])

        response = await provider.complete(ModelRequest(
            "turn-text-tool",
            (Message("user-1", MessageRole.USER, (TextBlock("inspect"),)),),
            tools=(self._tool_spec(),), require_evidence_questions=True,
        ))

        self.assertEqual(response.finish_reason, FinishReason.TOOL_CALL)
        self.assertEqual(response.message.text, "")
        call = response.message.content[0].call
        self.assertEqual(call.name, "core.read_file")
        self.assertEqual(call.arguments, {"path": "README.md"})
        assert call.evidence_question is not None
        self.assertEqual(call.evidence_question.question_id, "E-text")

    async def test_does_not_execute_text_tool_markup_mixed_with_prose(self) -> None:
        body = (
            'Example only:\n<tool_use id="call-text-1" '
            'name="core__read_file">{"path":"README.md"}</tool_use>'
        )
        provider, _ = await self._provider([{
            "id": "chatcmpl-text-example",
            "choices": [{
                "message": {"role": "assistant", "content": body},
                "finish_reason": "stop",
            }],
            "usage": {},
        }])

        response = await provider.complete(ModelRequest(
            "turn-text-example",
            (Message("user-1", MessageRole.USER, (TextBlock("show example"),)),),
            tools=(self._tool_spec(),),
        ))

        self.assertEqual(response.finish_reason, FinishReason.STOP)
        self.assertEqual(response.message.text, body)
        self.assertFalse(any(
            isinstance(block, ToolCallBlock) for block in response.message.content
        ))

    async def test_rejects_text_tools_when_calls_are_disabled(self) -> None:
        body = (
            '<tool_use id="call-text-1" name="core__read_file">'
            '{"path":"README.md"}</tool_use>'
        )
        provider, _ = await self._provider([{
            "id": "chatcmpl-text-disabled",
            "choices": [{
                "message": {"role": "assistant", "content": body},
                "finish_reason": "stop",
            }],
            "usage": {},
        }])

        with self.assertRaises(RecoverableToolProtocolError) as raised:
            await provider.complete(ModelRequest(
                "turn-text-disabled",
                (Message("user-1", MessageRole.USER, (TextBlock("finish"),)),),
                tools=(self._tool_spec(),), allow_tool_calls=False,
            ))
        self.assertEqual(
            raised.exception.reason_code,
            "tool_call_emitted_while_disabled",
        )

    async def test_stream_does_not_leak_recovered_text_tool_markup(self) -> None:
        body = (
            '<tool_use id="call-text-stream" name="core__read_file">'
            '{"evidence_question":{"question_id":"Q-stream",'
            '"question":"What does it contain?"},'
            '"tool_arguments":{"path":"README.md"}}'
            '</tool_use>'
        )
        provider, _ = await self._streaming_provider([
            '{"id":"chat-text-tool","choices":[{"delta":'
            '{"content":"<tool_"},"finish_reason":null}]}',
            json.dumps({
                "id": "chat-text-tool",
                "choices": [{
                    "delta": {"content": body[len("<tool_"):]},
                    "finish_reason": "stop",
                }],
            }),
            "[DONE]",
        ])
        events = [event async for event in provider.stream_complete(ModelRequest(
            "turn-text-stream",
            (Message("user-1", MessageRole.USER, (TextBlock("inspect"),)),),
            tools=(self._tool_spec(),), require_evidence_questions=True,
        ))]
        self.assertFalse(any(isinstance(event, ModelTextDelta) for event in events))
        completed = next(
            event for event in events if isinstance(event, ModelStreamCompleted)
        )
        self.assertEqual(completed.response.finish_reason, FinishReason.TOOL_CALL)

    async def test_stream_rejects_reordered_tool_markup_without_leaking_it(self) -> None:
        body = (
            '<tool_use name="core__read_file" id="call-reordered">'
            '{"path":"README.md"}</tool_use>'
        )
        provider, _ = await self._streaming_provider([
            json.dumps({
                "id": "chat-reordered-tool",
                "choices": [{
                    "delta": {"content": body},
                    "finish_reason": "stop",
                }],
            }),
            "[DONE]",
        ])

        emitted = []
        with self.assertRaises(RecoverableToolProtocolError) as raised:
            async for event in provider.stream_complete(ModelRequest(
                "turn-reordered-tool",
                (Message("user-1", MessageRole.USER, (TextBlock("finish"),)),),
                tools=(self._tool_spec(),), allow_tool_calls=False,
            )):
                emitted.append(event)
        self.assertEqual(
            raised.exception.reason_code, "tool_call_emitted_while_disabled"
        )
        self.assertFalse(any(isinstance(event, ModelTextDelta) for event in emitted))

    async def test_invalid_exact_text_tool_protocol_requests_correction(self) -> None:
        body = (
            '<tool_use name="unknown_tool" id="call-unknown">'
            '{"path":"README.md"}</tool_use>'
        )
        provider, _ = await self._provider([{
            "id": "chatcmpl-invalid-text-tool",
            "choices": [{
                "message": {"role": "assistant", "content": body},
                "finish_reason": "stop",
            }],
            "usage": {},
        }])

        with self.assertRaises(RecoverableToolProtocolError) as raised:
            await provider.complete(ModelRequest(
                "turn-invalid-text-tool",
                (Message("user-1", MessageRole.USER, (TextBlock("inspect"),)),),
                tools=(self._tool_spec(),),
            ))
        self.assertEqual(raised.exception.reason_code, "invalid_text_tool_protocol")

    async def test_invalid_tool_argument_json_fails_closed(self) -> None:
        provider, _ = await self._provider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "core.read_file",
                                            "arguments": "{broken",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ]
        )

        with self.assertRaisesRegex(OpenAICompatibleProviderError, "invalid JSON"):
            await provider.complete(
                ModelRequest("turn-1", (), tools=(self._tool_spec(),))
            )

    async def test_unknown_finish_reason_fails_closed(self) -> None:
        provider, _ = await self._provider(
            [
                {
                    "choices": [
                        {
                            "message": {"content": "maybe"},
                            "finish_reason": "unknown_reason",
                        }
                    ]
                }
            ]
        )
        with self.assertRaisesRegex(OpenAICompatibleProviderError, "finish reason"):
            await provider.complete(ModelRequest("turn-1", ()))

    async def test_outcome_ref_is_protocol_metadata_not_tool_argument(self) -> None:
        provider, transport = await self._provider([{
            "id": "chat-outcome",
            "choices": [{
                "message": {"role": "assistant", "tool_calls": [{
                    "id": "call-outcome", "type": "function",
                    "function": {
                        "name": "core__read_file",
                        "arguments": json.dumps({
                            "path": "README.md",
                            "outcome_ref": "model-proposed-outcome",
                        }),
                    },
                }]},
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        }])
        response = await provider.complete(ModelRequest(
            "turn-outcome",
            (Message("user", MessageRole.USER, (TextBlock("inspect"),)),),
            tools=(self._tool_spec(),), outcome_refs=("inspect-source",),
        ))
        call = next(
            block.call for block in response.message.content
            if isinstance(block, ToolCallBlock)
        )
        # Provider parses metadata but deliberately does not enforce the
        # advertised set; Kernel owns semantic validation and observability.
        self.assertEqual(call.outcome_ref, "model-proposed-outcome")
        self.assertEqual(call.arguments, {"path": "README.md"})
        schema = transport.requests[0]["payload"]["tools"][0]["function"]["parameters"]
        self.assertEqual(
            schema["properties"]["outcome_ref"]["type"], "string"
        )
        self.assertNotIn("enum", schema["properties"]["outcome_ref"])


if __name__ == "__main__":
    unittest.main()
