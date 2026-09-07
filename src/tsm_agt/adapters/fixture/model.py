from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    FinishReason,
    HealthState,
    HealthStatus,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    TextBlock,
    ToolCall,
    ToolCallBlock,
    ToolResultBlock,
    AdapterContext,
    AdapterDescriptor,
)


class EchoModelProvider:
    capabilities = ProviderCapabilities(context_window=4096)
    descriptor = AdapterDescriptor(
        adapter_id="fixture.echo-model",
        adapter_version="0.1.0",
        port_name="ModelProviderPort",
        port_version="1.0",
        capabilities=frozenset({"text"}),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        state = HealthState.HEALTHY if self._started else HealthState.UNHEALTHY
        return HealthStatus(state, "echo model ready" if self._started else "not started")

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._started:
            raise RuntimeError("adapter is not started")
        text = request.messages[-1].text if request.messages else ""
        input_tokens = sum(len(message.text.split()) for message in request.messages)
        return ModelResponse(
            message=Message(
                message_id=f"msg-assistant-{request.turn_id}",
                role=MessageRole.ASSISTANT,
                content=(TextBlock(text),),
            ),
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=len(text.split()),
            ),
        )


class ToolCallingModelProvider(EchoModelProvider):
    """Requests fixture.echo once, then summarizes the returned observation."""

    descriptor = AdapterDescriptor(
        adapter_id="fixture.tool-calling-model",
        adapter_version="0.1.0",
        port_name="ModelProviderPort",
        port_version="1.0",
        capabilities=frozenset({"text", "tools"}),
    )
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._started:
            raise RuntimeError("adapter is not started")
        tool_result = next(
            (
                block.result
                for message in reversed(request.messages)
                for block in message.content
                if isinstance(block, ToolResultBlock)
            ),
            None,
        )
        if tool_result is None:
            prompt = next(
                message.text for message in reversed(request.messages)
                if message.role is MessageRole.USER
                and not message.message_id.startswith((
                    "project-onboarding-context-",
                    "project-memory-context-", "session-context-",
                    "working-memory-context-",
                ))
            )
            return ModelResponse(
                message=Message(
                    message_id=f"msg-assistant-tool-{request.turn_id}",
                    role=MessageRole.ASSISTANT,
                    content=(
                        TextBlock("I will check with the echo tool."),
                        ToolCallBlock(
                            ToolCall(
                                call_id=f"call-{request.turn_id}",
                                name="fixture.echo",
                                arguments={"text": prompt},
                            )
                        ),
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALL,
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            )
        observed = (tool_result.data or {}).get("text", "")
        return ModelResponse(
            message=Message(
                message_id=f"msg-assistant-final-{request.turn_id}",
                role=MessageRole.ASSISTANT,
                content=(TextBlock(f"Tool observed: {observed}"),),
            ),
            finish_reason=FinishReason.STOP,
            usage=ModelUsage(input_tokens=2, output_tokens=2),
        )
