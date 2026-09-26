"""Dependency-free OpenAI-compatible Chat Completions Adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from tsm_agt.ports import (
    ConclusionBlock,
    ConclusionProtocolMode,
    AssistantConclusion,
    AdapterContext,
    AdapterDescriptor,
    FinishReason,
    HealthState,
    HealthStatus,
    Message,
    MessageRole,
    ModelAttemptFailed, ModelAttemptFailure, ModelFailureCategory,
    ModelRequest,
    ModelResponse,
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelTextDelta,
    ModelUsage,
    ModelTransportProgress,
    ModelRetrySafety,
    ProviderCapabilities,
    RecoverableToolProtocolError,
    TextBlock,
    ToolCall,
    EvidenceQuestion, ImageBlock, ToolCallBlock,
    ToolResultBlock,
)


_NATIVE_CONCLUSION_REQUEST = (
    "Conclusion protocol: keep the user-visible answer in content. When a "
    "structured conclusion is available, return it only in the optional "
    "assistant message field `conclusion` as an AssistantConclusion v1 object "
    "(schema_version, claims, overall_scope). Do not serialize that object in "
    "natural-language content and do not infer conclusions from prose."
)
_CONTROLLED_CONCLUSION_REQUEST = (
    "Conclusion protocol: keep the user-visible answer before the protocol. "
    "If a structured conclusion is available, end the response with exactly one "
    "<tsm-conclusion-v1>{...}</tsm-conclusion-v1> block containing one valid "
    "AssistantConclusion v1 JSON object. Nothing may follow the closing tag. "
    "Never infer a conclusion from prose."
)


class OpenAICompatibleProviderError(ModelAttemptFailed):
    """Normalized provider failure with an explicit retry classification."""

    def __init__(
        self, message: str, *, retryable: bool = False, reason_code: str = "",
        category: ModelFailureCategory | None = None,
        retry_safety: ModelRetrySafety | None = None,
    ) -> None:
        self.reason_code = reason_code or "provider_error"
        self.category = category or (
            ModelFailureCategory.TRANSIENT_PROVIDER
            if retryable else ModelFailureCategory.INVALID_RESPONSE
        )
        self.retry_safety = retry_safety or (
            ModelRetrySafety.SAFE_SAME_REQUEST
            if retryable else ModelRetrySafety.SAFE_RESAMPLE
        )
        self.retryable = self.retry_safety in {
            ModelRetrySafety.SAFE_SAME_REQUEST,
            ModelRetrySafety.SAFE_RESAMPLE,
            ModelRetrySafety.SAFE_FALLBACK_TRANSPORT,
        }
        super().__init__(ModelAttemptFailure(
            self.category, self.retry_safety, self.reason_code, message
        ))


class HttpJsonTransport(Protocol):
    async def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...

    def stream_sse(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> AsyncIterator[str]: ...


class UrllibHttpJsonTransport:
    """Small stdlib transport; tests inject a fake and never use the network."""

    async def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        return await asyncio.to_thread(
            self._post_json_sync, url, headers, payload, timeout_seconds
        )

    async def stream_sse(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> AsyncIterator[str]:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                **headers,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        loop = asyncio.get_running_loop()
        items: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        stopped = threading.Event()
        response_lock = threading.Lock()
        response_holder: list[Any] = []

        def publish(kind: str, value: object = None) -> None:
            if stopped.is_set() and kind == "data":
                return
            try:
                loop.call_soon_threadsafe(items.put_nowait, (kind, value))
            except RuntimeError:
                # The event loop may already be closed after a second Ctrl+C.
                pass

        def read_stream() -> None:
            response = None
            data_lines: list[str] = []
            terminal_published = False
            try:
                response = urlopen(request, timeout=timeout_seconds)
                with response_lock:
                    response_holder.append(response)
                while not stopped.is_set():
                    raw = response.readline()
                    if not raw:
                        if data_lines:
                            publish("data", "\n".join(data_lines))
                        break
                    line = raw.decode("utf-8", errors="strict").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            publish("data", "\n".join(data_lines))
                            data_lines.clear()
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                publish("done")
                terminal_published = True
            except HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")[:1000]
                publish("error", OpenAICompatibleProviderError(
                    f"provider returned HTTP {error.code}: {body}",
                    retryable=(error.code == 429 or 500 <= error.code < 600),
                    reason_code=f"http_{error.code}",
                ))
                terminal_published = True
            except URLError as error:
                publish("error", OpenAICompatibleProviderError(
                    f"provider connection failed: {error.reason}",
                    retryable=True, reason_code="connection_failed",
                ))
                terminal_published = True
            except UnicodeDecodeError:
                publish("error", OpenAICompatibleProviderError(
                    "provider SSE stream is not valid UTF-8"
                ))
                terminal_published = True
            except Exception as error:
                if not stopped.is_set():
                    publish("error", OpenAICompatibleProviderError(
                        f"provider SSE stream failed: {error}",
                        retryable=True, reason_code="stream_failed",
                    ))
                    terminal_published = True
            finally:
                with response_lock:
                    response_holder.clear()
                if response is not None:
                    response.close()
                if not terminal_published and not stopped.is_set():
                    publish("done")

        worker = threading.Thread(
            target=read_stream, name="tsm-agt-model-stream", daemon=True
        )
        worker.start()
        try:
            while True:
                kind, value = await items.get()
                if kind == "data":
                    yield str(value)
                elif kind == "error":
                    assert isinstance(value, Exception)
                    raise value
                else:
                    return
        finally:
            stopped.set()
            with response_lock:
                response = response_holder[0] if response_holder else None
            if response is not None:
                # Closing a buffered response concurrently with readline can itself
                # block. A daemon closer releases the socket without delaying CLI exit.
                threading.Thread(
                    target=response.close, name="tsm-agt-stream-close", daemon=True
                ).start()

    @staticmethod
    def _post_json_sync(
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")[:1000]
            transient = error.code == 429 or 500 <= error.code < 600
            raise OpenAICompatibleProviderError(
                f"provider returned HTTP {error.code}: {body}",
                retryable=transient,
                reason_code=f"http_{error.code}",
                category=(
                    ModelFailureCategory.AUTHENTICATION
                    if error.code in {401, 403}
                    else ModelFailureCategory.TRANSIENT_PROVIDER
                    if transient else ModelFailureCategory.CONFIGURATION
                ),
                retry_safety=(
                    ModelRetrySafety.SAFE_SAME_REQUEST
                    if transient else ModelRetrySafety.NEVER
                ),
            ) from error
        except TimeoutError as error:
            raise OpenAICompatibleProviderError(
                "provider response timed out", retryable=True,
                reason_code="read_timeout",
            ) from error
        except URLError as error:
            raise OpenAICompatibleProviderError(
                f"provider connection failed: {error.reason}",
                retryable=True, reason_code="connection_failed",
            ) from error
        except OSError as error:
            raise OpenAICompatibleProviderError(
                f"provider connection failed: {error}", retryable=True,
                reason_code="connection_failed",
            ) from error
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as error:
            raise OpenAICompatibleProviderError("provider returned invalid JSON") from error
        if not isinstance(decoded, Mapping):
            raise OpenAICompatibleProviderError("provider response must be a JSON object")
        return decoded


class OpenAICompatibleModelProvider:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.openai-compatible-model",
        adapter_version="0.1.0",
        port_name="ModelProviderPort",
        port_version="1.0",
        capabilities=frozenset({
            "text", "tools", "strict-json-schema", "streaming",
            "stream-cancel", "structured-conclusion",
        }),
    )
    capabilities = ProviderCapabilities(
        tools=True,
        parallel_tools=False,
        strict_json_schema=True,
        structured_conclusion=True,
        vision=True,
        stream_cancel=True,
        context_window=128_000,
    )

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        output_token_parameter: str = "max_tokens",
        strict_tool_schema: bool = True,
        streaming: bool = True,
        native_structured_conclusion: bool = True,
        transport: HttpJsonTransport | None = None,
    ) -> None:
        normalized_url = base_url.strip().rstrip("/")
        if not normalized_url.startswith(("https://", "http://")):
            raise ValueError("base_url must be an http(s) URL")
        if not model.strip():
            raise ValueError("model must not be empty")
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        if output_token_parameter not in {
            "max_tokens", "max_completion_tokens"
        }:
            raise ValueError(
                "output_token_parameter must be 'max_tokens' or "
                "'max_completion_tokens'"
            )
        self._base_url = normalized_url
        self._model = model.strip()
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._output_token_parameter = output_token_parameter
        self._strict_tool_schema = strict_tool_schema
        self._streaming = streaming
        self._native_structured_conclusion = native_structured_conclusion
        # `structured-conclusion` advertises a Kernel-reachable protocol, not
        # only an optional provider-native message field.  Every compatible
        # provider can receive the controlled text-block instruction below;
        # `_native_structured_conclusion` selects which wire protocol is used.
        descriptor_capabilities = {"text", "tools", "structured-conclusion"}
        if streaming:
            descriptor_capabilities.update({"streaming", "stream-cancel"})
        if strict_tool_schema:
            descriptor_capabilities.add("strict-json-schema")
        self.descriptor = AdapterDescriptor(
            adapter_id=type(self).descriptor.adapter_id,
            adapter_version=type(self).descriptor.adapter_version,
            port_name=type(self).descriptor.port_name,
            port_version=type(self).descriptor.port_version,
            capabilities=frozenset(descriptor_capabilities),
        )
        self.capabilities = ProviderCapabilities(
            tools=True, parallel_tools=False,
            strict_json_schema=strict_tool_schema,
            # The controlled block is an explicit, adapter-enforced fallback
            # whenever the Chat Completions provider cannot carry `conclusion`.
            structured_conclusion=True,
            vision=True,
            stream_cancel=streaming,
            context_window=128_000,
        )
        self._transport = transport or UrllibHttpJsonTransport()
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        state = HealthState.HEALTHY if self._started else HealthState.UNHEALTHY
        message = (
            f"OpenAI-compatible model {self._model} ready"
            if self._started
            else "not started"
        )
        return HealthStatus(state, message)

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._started:
            raise RuntimeError("adapter is not started")
        payload, provider_to_internal = self._build_payload(request)
        response = await self._transport.post_json(
            f"{self._base_url}/chat/completions",
            {"Authorization": f"Bearer {self._api_key}"},
            payload, request.timeout_seconds or self._timeout_seconds,
        )
        normalized = self._normalize_response(
            request.turn_id, response, provider_to_internal,
            require_evidence_questions=request.require_evidence_questions,
            evidence_required_tools=frozenset(
                tool.name for tool in request.tools
                if request.require_evidence_questions
                and tool.requires_evidence_question
            ),
            allow_text_tool_fallback=request.allow_tool_calls,
            allowed_outcome_refs=frozenset(request.outcome_refs),
            conclusion_protocol_mode=request.conclusion_protocol_mode,
        )
        raw_text = self._provider_text(response)
        return self._with_conclusion_protocol_diagnostics(
            self._with_response_diagnostics(
                normalized, transport_kind="json", raw_text=raw_text,
                chunk_count=1, saw_finish_reason=True, saw_done=None,
            ),
            request.conclusion_protocol_mode,
        )

    async def stream_complete(
        self, request: ModelRequest
    ) -> AsyncIterator[ModelStreamEvent]:
        if not self._started:
            raise RuntimeError("adapter is not started")
        if not self._streaming:
            yield ModelStreamCompleted(await self.complete(request))
            return
        async for event in self._stream_complete_once(request):
            yield event

    async def _stream_complete_once(
        self, request: ModelRequest
    ) -> AsyncIterator[ModelStreamEvent]:
        payload, provider_to_internal = self._build_payload(request)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        text_parts: list[str] = []
        pending_text_parts: list[str] = []
        conclusion_tail = ""
        conclusion_captured = False
        emitted_visible = ""
        streamed_conclusion: Mapping[str, Any] | None = None
        text_tool_candidate = True
        tool_parts: dict[int, dict[str, str]] = {}
        message_id: str | None = None
        finish_reason: object = None
        usage: Mapping[str, Any] = {}
        served_model: str | None = None
        system_fingerprint: str | None = None
        provider_routing: Mapping[str, Any] | None = None
        saw_done = False
        saw_chunk = False
        chunk_count = 0

        async for data in self._transport.stream_sse(
            f"{self._base_url}/chat/completions",
            {"Authorization": f"Bearer {self._api_key}"},
            payload,
            self._timeout_seconds,
        ):
            if data == "[DONE]":
                saw_done = True
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError as error:
                raise OpenAICompatibleProviderError(
                    "provider returned invalid SSE JSON"
                ) from error
            if not isinstance(chunk, Mapping):
                raise OpenAICompatibleProviderError(
                    "provider SSE chunk must be a JSON object"
                )
            saw_chunk = True
            chunk_count += 1
            chunk_id = chunk.get("id")
            if isinstance(chunk_id, str) and chunk_id:
                message_id = chunk_id
            chunk_model = chunk.get("model")
            if isinstance(chunk_model, str) and chunk_model:
                served_model = chunk_model
            chunk_fingerprint = chunk.get("system_fingerprint")
            if isinstance(chunk_fingerprint, str) and chunk_fingerprint:
                system_fingerprint = chunk_fingerprint
            chunk_routing = chunk.get("routing")
            if isinstance(chunk_routing, Mapping):
                provider_routing = chunk_routing
            raw_usage = chunk.get("usage")
            if raw_usage is not None:
                if not isinstance(raw_usage, Mapping):
                    raise OpenAICompatibleProviderError(
                        "provider stream usage must be an object"
                    )
                usage = raw_usage
            choices = chunk.get("choices", [])
            if not isinstance(choices, list):
                raise OpenAICompatibleProviderError(
                    "provider stream choices must be a list"
                )
            if not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, Mapping):
                raise OpenAICompatibleProviderError(
                    "provider stream choice must be an object"
                )
            raw_finish = choice.get("finish_reason")
            if raw_finish is not None:
                if finish_reason is not None:
                    raise OpenAICompatibleProviderError(
                        "provider stream repeated finish_reason"
                    )
                finish_reason = raw_finish
            delta = choice.get("delta", {})
            if not isinstance(delta, Mapping):
                raise OpenAICompatibleProviderError(
                    "provider stream delta must be an object"
                )
            content = delta.get("content")
            raw_stream_conclusion = delta.get("conclusion")
            if raw_stream_conclusion is not None:
                if streamed_conclusion is not None or not isinstance(
                    raw_stream_conclusion, Mapping
                ):
                    raise OpenAICompatibleProviderError(
                        "provider stream conclusion must be a single object"
                    )
                streamed_conclusion = raw_stream_conclusion
            if content is not None:
                if not isinstance(content, str):
                    raise OpenAICompatibleProviderError(
                        "provider text delta must be a string"
                    )
                if content:
                    text_parts.append(content)
                    if conclusion_captured:
                        visible = ""
                    else:
                        conclusion_tail += content
                        marker = "<tsm-conclusion-v1>"
                        marker_index = conclusion_tail.find(marker)
                        if marker_index >= 0:
                            visible = conclusion_tail[:marker_index]
                            conclusion_tail = conclusion_tail[marker_index:]
                            conclusion_captured = True
                        else:
                            partial_length = 0
                            for size in range(
                                min(len(marker) - 1, len(conclusion_tail)), 0, -1
                            ):
                                if conclusion_tail.endswith(marker[:size]):
                                    partial_length = size
                                    break
                            visible = (
                                conclusion_tail[:-partial_length]
                                if partial_length else conclusion_tail
                            )
                            conclusion_tail = (
                                conclusion_tail[-partial_length:]
                                if partial_length else ""
                            )
                    if visible:
                        if text_tool_candidate:
                            pending_text_parts.append(visible)
                            candidate = "".join(pending_text_parts).lstrip()
                            tool_marker = "<tool_use"
                            text_tool_candidate = (
                                tool_marker.startswith(candidate)
                                or candidate.startswith(tool_marker)
                            )
                            if not text_tool_candidate:
                                buffered = "".join(pending_text_parts)
                                pending_text_parts.clear()
                                emitted_visible += buffered
                                yield ModelTextDelta(buffered)
                        else:
                            emitted_visible += visible
                            yield ModelTextDelta(visible)
            raw_calls = delta.get("tool_calls", [])
            if not isinstance(raw_calls, list):
                raise OpenAICompatibleProviderError(
                    "provider tool call delta must be a list"
                )
            for raw_call in raw_calls:
                if not isinstance(raw_call, Mapping):
                    raise OpenAICompatibleProviderError(
                        "provider tool call delta must be an object"
                    )
                index = raw_call.get("index")
                if not isinstance(index, int) or index < 0:
                    raise OpenAICompatibleProviderError(
                        "provider tool call delta needs a non-negative index"
                    )
                aggregate = tool_parts.setdefault(
                    index, {"id": "", "name": "", "arguments": ""}
                )
                call_id = raw_call.get("id")
                if call_id is not None:
                    if not isinstance(call_id, str):
                        raise OpenAICompatibleProviderError(
                            "provider tool call id delta must be a string"
                        )
                    aggregate["id"] += call_id
                function = raw_call.get("function", {})
                if not isinstance(function, Mapping):
                    raise OpenAICompatibleProviderError(
                        "provider tool function delta must be an object"
                    )
                for key in ("name", "arguments"):
                    fragment = function.get(key)
                    if fragment is not None:
                        if not isinstance(fragment, str):
                            raise OpenAICompatibleProviderError(
                                f"provider tool {key} delta must be a string"
                            )
                        aggregate[key] += fragment

        if not saw_chunk:
            raise OpenAICompatibleProviderError(
                "provider SSE stream ended without any data",
                reason_code="empty_stream",
                category=ModelFailureCategory.INVALID_RESPONSE,
                retry_safety=ModelRetrySafety.SAFE_FALLBACK_TRANSPORT,
            )
        if finish_reason is None:
            raise OpenAICompatibleProviderError(
                "provider SSE stream ended without finish_reason",
                reason_code="incomplete_stream",
                category=ModelFailureCategory.INVALID_RESPONSE,
                retry_safety=ModelRetrySafety.SAFE_FALLBACK_TRANSPORT,
            )
        # Some OpenAI-compatible gateways close a semantically complete stream
        # after finish_reason without emitting the optional-looking [DONE]
        # sentinel.  The response is safe to accept because finish_reason and
        # the full JSON chunks already establish an application-level ending.
        raw_calls = [
            {
                "id": value["id"],
                "type": "function",
                "function": {
                    "name": value["name"],
                    "arguments": value["arguments"],
                },
            }
            for _, value in sorted(tool_parts.items())
        ]
        normalized = self._normalize_response(
            request.turn_id,
            {
                "id": message_id,
                "model": served_model,
                "system_fingerprint": system_fingerprint,
                "routing": provider_routing,
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "".join(text_parts) or None,
                        "conclusion": streamed_conclusion,
                        "tool_calls": raw_calls,
                    },
                    "finish_reason": finish_reason,
                }],
                "usage": usage,
            },
            provider_to_internal,
            require_evidence_questions=request.require_evidence_questions,
            evidence_required_tools=frozenset(
                tool.name for tool in request.tools
                if request.require_evidence_questions
                and tool.requires_evidence_question
            ),
            allow_text_tool_fallback=request.allow_tool_calls,
            allowed_outcome_refs=frozenset(request.outcome_refs),
            conclusion_protocol_mode=request.conclusion_protocol_mode,
        )
        suppression = normalized.diagnostics.get("controlled_conclusion")
        invalid_controlled_tail = (
            conclusion_captured
            and isinstance(suppression, Mapping)
            and suppression.get("status") == "invalid_suppressed"
        )
        if invalid_controlled_tail:
            # Once an opening marker reaches this transport, its remainder is
            # an internal protocol channel. Never reveal malformed, duplicate,
            # truncated, or trailing controlled JSON after withholding it.
            normalized = self._suppress_streamed_conclusion_tail(
                normalized, emitted_visible,
            )
        normalized = self._with_conclusion_protocol_diagnostics(
            self._with_response_diagnostics(
                normalized, transport_kind="sse",
                raw_text="".join(text_parts), chunk_count=chunk_count,
                saw_finish_reason=finish_reason is not None, saw_done=saw_done,
            ),
            request.conclusion_protocol_mode,
            status=(
                "controlled_block_invalid_suppressed"
                if invalid_controlled_tail else None
            ),
        )
        # Only normalized user-visible text may be released after completion.
        if normalized.message.text:
            if not normalized.message.text.startswith(emitted_visible):
                raise OpenAICompatibleProviderError(
                    "streamed text does not match normalized response"
                )
            remaining = normalized.message.text[len(emitted_visible):]
            if remaining:
                emitted_visible += remaining
                yield ModelTextDelta(remaining)
        yield ModelStreamCompleted(normalized)

    @staticmethod
    def _text_fingerprint(text: str) -> dict[str, object]:
        encoded = text.encode("utf-8")
        return {
            "characters": len(text),
            "utf8_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }

    _PROVIDER_METADATA_TEXT_LIMIT = 200

    @classmethod
    def _provider_response_diagnostics(
        cls, response: Mapping[str, Any], usage: Mapping[str, Any],
        message_id: object,
    ) -> dict[str, object]:
        """Record bounded, content-free provider metadata for post-hoc tracing.

        Gateway identity and routing fields are provider-controlled data. They
        are observability facts only: no Runtime decision reads them and they are
        never fed back into a prompt, so every copied string is length-bounded.
        """
        diagnostics: dict[str, object] = {}
        if isinstance(message_id, str) and message_id:
            diagnostics["response_id"] = cls._bounded_provider_text(message_id)
        model = response.get("model")
        if isinstance(model, str) and model.strip():
            diagnostics["model"] = cls._bounded_provider_text(model)
        fingerprint = response.get("system_fingerprint")
        if isinstance(fingerprint, str) and fingerprint.strip():
            diagnostics["system_fingerprint"] = cls._bounded_provider_text(
                fingerprint
            )
        routing = response.get("routing")
        if isinstance(routing, Mapping):
            bounded_routing = cls._scalar_mapping_diagnostics(routing)
            if bounded_routing:
                diagnostics["routing"] = bounded_routing
        for key in (
            "completion_tokens_details", "prompt_tokens_details", "total_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, Mapping):
                bounded_details = cls._scalar_mapping_diagnostics(value)
                if bounded_details:
                    diagnostics[key] = bounded_details
            elif isinstance(value, (int, float, bool)):
                diagnostics[key] = value
        return diagnostics

    @classmethod
    def _scalar_mapping_diagnostics(
        cls, value: Mapping[str, Any],
    ) -> dict[str, object]:
        bounded: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            if isinstance(item, str):
                bounded[key[:64]] = cls._bounded_provider_text(item)
            elif item is None or isinstance(item, (int, float, bool)):
                bounded[key[:64]] = item
        return bounded

    @classmethod
    def _bounded_provider_text(cls, value: str) -> str:
        return value[:cls._PROVIDER_METADATA_TEXT_LIMIT]

    @staticmethod
    def _provider_text(response: Mapping[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        choice = choices[0]
        if not isinstance(choice, Mapping):
            return ""
        message = choice.get("message")
        if not isinstance(message, Mapping):
            return ""
        content = message.get("content")
        return content if isinstance(content, str) else ""

    @classmethod
    def _with_response_diagnostics(
        cls, response: ModelResponse, *, transport_kind: str, raw_text: str,
        chunk_count: int, saw_finish_reason: bool, saw_done: bool | None,
    ) -> ModelResponse:
        transport = {
            "kind": transport_kind,
            "chunk_count": chunk_count,
            "saw_finish_reason": saw_finish_reason,
            "saw_done": saw_done,
            "text": cls._text_fingerprint(raw_text),
        }
        diagnostics = dict(response.diagnostics)
        diagnostics["transport"] = transport
        diagnostics["adapter"] = {
            "text": cls._text_fingerprint(response.message.text),
        }
        return ModelResponse(
            response.message, response.finish_reason, response.usage,
            diagnostics,
        )

    def _conclusion_protocol_instruction(
        self, mode: ConclusionProtocolMode,
    ) -> str | None:
        if mode is not ConclusionProtocolMode.REQUIRE_STRUCTURED:
            return None
        return (
            _NATIVE_CONCLUSION_REQUEST
            if self._native_structured_conclusion
            else _CONTROLLED_CONCLUSION_REQUEST
        )

    @staticmethod
    def _with_conclusion_protocol_diagnostics(
        response: ModelResponse, mode: ConclusionProtocolMode, *,
        status: str | None = None,
    ) -> ModelResponse:
        diagnostics = dict(response.diagnostics)
        controlled = diagnostics.get("controlled_conclusion")
        suppression_reason = (
            controlled.get("reason")
            if isinstance(controlled, Mapping)
            and isinstance(controlled.get("reason"), str)
            else None
        )
        if (
            mode is not ConclusionProtocolMode.REQUIRE_STRUCTURED
            and suppression_reason is None
        ):
            return response
        conclusion_present = any(
            isinstance(block, ConclusionBlock)
            for block in response.message.content
        )
        protocol_status = status
        if protocol_status is None and suppression_reason is not None:
            protocol_status = (
                "controlled_block_suppressed"
                if suppression_reason == "controlled_protocol_disabled"
                else "controlled_block_invalid_suppressed"
            )
        diagnostics["conclusion_protocol"] = {
            "requested": mode.value,
            "status": protocol_status or (
                "structured_conclusion"
                if conclusion_present else "plain_text_fallback"
            ),
        }
        if suppression_reason is not None:
            diagnostics["conclusion_protocol"]["reason"] = suppression_reason
        return ModelResponse(
            response.message, response.finish_reason, response.usage,
            diagnostics,
        )

    @staticmethod
    def _suppress_streamed_conclusion_tail(
        response: ModelResponse, visible_text: str,
    ) -> ModelResponse:
        """Drop an invalid controlled tail already withheld from stream output."""
        content = tuple(
            block for block in response.message.content
            if not isinstance(block, TextBlock)
        )
        if visible_text:
            content = (TextBlock(visible_text), *content)
        return ModelResponse(
            Message(
                response.message.message_id, response.message.role, content,
            ),
            response.finish_reason, response.usage, response.diagnostics,
        )

    def _build_payload(
        self, request: ModelRequest
    ) -> tuple[dict[str, Any], dict[str, str]]:
        provider_to_internal: dict[str, str] = {}
        internal_to_provider: dict[str, str] = {}
        for tool in request.tools:
            provider_name = self._encode_tool_name(tool.name)
            if provider_name in provider_to_internal:
                raise OpenAICompatibleProviderError(
                    f"tool names collide after provider encoding: {tool.name}"
                )
            provider_to_internal[provider_name] = tool.name
            internal_to_provider[tool.name] = provider_name
        # History tool calls are transcript facts, not capability requests. An
        # assistant message may reference a tool the Runtime has since withdrawn
        # from the advertised set (for example a completed Outcome retires the
        # explicit-completion protocol) or one a resumed checkpoint no longer
        # registers. Encode those names so the outgoing payload stays
        # well-formed; whether the model may call a tool *now* is decided by
        # `tools` alone, and an inbound call naming a non-advertised function is
        # still rejected in _normalize_response.
        history_to_provider = dict(internal_to_provider)
        for name in sorted({
            block.call.name
            for message in request.messages
            if message.role is not MessageRole.TOOL
            for block in message.content
            if isinstance(block, ToolCallBlock)
        } - set(internal_to_provider)):
            history_to_provider[name] = self._encode_tool_name(name)
        tool_outcome_refs = dict(request.tool_outcome_refs)
        provider_messages = [
            self._message_to_provider(
                message, history_to_provider,
                frozenset(
                    tool.name for tool in request.tools
                    if request.require_evidence_questions
                    and tool.requires_evidence_question
                ),
                frozenset(request.outcome_refs),
            )
            for message in request.messages
        ]
        protocol_instruction = self._conclusion_protocol_instruction(
            request.conclusion_protocol_mode
        )
        if protocol_instruction is not None:
            provider_messages.insert(0, {
                "role": "system", "content": protocol_instruction,
            })
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": provider_messages,
            self._output_token_parameter: request.max_output_tokens,
        }
        if request.tools:
            encoded_tools: list[dict[str, Any]] = []
            for tool in request.tools:
                provider_name = internal_to_provider[tool.name]
                encoded_tools.append(self._tool_to_provider(
                    tool, provider_name,
                    request.require_evidence_questions
                    and tool.requires_evidence_question,
                    self._strict_tool_schema,
                    tool_outcome_refs.get(tool.name, request.outcome_refs),
                ))
            payload["tools"] = encoded_tools
            payload["tool_choice"] = (
                "auto" if request.allow_tool_calls else "none"
            )
        return payload, provider_to_internal

    @staticmethod
    def _tool_to_provider(
        tool: Any, provider_name: str, require_evidence_questions: bool,
        strict_tool_schema: bool,
        outcome_refs: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        parameters = dict(tool.parameters)
        description = tool.description
        if require_evidence_questions:
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
                        "required": [
                            "question_id", "question", "expected_scope"
                        ],
                        "additionalProperties": False,
                    },
                    "tool_arguments": parameters,
                },
                "required": ["evidence_question", "tool_arguments"],
                "additionalProperties": False,
            }
            description = (
                f"{description} Before invoking it, bind the call to one concrete "
                "evidence question: a stable question_id and the unknown this "
                "result should clarify. Put the tool's normal parameters in "
                "tool_arguments. The function arguments must be exactly "
                "{\"evidence_question\":{\"question_id\":\"Q1\","
                "\"question\":\"What unknown will this resolve?\","
                "\"expected_scope\":\"the path this question is about\"},"
                "\"tool_arguments\":{...}}. If deliberately expanding a previously narrowed "
                "search scope, add scope_expansion_reason; otherwise omit it. "
                "For tools without a filesystem scope, expected_scope is an "
                "empty string."
            )
        if outcome_refs:
            properties = dict(parameters.get("properties", {}))
            properties["outcome_ref"] = {
                "type": "string",
                "description": (
                    "Task outcome the action should advance. Runtime validates "
                    "existence, execution focus, dependencies, and effect compatibility."
                ),
            }
            parameters = {**parameters, "properties": properties}
        function = {
            "name": provider_name,
            "description": description,
            "parameters": parameters,
        }
        if strict_tool_schema:
            function["strict"] = True
        return {
            "type": "function",
            "function": function,
        }

    @staticmethod
    def _message_to_provider(
        message: Message, internal_to_provider: Mapping[str, str],
        evidence_required_tools: frozenset[str],
        outcome_refs: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        if message.role is MessageRole.TOOL:
            result_blocks = [
                block for block in message.content if isinstance(block, ToolResultBlock)
            ]
            if len(result_blocks) != 1:
                raise OpenAICompatibleProviderError(
                    "tool message must contain exactly one tool result block"
                )
            result = result_blocks[0].result
            content = json.dumps(result.to_data(), ensure_ascii=False, separators=(",", ":"))
            return {
                "role": "tool",
                "tool_call_id": result.call_id,
                "content": content,
            }

        data: dict[str, Any] = {"role": message.role.value}
        provider_content: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                provider_content.append({
                    "type": "text",
                    "text": block.text,
                })
            elif isinstance(block, ImageBlock):
                provider_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": block.image_url,
                        "detail": block.detail,
                    },
                })

        data["content"] = provider_content or (message.text or None)
        calls = [block.call for block in message.content if isinstance(block, ToolCallBlock)]
        if calls:
            unknown = [call.name for call in calls if call.name not in internal_to_provider]
            if unknown:
                raise OpenAICompatibleProviderError(
                    "assistant history references unadvertised tools: " + ", ".join(unknown)
                )
            data["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": internal_to_provider[call.name],
                        "arguments": json.dumps(
                            (
                                {
                                    "evidence_question": (
                                        call.evidence_question.to_data()
                                        if call.evidence_question is not None else None
                                    ),
                                    "tool_arguments": dict(call.arguments),
                                    **({"outcome_ref": call.outcome_ref}
                                       if call.outcome_ref is not None else {}),
                                }
                                if call.name in evidence_required_tools
                                else {
                                    **dict(call.arguments),
                                    **({"outcome_ref": call.outcome_ref}
                                       if call.outcome_ref is not None else {}),
                                }
                            ),
                            ensure_ascii=False, separators=(",", ":")
                        ),
                    },
                }
                for call in calls
            ]
        return data

    @staticmethod
    def _encode_tool_name(internal_name: str) -> str:
        encoded = internal_name.replace("__", "_u_").replace(".", "__")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", encoded):
            raise OpenAICompatibleProviderError(
                f"tool name cannot be represented by provider: {internal_name}"
            )
        return encoded

    @classmethod
    def _normalize_response(
        cls,
        turn_id: str,
        response: Mapping[str, Any],
        provider_to_internal: Mapping[str, str],
        *, require_evidence_questions: bool = False,
        evidence_required_tools: frozenset[str] | None = None,
        allow_text_tool_fallback: bool = True,
        allowed_outcome_refs: frozenset[str] = frozenset(),
        conclusion_protocol_mode: ConclusionProtocolMode = ConclusionProtocolMode.OBSERVE,
    ) -> ModelResponse:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenAICompatibleProviderError("provider response has no choices")
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise OpenAICompatibleProviderError("provider choice must be an object")
        provider_message = choice.get("message")
        if not isinstance(provider_message, Mapping):
            raise OpenAICompatibleProviderError("provider choice has no message")

        raw_reason = choice.get("finish_reason")
        content: list[TextBlock | ToolCallBlock | ConclusionBlock] = []
        text = provider_message.get("content")
        raw_conclusion = provider_message.get("conclusion")
        raw_calls = provider_message.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise OpenAICompatibleProviderError("provider tool_calls must be a list")
        if (
            not raw_calls
            and raw_reason == "stop"
            and isinstance(text, str)
        ):
            recovered = cls._recover_text_tool_calls(
                text, provider_to_internal,
                evidence_required_tools=(
                    evidence_required_tools
                    if evidence_required_tools is not None
                    else (
                        frozenset(provider_to_internal.values())
                        if require_evidence_questions else frozenset()
                    )
                ),
            )
            if recovered is not None:
                if not allow_text_tool_fallback:
                    raise RecoverableToolProtocolError(
                        "tool_call_emitted_while_disabled"
                    )
                raw_calls = recovered
                text = None
                raw_reason = "tool_calls"
            elif cls._looks_like_text_tool_protocol(text):
                # A whole response shaped as tool protocol is never an answer.
                # Recovery above is intentionally strict; malformed, unknown,
                # or differently ordered blocks must be corrected, not printed.
                raise RecoverableToolProtocolError(
                    "invalid_text_tool_protocol"
                    if allow_text_tool_fallback
                    else "tool_call_emitted_while_disabled"
                )
        conclusion: AssistantConclusion | None = None
        controlled_suppression_reason: str | None = None
        if text is None:
            text = ""
        if isinstance(text, str):
            text, conclusion, controlled_suppression_reason = (
                cls._extract_conclusion(
                    text, raw_conclusion, conclusion_protocol_mode
                )
            )
        if isinstance(text, str) and text:
            content.append(TextBlock(text))
        if conclusion is not None:
            content.append(ConclusionBlock(conclusion))
        for raw_call in raw_calls:
            if not isinstance(raw_call, Mapping):
                raise OpenAICompatibleProviderError("provider tool call must be an object")
            function = raw_call.get("function")
            if not isinstance(function, Mapping):
                raise OpenAICompatibleProviderError("tool call function must be an object")
            call_id = raw_call.get("id")
            name = function.get("name")
            raw_arguments = function.get("arguments")
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise OpenAICompatibleProviderError("tool call id and name must be strings")
            if not isinstance(raw_arguments, str):
                raise OpenAICompatibleProviderError("tool call arguments must be JSON text")
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as error:
                raise OpenAICompatibleProviderError(
                    f"tool call {call_id} contains invalid JSON arguments"
                ) from error
            if not isinstance(arguments, Mapping):
                raise OpenAICompatibleProviderError(
                    f"tool call {call_id} arguments must be an object"
                )
            outcome_ref = arguments.get("outcome_ref")
            if outcome_ref is not None:
                if not isinstance(outcome_ref, str) or not outcome_ref.strip():
                    raise RecoverableToolProtocolError("invalid_outcome_ref")
                arguments = {
                    key: value for key, value in arguments.items()
                    if key != "outcome_ref"
                }
            internal_name = provider_to_internal.get(name)
            if internal_name is None:
                raise OpenAICompatibleProviderError(
                    f"provider requested an unadvertised tool: {name}"
                )
            evidence_question = None
            requires_evidence = (
                internal_name in evidence_required_tools
                if evidence_required_tools is not None
                else require_evidence_questions
            )
            if requires_evidence:
                envelope_keys = {"evidence_question", "tool_arguments"}
                if set(arguments) == envelope_keys:
                    raw_question = arguments.get("evidence_question")
                    tool_arguments = arguments.get("tool_arguments")
                    if not isinstance(raw_question, Mapping):
                        raise RecoverableToolProtocolError(
                            "invalid_evidence_question"
                        )
                    if not isinstance(tool_arguments, Mapping):
                        raise RecoverableToolProtocolError(
                            "invalid_tool_arguments_envelope"
                        )
                    try:
                        evidence_question = EvidenceQuestion.from_data(raw_question)
                    except ValueError as error:
                        raise RecoverableToolProtocolError(
                            "invalid_evidence_question"
                        ) from error
                    arguments = tool_arguments
                elif set(arguments) & envelope_keys:
                    raise RecoverableToolProtocolError(
                        "missing_evidence_question_envelope"
                    )
                else:
                    evidence_question = cls._automatic_evidence_question(
                        call_id, internal_name, arguments
                    )
            content.append(ToolCallBlock(ToolCall(
                call_id, internal_name, arguments, evidence_question, outcome_ref
            )))
        if not content:
            # A successful HTTP response with neither text nor a tool action is
            # not a valid assistant turn. Compatible gateways can occasionally
            # produce this transiently after a long tool history, so the caller
            # may safely resample while nothing has been shown or executed from
            # this response. Persistent emptiness still fails closed.
            raise OpenAICompatibleProviderError(
                "provider returned an empty message",
                reason_code="empty_message",
                category=ModelFailureCategory.INVALID_RESPONSE,
                retry_safety=ModelRetrySafety.SAFE_RESAMPLE,
            )

        reason_map = {
            "stop": FinishReason.STOP,
            "length": FinishReason.LENGTH,
            "tool_calls": FinishReason.TOOL_CALL,
            "function_call": FinishReason.TOOL_CALL,
            "content_filter": FinishReason.ERROR,
        }
        try:
            finish_reason = reason_map[str(raw_reason)]
        except KeyError as error:
            raise OpenAICompatibleProviderError(
                f"unsupported provider finish reason: {raw_reason}"
            ) from error
        usage = response.get("usage", {})
        if not isinstance(usage, Mapping):
            raise OpenAICompatibleProviderError("provider usage must be an object")
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            raise OpenAICompatibleProviderError("provider token usage must be integers")
        message_id = response.get("id")
        diagnostics: dict[str, object] = {}
        provider_diagnostics = cls._provider_response_diagnostics(
            response, usage, message_id
        )
        if provider_diagnostics:
            diagnostics["provider"] = provider_diagnostics
        if controlled_suppression_reason is not None:
            # Keep protocol diagnostics content-free: report only a stable
            # classification, never the model's hidden marker payload.
            diagnostics["controlled_conclusion"] = {
                "status": "disabled_suppressed"
                if controlled_suppression_reason == "controlled_protocol_disabled"
                else "invalid_suppressed",
                "reason": controlled_suppression_reason,
            }
        return ModelResponse(
            message=Message(
                message_id=(
                    str(message_id)
                    if isinstance(message_id, str) and message_id
                    else f"msg-{turn_id}-{uuid4().hex}"
                ),
                role=MessageRole.ASSISTANT,
                content=tuple(content),
            ),
            finish_reason=finish_reason,
            usage=ModelUsage(input_tokens, output_tokens),
            diagnostics=diagnostics,
        )

    @classmethod
    def _extract_conclusion(
        cls,
        text: str,
        raw_conclusion: object,
        protocol_mode: ConclusionProtocolMode,
    ) -> tuple[str, AssistantConclusion | None, str | None]:
        """Normalize explicit conclusion channels without exposing bad blocks.

        A controlled marker reserves the remainder of the assistant content for
        protocol data as soon as it appears.  Invalid, duplicate, truncated,
        or unsupported blocks therefore retain only the user-visible prefix.
        """
        native_conclusion: AssistantConclusion | None = None
        if raw_conclusion is not None and protocol_mode is not (
            ConclusionProtocolMode.DISABLED
        ):
            if isinstance(raw_conclusion, Mapping):
                try:
                    native_conclusion = AssistantConclusion.from_data(raw_conclusion)
                except (TypeError, ValueError):
                    pass

        opening = "<tsm-conclusion-v1>"
        closing = "</tsm-conclusion-v1>"
        start = text.find(opening)
        if start < 0:
            return text, native_conclusion, None

        visible_text = text[:start]
        if protocol_mode is ConclusionProtocolMode.DISABLED:
            return visible_text, None, "controlled_protocol_disabled"
        if text.count(opening) != 1:
            return visible_text, native_conclusion, "repeated_marker"
        if text.count(closing) == 0:
            return visible_text, native_conclusion, "truncated"
        if text.count(closing) != 1:
            return visible_text, native_conclusion, "repeated_marker"
        end = text.find(closing, start + len(opening))
        if end < 0:
            return visible_text, native_conclusion, "truncated"
        if text[end + len(closing):].strip():
            return visible_text, native_conclusion, "trailing_suffix"
        raw_json = text[start + len(opening):end].strip()
        if not raw_json:
            return visible_text, native_conclusion, "invalid_json"
        try:
            conclusion_data = json.loads(
                raw_json,
                object_pairs_hook=cls._reject_duplicate_json_keys,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return visible_text, native_conclusion, "invalid_json"
        if not isinstance(conclusion_data, Mapping):
            return visible_text, native_conclusion, "invalid_schema"
        try:
            controlled_conclusion = AssistantConclusion.from_data(conclusion_data)
        except (TypeError, ValueError):
            return visible_text, native_conclusion, "invalid_schema"
        if native_conclusion is not None:
            # A native field and a controlled block in one response are not a
            # valid single-channel protocol response.  Preserve the native
            # data, but never expose the controlled payload.
            return visible_text, native_conclusion, "multiple_channels"
        return visible_text, AssistantConclusion(
            schema_version=controlled_conclusion.schema_version,
            claims=controlled_conclusion.claims,
            overall_scope=controlled_conclusion.overall_scope,
            origin="controlled_text_block",
        ), None

    @staticmethod
    def _reject_duplicate_json_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate conclusion JSON key: {key}")
            result[key] = value
        return result

    @classmethod
    def _recover_text_tool_calls(
        cls, text: str, provider_to_internal: Mapping[str, str],
        evidence_required_tools: frozenset[str],
    ) -> list[dict[str, Any]] | None:
        """Recover exact top-level tool blocks emitted by compatible gateways."""
        block = re.compile(
            r'\s*<tool_use\s+id="([^"]+)"\s+name="([^"]+)"\s*>'
            r'\s*(\{.*?\})\s*</tool_use>',
            re.DOTALL,
        )
        position = 0
        recovered: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        while position < len(text):
            match = block.match(text, position)
            if match is None:
                return None
            call_id, provider_name, raw_arguments = match.groups()
            if not call_id or call_id in seen_ids:
                return None
            if provider_name not in provider_to_internal:
                return None
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                return None
            if not isinstance(arguments, Mapping):
                return None
            if provider_to_internal[provider_name] in evidence_required_tools:
                envelope_keys = {"evidence_question", "tool_arguments"}
                if set(arguments) == envelope_keys:
                    question = arguments.get("evidence_question")
                    tool_arguments = arguments.get("tool_arguments")
                    if not isinstance(question, Mapping) or not isinstance(
                        tool_arguments, Mapping
                    ):
                        return None
                    try:
                        EvidenceQuestion.from_data(question)
                    except ValueError:
                        return None
                elif set(arguments) & envelope_keys:
                    return None
            seen_ids.add(call_id)
            recovered.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": provider_name,
                    "arguments": json.dumps(
                        arguments, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            })
            position = match.end()
        return recovered or None

    @staticmethod
    def _looks_like_text_tool_protocol(text: str) -> bool:
        """Recognize a whole tool block more broadly than execution recovery."""
        return bool(re.fullmatch(
            r'\s*<tool_use(?:\s+[^>]*)?>.*</tool_use>\s*',
            text, flags=re.DOTALL,
        ))

    @staticmethod
    def _automatic_evidence_question(
        call_id: str, tool_name: str, arguments: Mapping[str, Any],
    ) -> EvidenceQuestion:
        """Add audit metadata when a compatible model omits only the envelope."""

        encoded = json.dumps(
            {"call_id": call_id, "tool": tool_name, "arguments": arguments},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        question_id = "Q-auto-" + hashlib.sha256(encoded).hexdigest()[:12]
        target = next((
            value.strip()[:160]
            for key in ("path", "query", "symbol")
            if isinstance((value := arguments.get(key)), str) and value.strip()
        ), "the requested target")
        return EvidenceQuestion(
            question_id,
            f"What evidence does {tool_name} return for {target}?",
            expected_scope=(
                str(arguments.get("path", "")).strip()
                if tool_name in {
                    "core.read_file", "core.list_files",
                    "core.find_files", "core.search_text",
                    "core.grep_search",
                } else ""
            ),
        )
