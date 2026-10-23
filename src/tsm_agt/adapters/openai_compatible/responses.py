"""OpenAI Responses wire adapter with opt-in reasoning summaries."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace

from tsm_agt.ports import (Message, MessageRole, ModelReasoningDelta,
                           ModelStreamCompleted, ModelTextDelta, ToolCallBlock,
                           ModelFailureCategory, ModelRetrySafety)
from tsm_agt.ports.model import ReasoningBlock
from .model import OpenAICompatibleModelProvider, OpenAICompatibleProviderError


def _responses_error(payload, fallback):
    """Keep errors embedded in HTTP-200 SSE responses visible to recovery."""
    response = payload.get("response")
    error = response.get("error") if isinstance(response, Mapping) else None
    if not isinstance(error, Mapping):
        error = payload.get("error")
    if not isinstance(error, Mapping):
        error = payload
    code = str(error.get("code") or error.get("type") or "provider_error")
    message = str(error.get("message") or fallback)[:1000]
    options = {}
    if code in {"http_401", "http_403", "invalid_api_key", "authentication_error"}:
        options = {"category": ModelFailureCategory.AUTHENTICATION,
                   "retry_safety": ModelRetrySafety.NEVER}
    elif code in {"http_400", "http_404", "http_422", "unsupported_parameter",
                  "invalid_request_error", "model_not_found"}:
        options = {"category": ModelFailureCategory.CONFIGURATION,
                   "retry_safety": ModelRetrySafety.NEVER}
    elif code in {"server_error", "rate_limit_exceeded", "http_429"} or (
        code.startswith("http_5") and len(code) == 8 and code[5:].isdigit()
    ):
        options = {"category": ModelFailureCategory.TRANSIENT_PROVIDER,
                   "retry_safety": ModelRetrySafety.SAFE_SAME_REQUEST}
    return OpenAICompatibleProviderError(
        f"{fallback} ({code}): {message}", reason_code=code, **options,
    )


class OpenAIResponsesModelProvider(OpenAICompatibleModelProvider):
    """Responses is opt-in; existing Chat Completions behavior is untouched."""

    def __init__(self, *args, reasoning_effort="medium", **kwargs):
        super().__init__(*args, native_structured_conclusion=False, **kwargs)
        self._reasoning_effort = reasoning_effort or "medium"

    def _responses_payload(self, request):
        chat, names = self._build_payload(request)
        items = list(chat["messages"][:len(chat["messages"]) - len(request.messages)])
        history = chat["messages"][len(items):]
        for original, message in zip(request.messages, history):
            for block in original.content:
                if isinstance(block, ReasoningBlock) and block.provider == "openai_responses":
                    items.append(json.loads(block.data))
            role = message["role"]
            if role == "tool":
                items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message.get("content") or ""})
                continue
            content = message.get("content")
            if content:
                if isinstance(content, list):
                    converted = []
                    for part in content:
                        if part.get("type") == "text":
                            converted.append({"type": "output_text" if role == "assistant" else "input_text", "text": part["text"]})
                        elif part.get("type") == "image_url":
                            image = part["image_url"]
                            converted.append({"type": "input_image", "image_url": image["url"], "detail": image.get("detail", "auto")})
                    content = converted
                items.append({"role": role, "content": content})
            for call in message.get("tool_calls", []):
                function = call["function"]
                items.append({"type": "function_call", "call_id": call["id"], "name": function["name"], "arguments": function["arguments"]})
        payload = {"model": self._model, "input": items,
                   "reasoning": {"effort": self._reasoning_effort, "summary": "auto"},
                   "include": ["reasoning.encrypted_content"], "store": False}
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens
        if chat.get("tools"):
            payload["tools"] = [{"type": "function", **tool["function"]} for tool in chat["tools"]]
            payload["tool_choice"] = chat["tool_choice"]
        return payload, names

    def _normalize_responses(self, request, response, names):
        if response.get("status") not in ("completed", "incomplete"):
            raise _responses_error(response, "Responses request did not complete")
        output = response.get("output")
        if not isinstance(output, list):
            raise OpenAICompatibleProviderError("Responses output must be a list")
        text = []
        calls = []
        reasoning = []
        for item in output:
            if not isinstance(item, Mapping):
                raise OpenAICompatibleProviderError("invalid Responses output item")
            if item.get("type") == "reasoning":
                reasoning.append(ReasoningBlock("openai_responses", "reasoning", data=json.dumps(item, ensure_ascii=False, separators=(",", ":"))))
            elif item.get("type") == "message":
                for block in item.get("content", []):
                    if block.get("type") == "output_text":
                        text.append(block.get("text", ""))
            elif item.get("type") == "function_call":
                calls.append({"id": item.get("call_id"), "type": "function", "function": {"name": item.get("name"), "arguments": item.get("arguments")}})
        usage = response.get("usage") or {}
        chat_response = {"id": response.get("id"), "model": response.get("model"),
                         "choices": [{"message": {"role": "assistant", "content": "".join(text) or None, "tool_calls": calls},
                                      "finish_reason": "tool_calls" if calls else ("length" if response.get("status") == "incomplete" else "stop")}],
                         "usage": {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)}}
        result = self._normalize_response(request.turn_id, chat_response, names,
                    allow_text_tool_fallback=request.allow_tool_calls,
                    allowed_outcome_refs=frozenset(request.outcome_refs),
                    conclusion_protocol_mode=request.conclusion_protocol_mode)
        if reasoning:
            result = replace(result, message=replace(result.message, content=tuple(reasoning) + result.message.content))
        return result

    async def complete(self, request):
        payload, names = self._responses_payload(request)
        self._capture_request(request, payload)
        response = await self._transport.post_json(self._base_url + "/responses", {"Authorization": f"Bearer {self._api_key}"}, payload, request.timeout_seconds or self._timeout_seconds)
        return self._normalize_responses(request, response, names)

    async def stream_complete(self, request):
        if not self._streaming:
            yield ModelStreamCompleted(await self.complete(request))
            return
        payload, names = self._responses_payload(request)
        payload["stream"] = True
        self._capture_request(request, payload)
        final = None
        completed_reasoning = {}
        async for data in self._transport.stream_sse(self._base_url + "/responses", {"Authorization": f"Bearer {self._api_key}"}, payload, request.timeout_seconds or self._timeout_seconds, cancellation_scope=request.cancellation_scope):
            if data == "[DONE]":
                break
            event = json.loads(data)
            kind = event.get("type")
            if kind == "response.reasoning_summary_text.delta" and event.get("delta"):
                yield ModelReasoningDelta(event["delta"])
            elif kind == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, Mapping) and item.get("type") == "reasoning" and isinstance(item.get("id"), str):
                    completed_reasoning[item["id"]] = item
            elif kind in ("response.completed", "response.incomplete"):
                final = event.get("response")
            elif kind in ("response.failed", "error"):
                raise _responses_error(event, "Responses stream failed")
        if not isinstance(final, Mapping):
            raise OpenAICompatibleProviderError("Responses stream ended before completion")
        final = dict(final)
        if isinstance(final.get("output"), list):
            final["output"] = [completed_reasoning.get(item.get("id"), item)
                               if isinstance(item, Mapping) and item.get("type") == "reasoning" else item
                               for item in final["output"]]
        result = self._normalize_responses(request, final, names)
        if result.message.text:
            yield ModelTextDelta(result.message.text)
        yield ModelStreamCompleted(result)
