"""Provider-neutral messages and model invocation Port."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from .adapter import RuntimeAdapter
from .conclusion import AssistantConclusion
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


class ModelCallPurpose(StrEnum):
    AGENT_TURN = "AGENT_TURN"
    SESSION_ROUTING = "SESSION_ROUTING"
    TASK_SPEC = "TASK_SPEC"
    COMPACTION = "COMPACTION"


class ConclusionProtocolMode(StrEnum):
    """How an adapter may accept structured assistant conclusions."""

    DISABLED = "disabled"
    OBSERVE = "observe"
    REQUIRE_STRUCTURED = "require_structured"


class RecoverableToolProtocolError(RuntimeError):
    """One model response used a correctable tool-call wire format."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(detail or reason_code)


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
    structured_conclusion: bool = False

    def to_data(self) -> dict[str, bool | int]:
        return {
            "tools": self.tools,
            "parallel_tools": self.parallel_tools,
            "strict_json_schema": self.strict_json_schema,
            "structured_conclusion": self.structured_conclusion,
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


IMAGE_DETAIL_LEVELS = ("auto", "low", "high")


@dataclass(frozen=True, slots=True)
class ImageBlock:
    """An image handed to the model provider verbatim.

    ``image_url`` reaches the provider unchanged, so what counts as an acceptable
    source is this type's own invariant, not a check each caller repeats. Only
    inline base64 data URLs are accepted today: the bytes originate in this
    deployment, so no outbound fetch is delegated to the provider.

    Supporting remote links (a CDN, say) is a single change here plus an explicit
    decision about who owns that egress and what happens when a link expires
    while it still sits in a persisted Agent checkpoint. Re-adding a per-caller
    prefix test would put the same rule in several places again, which is how
    attachments came to be dropped silently in the first place.
    """

    image_url: str
    detail: str = "auto"

    def __post_init__(self) -> None:
        if self.detail not in IMAGE_DETAIL_LEVELS:
            raise ValueError(
                "image detail must be one of "
                f"{', '.join(IMAGE_DETAIL_LEVELS)}, got {self.detail!r}"
            )
        prefix, separator, payload = self.image_url.partition(";base64,")
        if not separator or not prefix.startswith("data:image/"):
            raise ValueError(
                "image_url must be an inline base64 data URL of the form "
                "'data:image/<subtype>;base64,<payload>'; remote links are not "
                "an accepted image source yet"
            )
        if not prefix[len("data:image/"):]:
            raise ValueError("image data URL is missing its image subtype")
        if not payload:
            raise ValueError("image data URL carries no base64 payload")

    def to_data(self) -> dict[str, str]:
        return {
            "type": "input_image",
            "image_url": self.image_url,
            "detail": self.detail,
        }


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


@dataclass(frozen=True, slots=True)
class ConclusionBlock:
    conclusion: AssistantConclusion

    def to_data(self) -> dict[str, object]:
        return {"type": "conclusion", "conclusion": self.conclusion.to_data()}


MessageBlock = (
    TextBlock | ImageBlock | ToolCallBlock | ToolResultBlock | ConclusionBlock
)


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
            elif block_type == "input_image":
                blocks.append(ImageBlock(
                    image_url=str(raw_block.get("image_url", "")),
                    detail=str(raw_block.get("detail", "auto")),
                ))
            elif block_type == "tool_call":
                blocks.append(ToolCallBlock(ToolCall.from_data(raw_block)))
            elif block_type == "tool_result":
                blocks.append(ToolResultBlock(ToolResult.from_data(raw_block)))
            elif block_type == "conclusion":
                raw_conclusion = raw_block.get("conclusion")
                if not isinstance(raw_conclusion, Mapping):
                    raise ValueError("conclusion block requires an object conclusion")
                blocks.append(ConclusionBlock(
                    AssistantConclusion.from_data(raw_conclusion)
                ))
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
    category: str = ""
    retry_safety: str = ""
    diagnostic_code: str = ""
    diagnostic_detail: str = ""
    recovery_action: str = ""
    visible_output_emitted: bool = False
    response_committed: bool = False
    transport_mode: str = ""

    def to_data(self) -> dict[str, object]:
        return {
            "kind": self.kind, "attempt": self.attempt,
            "max_attempts": self.max_attempts, "reason": self.reason,
            "delay_seconds": self.delay_seconds, "category": self.category,
            "retry_safety": self.retry_safety,
            "diagnostic_code": self.diagnostic_code,
            "diagnostic_detail": self.diagnostic_detail,
            "recovery_action": self.recovery_action,
            "visible_output_emitted": self.visible_output_emitted,
            "response_committed": self.response_committed,
            "transport_mode": self.transport_mode,
        }


@dataclass(frozen=True, slots=True)
class ModelRequest:
    turn_id: str
    messages: tuple[Message, ...]
    max_output_tokens: int = 1024
    tools: tuple[ToolSpec, ...] = ()
    allow_tool_calls: bool = True
    require_evidence_questions: bool = False
    outcome_refs: tuple[str, ...] = ()
    # This callback is process-local and is never serialized into a Checkpoint.
    # Adapters can expose retries without leaking HTTP/SSE details into Kernel.
    on_transport_progress: Callable[[ModelTransportProgress], Any] | None = None
    # Optional per-tool narrowing. Runtime computes this from the authoritative
    # TaskSpec so Provider schemas do not advertise impossible Tool→Outcome pairs.
    # Kept after the older callback field to preserve positional compatibility.
    tool_outcome_refs: tuple[tuple[str, tuple[str, ...]], ...] = ()
    purpose: ModelCallPurpose = ModelCallPurpose.AGENT_TURN
    timeout_seconds: float | None = None
    max_provider_attempts: int | None = None
    conclusion_protocol_mode: ConclusionProtocolMode = ConclusionProtocolMode.OBSERVE


@dataclass(frozen=True, slots=True)
class ModelResponse:
    message: Message
    finish_reason: FinishReason = FinishReason.STOP
    usage: ModelUsage = ModelUsage()
    diagnostics: Mapping[str, object] = field(default_factory=dict)


class ModelProviderPort(RuntimeAdapter, Protocol):
    capabilities: ProviderCapabilities

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    """One user-displayable text fragment from a streaming model call."""

    text: str


@dataclass(frozen=True, slots=True)
class ModelStreamCompleted:
    """The single normalized, fully validated result ending one stream.

    A Provider may emit this as the only event for non-streaming compatibility
    or a safe transport fallback.  If text deltas precede it, they must describe
    the same assistant text as this committed response.
    """

    response: ModelResponse


ModelStreamEvent = ModelTextDelta | ModelStreamCompleted


@runtime_checkable
class StreamingModelProviderPort(Protocol):
    """Optional capability implemented without changing ModelProviderPort."""

    def stream_complete(
        self, request: ModelRequest
    ) -> AsyncIterator[ModelStreamEvent]: ...
