"""Provider-neutral messages and model invocation Port."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from .adapter import RuntimeAdapter
from .tool import ToolCall, ToolResult, ToolSpec


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALL = "tool_call"
    CANCELLED = "cancelled"
    ERROR = "error"


class RecoverableToolProtocolError(RuntimeError):
    """One model response used a correctable tool-call wire format."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    tools: bool = False
    parallel_tools: bool = False
    strict_json_schema: bool = False
    reasoning_blocks: bool = False
    vision: bool = False
    prompt_cache: bool = False
    stream_cancel: bool = False
    context_window: int = 0

    def to_data(self) -> dict[str, bool | int]:
        return {
            "tools": self.tools,
            "parallel_tools": self.parallel_tools,
            "strict_json_schema": self.strict_json_schema,
            "reasoning_blocks": self.reasoning_blocks,
            "vision": self.vision,
            "prompt_cache": self.prompt_cache,
            "stream_cancel": self.stream_cancel,
            "context_window": self.context_window,
        }


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str

    def to_data(self) -> dict[str, str]:
        return {"type": "text", "text": self.text}


@dataclass(frozen=True, slots=True)
class ToolCallBlock:
    call: ToolCall

    def to_data(self) -> dict[str, object]:
        return {"type": "tool_call", **self.call.to_data()}


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    result: ToolResult

    def to_data(self) -> dict[str, object]:
        return {"type": "tool_result", **self.result.to_data()}


MessageBlock = TextBlock | ToolCallBlock | ToolResultBlock


@dataclass(frozen=True, slots=True)
class Message:
    message_id: str
    role: MessageRole
    content: tuple[MessageBlock, ...]

    @property
    def text(self) -> str:
        return "".join(
            block.text for block in self.content if isinstance(block, TextBlock)
        )

    def to_data(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "role": self.role.value,
            "content": [block.to_data() for block in self.content],
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> Message:
        raw_content = data.get("content")
        if not isinstance(raw_content, list):
            raise ValueError("message content must be a list")
        blocks: list[MessageBlock] = []
        for raw_block in raw_content:
            if not isinstance(raw_block, Mapping):
                raise ValueError("message block must be an object")
            block_type = raw_block.get("type")
            if block_type == "text":
                blocks.append(TextBlock(str(raw_block.get("text", ""))))
            elif block_type == "tool_call":
                blocks.append(ToolCallBlock(ToolCall.from_data(raw_block)))
            elif block_type == "tool_result":
                blocks.append(ToolResultBlock(ToolResult.from_data(raw_block)))
            else:
                raise ValueError(f"unsupported message block type: {block_type}")
        return cls(
            message_id=str(data["message_id"]),
            role=MessageRole(str(data["role"])),
            content=tuple(blocks),
        )


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def to_data(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass(frozen=True, slots=True)
class ModelTransportProgress:
    """Provider-neutral progress for one physical model request attempt."""

    kind: str
    attempt: int
    max_attempts: int
    reason: str = ""
    delay_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ModelRequest:
    turn_id: str
    messages: tuple[Message, ...]
    max_output_tokens: int = 1024
    tools: tuple[ToolSpec, ...] = ()
    allow_tool_calls: bool = True
    require_evidence_questions: bool = False
    # This callback is process-local and is never serialized into a Checkpoint.
    # Adapters can expose retries without leaking HTTP/SSE details into Kernel.
    on_transport_progress: Callable[[ModelTransportProgress], None] | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    message: Message
    finish_reason: FinishReason = FinishReason.STOP
    usage: ModelUsage = ModelUsage()


class ModelProviderPort(RuntimeAdapter, Protocol):
    capabilities: ProviderCapabilities

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    """One user-displayable text fragment from a streaming model call."""

    text: str


@dataclass(frozen=True, slots=True)
class ModelStreamCompleted:
    """The single normalized, fully validated result ending one stream."""

    response: ModelResponse


ModelStreamEvent = ModelTextDelta | ModelStreamCompleted


@runtime_checkable
class StreamingModelProviderPort(Protocol):
    """Optional capability implemented without changing ModelProviderPort."""

    def stream_complete(
        self, request: ModelRequest
    ) -> AsyncIterator[ModelStreamEvent]: ...
