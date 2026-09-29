"""Native Anthropic Messages API provider.

This adapter intentionally shares only the stdlib HTTP/SSE transport with the
OpenAI-compatible adapter.  The request path, headers, content blocks, tool
wire format, finish reasons and stream event protocol are Anthropic-specific.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Any
from uuid import uuid4

from tsm_agt.adapters.http_json import (
    HttpJsonTransport,
    ModelProviderTransportError,
    UrllibHttpJsonTransport,
)
from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    ConclusionBlock,
    ConclusionProtocolMode,
    EvidenceQuestion,
    FinishReason,
    HealthState,
    HealthStatus,
    ImageBlock,
    Message,
    MessageRole,
    ModelFailureCategory,
    ModelRequest,
    ModelResponse,
    ModelRetrySafety,
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelTextDelta,
    ModelUsage,
    ProviderCapabilities,
    RecoverableToolProtocolError,
    TextBlock,
    ToolCall,
    ToolCallBlock,
    ToolResultBlock,
)

_ANTHROPIC_VERSION = "2023-06-01"
_CONTROLLED_CONCLUSION_REQUEST = (
    "Conclusion protocol: keep the user-visible answer before the protocol. "
    "If a structured conclusion is available, end the response with exactly one "
    "<tsm-conclusion-v1>{...}</tsm-conclusion-v1> block containing one valid "
    "AssistantConclusion v1 JSON object. Nothing may follow the closing tag. "
    "Never infer a conclusion from prose."
)


class AnthropicMessagesModelProvider:
    """Translate the provider-neutral model contract to ``POST /v1/messages``."""

    descriptor = AdapterDescriptor(
        adapter_id="builtin.anthropic-messages-model",
        adapter_version="0.1.0",
        port_name="ModelProviderPort",
        port_version="1.0",
        capabilities=frozenset({"text", "tools", "streaming", "stream-cancel", "vision"}),
    )
    capabilities = ProviderCapabilities(
        tools=True,
        parallel_tools=True,
        vision=True,
        stream_cancel=True,
        context_window=200_000,
        # Claude's native structured-output format is not the project Conclusion
        # protocol.  The first implementation intentionally keeps that protocol
        # disabled rather than claiming an incompatible schema is equivalent.
        structured_conclusion=False,
    )

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        anthropic_version: str = _ANTHROPIC_VERSION,
        timeout_seconds: float = 60.0,
        streaming: bool = True,
        transport: HttpJsonTransport | None = None,
    ) -> None:
        normalized_url = base_url.strip().rstrip("/")
        if not normalized_url.startswith(("https://", "http://")):
            raise ValueError("base_url must be an http(s) URL")
        if not model.strip():
            raise ValueError("model must not be empty")
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        if not anthropic_version.strip():
            raise ValueError("anthropic_version must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._base_url = normalized_url
        self._model = model.strip()
        self._api_key = api_key
        self._anthropic_version = anthropic_version.strip()
        self._timeout_seconds = timeout_seconds
        self._streaming = streaming
        self._transport = transport or UrllibHttpJsonTransport()
        self._started = False
        capabilities = {"text", "tools", "vision"}
        if streaming:
            capabilities.update({"streaming", "stream-cancel"})
        self.descriptor = AdapterDescriptor(
            adapter_id=type(self).descriptor.adapter_id,
            adapter_version=type(self).descriptor.adapter_version,
            port_name=type(self).descriptor.port_name,
            port_version=type(self).descriptor.port_version,
            capabilities=frozenset(capabilities),
        )
        self.capabilities = ProviderCapabilities(
            tools=True,
            parallel_tools=True,
            vision=True,
            stream_cancel=streaming,
            context_window=200_000,
            structured_conclusion=False,
        )

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            f"Anthropic Messages model {self._model} ready"
            if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._started:
            raise RuntimeError("adapter is not started")
        payload, provider_to_internal = self._build_payload(request)
        response = await self._transport.post_json(
            self._messages_url(), self._headers(), payload,
            request.timeout_seconds or self._timeout_seconds,
        )
        normalized = self._normalize_response(
            request.turn_id, response, provider_to_internal,
            require_evidence_questions=request.require_evidence_questions,
            evidence_required_tools=frozenset(
                tool.name for tool in request.tools
                if request.require_evidence_questions and tool.requires_evidence_question
            ),
            allowed_outcome_refs=frozenset(request.outcome_refs),
        )
        return self._with_diagnostics(
            normalized, transport_kind="json", raw_text=normalized.message.text,
            chunk_count=1, saw_stop=True,
        )

    async def stream_complete(
        self, request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        if not self._started:
            raise RuntimeError("adapter is not started")
        if not self._streaming:
            yield ModelStreamCompleted(await self.complete(request))
            return
        payload, provider_to_internal = self._build_payload(request)
        payload["stream"] = True
        text_parts: list[str] = []
        tool_parts: dict[int, dict[str, Any]] = {}
        message_id: str | None = None
        served_model: str | None = None
        finish_reason: str | None = None
        usage: Mapping[str, Any] = {}
        saw_chunk = False
        saw_message_stop = False
        chunk_count = 0

        async for data in self._transport.stream_sse(
            self._messages_url(), self._headers(), payload,
            request.timeout_seconds or self._timeout_seconds,
            cancellation_scope=request.cancellation_scope,
        ):
            try:
                event = json.loads(data)
            except json.JSONDecodeError as error:
                raise ModelProviderTransportError(
                    "provider returned invalid Anthropic SSE JSON",
                    category=ModelFailureCategory.INVALID_RESPONSE,
                    retry_safety=ModelRetrySafety.SAFE_RESAMPLE,
                ) from error
            if not isinstance(event, Mapping):
                raise ModelProviderTransportError("Anthropic SSE event must be an object")
            event_type = event.get("type")
            if not isinstance(event_type, str):
                raise ModelProviderTransportError("Anthropic SSE event has no type")
            saw_chunk = True
            chunk_count += 1
            if event_type == "message_start":
                message = event.get("message", {})
                if not isinstance(message, Mapping):
                    raise ModelProviderTransportError("message_start message must be an object")
                if isinstance(message.get("id"), str):
                    message_id = message["id"]
                if isinstance(message.get("model"), str):
                    served_model = message["model"]
                if isinstance(message.get("usage"), Mapping):
                    usage = message["usage"]
            elif event_type == "content_block_start":
                index = event.get("index")
                block = event.get("content_block", {})
                if not isinstance(index, int) or not isinstance(block, Mapping):
                    raise ModelProviderTransportError("invalid content_block_start")
                block_type = block.get("type")
                if block_type == "text":
                    tool_parts[index] = {"type": "text"}
                elif block_type == "tool_use":
                    tool_parts[index] = {
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": (
                            "" if not block.get("input")
                            else json.dumps(block.get("input"), separators=(",", ":"))
                        ),
                    }
                else:
                    tool_parts[index] = {"type": "ignored"}
            elif event_type == "content_block_delta":
                index = event.get("index")
                delta = event.get("delta", {})
                if not isinstance(index, int) or not isinstance(delta, Mapping):
                    raise ModelProviderTransportError("invalid content_block_delta")
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text = delta.get("text")
                    if not isinstance(text, str):
                        raise ModelProviderTransportError("text_delta text must be a string")
                    if text:
                        text_parts.append(text)
                        yield ModelTextDelta(text)
                elif delta_type == "input_json_delta":
                    aggregate = tool_parts.get(index)
                    partial_json = delta.get("partial_json")
                    if (
                        not isinstance(aggregate, dict)
                        or aggregate.get("type") != "tool_use"
                        or not isinstance(partial_json, str)
                    ):
                        raise ModelProviderTransportError("invalid tool input JSON delta")
                    aggregate["input"] = str(aggregate["input"]) + partial_json
            elif event_type == "message_delta":
                delta = event.get("delta", {})
                if not isinstance(delta, Mapping):
                    raise ModelProviderTransportError("message_delta delta must be an object")
                raw_reason = delta.get("stop_reason")
                if raw_reason is not None:
                    if not isinstance(raw_reason, str):
                        raise ModelProviderTransportError("stop_reason must be a string")
                    finish_reason = raw_reason
                if isinstance(event.get("usage"), Mapping):
                    usage = {**usage, **event["usage"]}
            elif event_type == "message_stop":
                saw_message_stop = True

        if not saw_chunk or not saw_message_stop or finish_reason is None:
            raise ModelProviderTransportError(
                "Anthropic SSE stream ended before a complete message",
                category=ModelFailureCategory.INVALID_RESPONSE,
                retry_safety=ModelRetrySafety.SAFE_RESAMPLE,
                reason_code="incomplete_anthropic_stream",
            )
        content: list[dict[str, Any]] = [{"type": "text", "text": "".join(text_parts)}]
        for _, value in sorted(tool_parts.items()):
            if value.get("type") != "tool_use":
                continue
            try:
                tool_input = json.loads(str(value["input"]))
            except json.JSONDecodeError as error:
                raise ModelProviderTransportError("Anthropic tool input is invalid JSON") from error
            content.append({
                "type": "tool_use", "id": value.get("id"),
                "name": value.get("name"), "input": tool_input,
            })
        normalized = self._normalize_response(
            request.turn_id,
            {
                "id": message_id,
                "model": served_model,
                "stop_reason": finish_reason,
                "content": content,
                "usage": usage,
            },
            provider_to_internal,
            require_evidence_questions=request.require_evidence_questions,
            evidence_required_tools=frozenset(
                tool.name for tool in request.tools
                if request.require_evidence_questions and tool.requires_evidence_question
            ),
            allowed_outcome_refs=frozenset(request.outcome_refs),
        )
        normalized = self._with_diagnostics(
            normalized, transport_kind="anthropic_sse", raw_text="".join(text_parts),
            chunk_count=chunk_count, saw_stop=saw_message_stop,
        )
        yield ModelStreamCompleted(normalized)

    def _messages_url(self) -> str:
        if self._base_url.endswith("/v1/messages"):
            return self._base_url
        if self._base_url.endswith("/v1"):
            return self._base_url + "/messages"
        return self._base_url + "/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
        }

    def _build_payload(self, request: ModelRequest) -> tuple[dict[str, Any], dict[str, str]]:
        provider_to_internal: dict[str, str] = {}
        internal_to_provider: dict[str, str] = {}
        for tool in request.tools:
            name = self._encode_tool_name(tool.name)
            if name in provider_to_internal:
                raise ModelProviderTransportError("tool names collide after provider encoding")
            provider_to_internal[name] = tool.name
            internal_to_provider[tool.name] = name
        history_names = dict(internal_to_provider)
        for name in {
            block.call.name
            for message in request.messages
            for block in message.content
            if isinstance(block, ToolCallBlock)
        }:
            history_names.setdefault(name, self._encode_tool_name(name))

        system: list[str] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role is MessageRole.SYSTEM:
                for block in message.content:
                    if not isinstance(block, TextBlock):
                        raise ModelProviderTransportError("Anthropic system messages support text blocks only")
                    system.append(block.text)
                continue
            converted = self._message_to_provider(
                message, history_names,
                frozenset(tool.name for tool in request.tools if request.require_evidence_questions and tool.requires_evidence_question),
                frozenset(request.outcome_refs),
            )
            if messages and messages[-1]["role"] == converted["role"]:
                messages[-1]["content"].extend(converted["content"])
            else:
                messages.append(converted)
        if request.conclusion_protocol_mode is ConclusionProtocolMode.REQUIRE_STRUCTURED:
            # `system` collects plain strings because the wire shape below is a
            # single joined string; appending a content block here would break
            # the join.
            system.append(_CONTROLLED_CONCLUSION_REQUEST)

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_output_tokens,
            "messages": messages,
        }
        if system:
            # aibrain's Anthropic Messages gateway documents `system` as a
            # string. Keep internal multiple system messages ordered, then join
            # them into the documented wire shape instead of sending OpenAI-like
            # content blocks.
            payload["system"] = "\n\n".join(system)
        if request.tools:
            tool_refs = dict(request.tool_outcome_refs)
            payload["tools"] = [
                self._tool_to_provider(
                    tool, internal_to_provider[tool.name],
                    request.require_evidence_questions and tool.requires_evidence_question,
                    tool_refs.get(tool.name, request.outcome_refs),
                )
                for tool in request.tools
            ]
            payload["tool_choice"] = {"type": "auto" if request.allow_tool_calls else "none"}
        return payload, provider_to_internal

    def _message_to_provider(
        self,
        message: Message,
        internal_to_provider: Mapping[str, str],
        evidence_required_tools: frozenset[str],
        outcome_refs: frozenset[str],
    ) -> dict[str, Any]:
        if message.role is MessageRole.TOOL:
            blocks = [block for block in message.content if isinstance(block, ToolResultBlock)]
            if len(blocks) != 1:
                raise ModelProviderTransportError("tool message must contain exactly one tool result block")
            result = blocks[0].result
            return {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": result.call_id,
                    "content": json.dumps(result.to_data(), ensure_ascii=False, separators=(",", ":")),
                    "is_error": not result.ok,
                }],
            }
        if message.role not in {MessageRole.USER, MessageRole.ASSISTANT}:
            raise ModelProviderTransportError(f"unsupported Anthropic message role: {message.role.value}")
        content: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                content.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageBlock):
                prefix, _, data = block.image_url.partition(";base64,")
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": prefix[5:], "data": data},
                })
            elif isinstance(block, ToolCallBlock):
                if message.role is not MessageRole.ASSISTANT:
                    raise ModelProviderTransportError("tool_use history requires assistant role")
                provider_name = internal_to_provider.get(block.call.name)
                if provider_name is None:
                    raise ModelProviderTransportError("assistant history references unknown tool")
                content.append({
                    "type": "tool_use", "id": block.call.call_id,
                    "name": provider_name,
                    "input": self._wire_tool_input(block.call, evidence_required_tools, outcome_refs),
                })
            elif isinstance(block, ConclusionBlock):
                content.append({
                    "type": "text",
                    "text": "<tsm-conclusion-v1>" + json.dumps(block.conclusion.to_data(), ensure_ascii=False, separators=(",", ":")) + "</tsm-conclusion-v1>",
                })
        if not content:
            content.append({"type": "text", "text": message.text})
        return {"role": message.role.value, "content": content}

    def _tool_to_provider(self, tool: Any, name: str, requires_evidence: bool, outcome_refs: tuple[str, ...]) -> dict[str, Any]:
        parameters = dict(tool.parameters)
        description = tool.description
        if requires_evidence:
            parameters = {
                "type": "object",
                "properties": {
                    "evidence_question": {
                        "type": "object",
                        "properties": {
                            "question_id": {"type": "string"},
                            "question": {"type": "string"},
                            "scope_expansion_reason": {"type": "string"},
                            "expected_scope": {"type": "string"},
                        },
                        "required": ["question_id", "question", "expected_scope"],
                        "additionalProperties": False,
                    },
                    "tool_arguments": parameters,
                },
                "required": ["evidence_question", "tool_arguments"],
                "additionalProperties": False,
            }
            description = (
                f"{description} Bind this call to one evidence question in "
                "evidence_question and put normal arguments in tool_arguments."
            )
        if outcome_refs:
            properties = dict(parameters.get("properties", {}))
            properties["outcome_ref"] = {"type": "string"}
            parameters = {**parameters, "properties": properties}
        return {"name": name, "description": description, "input_schema": parameters}

    def _normalize_response(
        self,
        turn_id: str,
        response: Mapping[str, Any],
        provider_to_internal: Mapping[str, str],
        *,
        require_evidence_questions: bool,
        evidence_required_tools: frozenset[str],
        allowed_outcome_refs: frozenset[str],
    ) -> ModelResponse:
        raw_content = response.get("content")
        if not isinstance(raw_content, list):
            raise ModelProviderTransportError("Anthropic response content must be a list")
        content: list[TextBlock | ToolCallBlock] = []
        for block in raw_content:
            if not isinstance(block, Mapping):
                raise ModelProviderTransportError("Anthropic content block must be an object")
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise ModelProviderTransportError("Anthropic text block must contain text")
                if text:
                    content.append(TextBlock(text))
            elif block_type == "tool_use":
                call_id, provider_name, raw_input = block.get("id"), block.get("name"), block.get("input")
                if not isinstance(call_id, str) or not isinstance(provider_name, str) or not isinstance(raw_input, Mapping):
                    raise ModelProviderTransportError("Anthropic tool_use block is invalid")
                internal_name = provider_to_internal.get(provider_name)
                if internal_name is None:
                    raise ModelProviderTransportError("provider requested an unadvertised tool: " + provider_name)
                arguments = dict(raw_input)
                outcome_ref = arguments.pop("outcome_ref", None)
                if outcome_ref is not None:
                    if not isinstance(outcome_ref, str) or not outcome_ref.strip() or (allowed_outcome_refs and outcome_ref not in allowed_outcome_refs):
                        raise RecoverableToolProtocolError("invalid_outcome_ref")
                evidence = None
                if internal_name in evidence_required_tools:
                    keys = {"evidence_question", "tool_arguments"}
                    if set(arguments) == keys:
                        raw_question = arguments["evidence_question"]
                        raw_arguments = arguments["tool_arguments"]
                        if not isinstance(raw_question, Mapping) or not isinstance(raw_arguments, Mapping):
                            raise RecoverableToolProtocolError("invalid_evidence_question")
                        try:
                            evidence = EvidenceQuestion.from_data(raw_question)
                        except ValueError as error:
                            raise RecoverableToolProtocolError("invalid_evidence_question") from error
                        arguments = dict(raw_arguments)
                    elif set(arguments) & keys:
                        raise RecoverableToolProtocolError("missing_evidence_question_envelope")
                    else:
                        evidence = self._automatic_evidence_question(call_id, internal_name, arguments)
                content.append(ToolCallBlock(ToolCall(call_id, internal_name, arguments, evidence, outcome_ref)))
        if not content:
            raise ModelProviderTransportError(
                "provider returned an empty message",
                reason_code="empty_message",
                category=ModelFailureCategory.INVALID_RESPONSE,
                retry_safety=ModelRetrySafety.SAFE_RESAMPLE,
            )
        raw_reason = response.get("stop_reason")
        reason_map = {
            "end_turn": FinishReason.STOP,
            "max_tokens": FinishReason.LENGTH,
            "tool_use": FinishReason.TOOL_CALL,
            "pause_turn": FinishReason.STOP,
            "refusal": FinishReason.ERROR,
        }
        if raw_reason not in reason_map:
            raise ModelProviderTransportError("unsupported Anthropic stop_reason: " + str(raw_reason))
        usage = response.get("usage", {})
        if not isinstance(usage, Mapping):
            raise ModelProviderTransportError("Anthropic usage must be an object")
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            raise ModelProviderTransportError("Anthropic usage tokens must be integers")
        diagnostics: dict[str, object] = {}
        if isinstance(response.get("id"), str):
            diagnostics["provider"] = {"response_id": response["id"], "model": response.get("model", "")}
        return ModelResponse(
            Message(str(response.get("id") or f"msg-{turn_id}-{uuid4().hex}"), MessageRole.ASSISTANT, tuple(content)),
            reason_map[raw_reason], ModelUsage(input_tokens, output_tokens), diagnostics,
        )

    @staticmethod
    def _encode_tool_name(internal_name: str) -> str:
        name = internal_name.replace("__", "_u_").replace(".", "__")
        if not name or len(name) > 256:
            raise ModelProviderTransportError("tool name cannot be represented by Anthropic")
        return name

    @staticmethod
    def _wire_tool_input(call: ToolCall, evidence_required_tools: frozenset[str], outcome_refs: frozenset[str]) -> dict[str, Any]:
        payload: dict[str, Any]
        if call.name in evidence_required_tools:
            payload = {
                "evidence_question": call.evidence_question.to_data() if call.evidence_question else None,
                "tool_arguments": dict(call.arguments),
            }
        else:
            payload = dict(call.arguments)
        if call.outcome_ref is not None:
            payload["outcome_ref"] = call.outcome_ref
        return payload

    @staticmethod
    def _automatic_evidence_question(call_id: str, tool_name: str, arguments: Mapping[str, Any]) -> EvidenceQuestion:
        encoded = json.dumps({"call_id": call_id, "tool": tool_name, "arguments": arguments}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
        question_id = "Q-auto-" + hashlib.sha256(encoded).hexdigest()[:12]
        target = next((str(arguments[key]).strip()[:160] for key in ("path", "query", "symbol") if isinstance(arguments.get(key), str) and str(arguments[key]).strip()), "the requested target")
        return EvidenceQuestion(
            question_id,
            f"What evidence does {tool_name} return for {target}?",
            expected_scope=(
                str(arguments.get("path", "")).strip()
                if tool_name in {
                    "core.read_file", "core.list_files", "core.find_files",
                    "core.search_text", "core.grep_search",
                }
                else ""
            ),
        )

    @staticmethod
    def _with_diagnostics(response: ModelResponse, *, transport_kind: str, raw_text: str, chunk_count: int, saw_stop: bool) -> ModelResponse:
        encoded = raw_text.encode()
        diagnostics = dict(response.diagnostics)
        diagnostics["transport"] = {
            "kind": transport_kind,
            "chunk_count": chunk_count,
            "saw_finish_reason": saw_stop,
            "text": {"characters": len(raw_text), "utf8_bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()},
        }
        return ModelResponse(response.message, response.finish_reason, response.usage, diagnostics)
