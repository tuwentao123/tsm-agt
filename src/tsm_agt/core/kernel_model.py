"""Model request/response validation, diagnostics and failure recording for `Kernel`.

A mixin module in the facade-plus-siblings shape (``run_agent.py`` +
``agent/session_persistence.py``): the facade ``core/kernel.py`` imports
this at module level and adds ``ModelMixin`` to ``Kernel``'s bases, so
**every method resolves through ``Kernel``'s MRO unchanged** --
``self._dependencies``, ``self._append_events`` and ``Kernel``'s own class
attributes all still resolve. Moved bodies are verbatim: nothing is renamed
and no signature changes.

This module must never bind the facade at module level; late-import it
inside a method instead (the project's module-boundary rule).

Moved from ``core/kernel.py``:

* ``_commit_model_response_checkpoint``
* ``_complete_agent_model_request``
* ``_normalize_and_validate_model_boundary``
* ``_protocol_correction_text``
* ``_record_model_attempt_events``
* ``_response_diagnostics``
* ``_response_text_fingerprint``
* ``_validate_agent_response``

Module-level symbols that moved with them, transitively. They had no
user outside the moved set, and this module must not import the facade
to reach them, so their home is here now:

* ``_GENERIC_PROTOCOL_CORRECTION``
* ``_PROTOCOL_CORRECTION_BY_REASON``
"""

from __future__ import annotations


import asyncio
import hashlib
from collections.abc import Mapping
from tsm_agt.ports import (
    FinishReason, Message, MessageRole, ModelResponse,
    ModelStreamCompleted, ModelTextDelta, ModelReasoningDelta, RecoverableToolProtocolError,
    RuntimeEvent, RuntimeUnitOfWork, StreamingModelProviderPort,
    ToolCall, ToolCallBlock, ToolResultBlock
)
from typing import Any
from uuid import uuid4
from .agent_loop import AgentCheckpointConflict
from .task import TaskSnapshot
from .turn import InvalidModelResponse


_PROTOCOL_CORRECTION_BY_REASON = {
    "tool_call_emitted_while_disabled": (
        "Your previous response attempted a tool call after the Runtime disabled "
        "tools for finalization. Do not emit XML, tool_use markup, JSON tool "
        "requests, or structured tool calls. Answer the user's request directly "
        "now from the evidence already collected, and explicitly state any "
        "remaining uncertainty."
    ),
    "invalid_tool_batch": (
        "One of your tool calls was rejected before execution: {detail} "
        "Correct its name or arguments using the advertised schema and continue "
        "the original user request. No rejected call ran. Do not replace an "
        "unfinished requested modification with instructions, a draft, or a claim "
        "that a tool or permission is unavailable. Do not repeat the rejected call "
        "unchanged."
    ),
}


_GENERIC_PROTOCOL_CORRECTION = (
    "Your previous response used an invalid tool-call payload. Retry once with "
    "the tool's declared parameters. Do not repeat or describe the invalid call."
)


class ModelMixin:
    async def _commit_model_response_checkpoint(
        self, checkpoint: AgentTurnCheckpoint, response: Any,
        prompt_receipt: PromptAssemblyReceipt, context_budget: Any, *,
        final: bool,
        additional_events: tuple[tuple[str, Mapping[str, Any]], ...] = (),
    ) -> None:
        response_diagnostics = self._response_diagnostics(response)
        user_message = self._source_user_message(checkpoint.messages)
        llm_payload = {
            "turn_id": checkpoint.turn_id,
            "orchestration_mode": "CONVERSATION_LOOP",
            "current_input_ref": (
                f"message:{user_message.message_id}"
                if user_message is not None else ""
            ),
            "prompt_projection_hash": checkpoint.session_context_hash,
            "model_call": checkpoint.model_calls,
            "message": response.message.to_data(),
            "finish_reason": response.finish_reason.value,
            "usage": response.usage.to_data(),
            "response_diagnostics": response_diagnostics,
            "context_window": context_budget.context_window,
            "context_estimated_input_tokens": context_budget.estimated_input_tokens,
            "context_estimation_method": context_budget.estimation_method,
            "context_budget": context_budget.event_data(),
            **prompt_receipt.event_data(),
        }
        if final:
            if user_message is None:
                raise RuntimeError("Agent turn lost its source user message")
            await self._commit_final_agent_result(
                task_id=checkpoint.task_id, turn_id=checkpoint.turn_id,
                user_message=user_message, assistant_message=response.message,
                llm_payload=llm_payload, additional_events=additional_events,
            )
            return
        stored = await self._require_stored_task(checkpoint.task_id)
        task = TaskSnapshot.from_data(stored.data)
        updated = task.with_agent_checkpoint(checkpoint.to_data())
        llm_event = RuntimeEvent(
            f"evt-{uuid4().hex}", checkpoint.task_id,
            stored.last_event_sequence + 1, "llm.completed", llm_payload,
        )
        extra_events = tuple(
            RuntimeEvent(
                f"evt-{uuid4().hex}", checkpoint.task_id,
                stored.last_event_sequence + index, event_type, payload,
            )
            for index, (event_type, payload) in enumerate(
                additional_events, start=2
            )
        )
        terminal_sequence = stored.last_event_sequence + 2 + len(extra_events)
        if final:
            events = (llm_event, *extra_events, RuntimeEvent(
                f"evt-{uuid4().hex}", checkpoint.task_id,
                terminal_sequence, "turn.completed",
                {"turn_id": checkpoint.turn_id,
                 "model_calls": checkpoint.model_calls,
                 "tool_calls": checkpoint.tool_calls},
            ))
        else:
            events = (llm_event, *extra_events, RuntimeEvent(
                f"evt-{uuid4().hex}", checkpoint.task_id,
                terminal_sequence, "checkpoint.saved",
                {"turn_id": checkpoint.turn_id,
                 "revision": checkpoint.revision,
                 "checkpoint_hash": checkpoint.checkpoint_hash,
                 "reason": "model-response-recorded",
                 "model_calls": checkpoint.model_calls,
                 "tool_calls": checkpoint.tool_calls,
                 "model_budget_renewal_count": (
                     checkpoint.model_budget_renewal_count
                 ),
                 "model_budget_total_granted": (
                     checkpoint.model_budget_total_granted
                 ),
                 "pending_tool_calls": len(checkpoint.pending_tool_calls)},
            ))
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            checkpoint.task_id, stored.version, updated.to_data(), events
        ))

    async def _complete_agent_model_request(
        self, request: ModelRequest, on_text_delta: Callable[[str], None] | None,
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        model = self._dependencies.model
        if not isinstance(model, StreamingModelProviderPort):
            return await model.complete(request)
        text_parts: list[str] = []
        completed: ModelResponse | None = None
        async for event in model.stream_complete(request):
            if isinstance(event, ModelTextDelta):
                if completed is not None:
                    raise InvalidModelResponse(
                        "model stream emitted text after completion"
                    )
                if not event.text:
                    raise InvalidModelResponse("model stream emitted an empty text delta")
                text_parts.append(event.text)
                if on_text_delta is not None:
                    on_text_delta(event.text)
            elif isinstance(event, ModelReasoningDelta):
                if completed is not None or not event.text:
                    raise InvalidModelResponse("invalid reasoning summary delta")
                if on_reasoning_delta is not None:
                    on_reasoning_delta(event.text)
            elif isinstance(event, ModelStreamCompleted):
                if completed is not None:
                    raise InvalidModelResponse(
                        "model stream emitted completion more than once"
                    )
                completed = event.response
            else:
                raise InvalidModelResponse("model stream emitted an unknown event")
        if completed is None:
            raise InvalidModelResponse("model stream ended without completion")
        # A StreamingModelProvider may legally finish atomically with only a
        # ModelStreamCompleted event.  This is how non-streaming compatibility
        # mode and safe stream-to-JSON fallback are exposed through the same
        # port.  When deltas were emitted, however, their exact concatenation
        # must still match the committed response so the UI cannot display one
        # answer while the Event Log records another.
        if text_parts and "".join(text_parts) != completed.message.text:
            raise InvalidModelResponse(
                "model stream text does not match the completed response"
            )
        return completed

    @staticmethod
    def _response_text_fingerprint(text: str) -> dict[str, object]:
        encoded = text.encode("utf-8")
        return {
            "characters": len(text),
            "utf8_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }

    @classmethod
    def _response_diagnostics(cls, response: ModelResponse) -> dict[str, Any]:
        inherited = dict(response.diagnostics)
        kernel_text = cls._response_text_fingerprint(response.message.text)
        persisted_text = cls._response_text_fingerprint(
            Message.from_data(response.message.to_data()).text
        )
        inherited["kernel"] = {"text": kernel_text}
        inherited["persisted"] = {"text": persisted_text}

        def digest(layer: str) -> str | None:
            value = inherited.get(layer)
            if not isinstance(value, Mapping):
                return None
            text = value.get("text")
            if not isinstance(text, Mapping):
                return None
            result = text.get("sha256")
            return str(result) if isinstance(result, str) else None

        transport_hash = digest("transport")
        adapter_hash = digest("adapter")
        kernel_hash = digest("kernel")
        persisted_hash = digest("persisted")
        inherited["checks"] = {
            "transport_matches_adapter": (
                transport_hash == adapter_hash
                if transport_hash is not None and adapter_hash is not None
                else None
            ),
            "adapter_matches_kernel": (
                adapter_hash == kernel_hash if adapter_hash is not None else None
            ),
            "kernel_matches_persisted": kernel_hash == persisted_hash,
        }
        return inherited

    @staticmethod
    def _validate_agent_response(
        message: Message, finish_reason: FinishReason, *,
        allow_tool_calls: bool = True,
    ) -> tuple[ToolCall, ...]:
        from .kernel import Kernel
        if message.role is not MessageRole.ASSISTANT:
            raise InvalidModelResponse("model response message must have assistant role")
        if any(isinstance(block, ToolResultBlock) for block in message.content):
            raise InvalidModelResponse(
                "assistant response cannot contain tool result blocks"
            )
        tool_calls = tuple(
            block.call for block in message.content if isinstance(block, ToolCallBlock)
        )
        call_ids = [call.call_id for call in tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise InvalidModelResponse(
                "assistant response contains duplicate tool call_id values"
            )
        if tool_calls and not allow_tool_calls:
            raise RecoverableToolProtocolError(
                "tool_call_emitted_while_disabled"
            )
        if not tool_calls and Kernel._looks_like_exact_text_tool_call(message.text):
            raise RecoverableToolProtocolError(
                "invalid_text_tool_protocol"
                if allow_tool_calls else "tool_call_emitted_while_disabled"
            )
        if tool_calls and finish_reason is not FinishReason.TOOL_CALL:
            raise InvalidModelResponse(
                "assistant response with tool calls must finish with tool_call"
            )
        if not tool_calls and finish_reason is FinishReason.TOOL_CALL:
            raise InvalidModelResponse(
                "tool_call finish reason requires at least one tool call"
            )
        if not tool_calls and finish_reason not in {
            FinishReason.STOP,
            FinishReason.LENGTH,
        }:
            raise InvalidModelResponse(
                f"unsupported agent finish reason: {finish_reason.value}"
            )
        if not tool_calls and not message.text:
            raise InvalidModelResponse("final assistant response must contain text")
        return tool_calls

    @staticmethod
    def _normalize_and_validate_model_boundary(
        messages: tuple[Message, ...],
    ) -> tuple[Message, ...]:
        """Enforce Provider ToolCall/ToolResult adjacency before HTTP.

        Tool results may be persisted one message per call.  This accepts that
        representation but rejects user/assistant interleaving and orphaned or
        duplicate results locally, before a Provider can return HTTP 400.
        """
        normalized: list[Message] = []
        consumed: set[int] = set()
        index = 0
        while index < len(messages):
            if index in consumed:
                index += 1
                continue
            message = messages[index]
            calls = tuple(
                block.call.call_id for block in message.content
                if isinstance(block, ToolCallBlock)
            )
            if message.role is not MessageRole.ASSISTANT or not calls:
                if message.role is MessageRole.TOOL:
                    raise AgentCheckpointConflict(
                        "orphaned ToolResult message at model boundary"
                    )
                normalized.append(message)
                index += 1
                continue
            normalized.append(message)
            expected = set(calls)
            observed: set[str] = set()
            result_messages: list[Message] = []
            cursor = index + 1
            while cursor < len(messages) and observed != expected:
                candidate = messages[cursor]
                if cursor in consumed or candidate.role is not MessageRole.TOOL:
                    cursor += 1
                    continue
                result_ids = {
                    block.result.call_id for block in candidate.content
                    if isinstance(block, ToolResultBlock)
                }
                if not result_ids.intersection(expected):
                    cursor += 1
                    continue
                for block in candidate.content:
                    if not isinstance(block, ToolResultBlock):
                        raise AgentCheckpointConflict(
                            "tool message contains a non-result block"
                        )
                    call_id = block.result.call_id
                    if call_id not in expected or call_id in observed:
                        raise AgentCheckpointConflict(
                            f"orphaned or duplicate ToolResult: {call_id}"
                        )
                    observed.add(call_id)
                result_messages.append(candidate)
                consumed.add(cursor)
                cursor += 1
            missing = expected - observed
            if missing:
                raise AgentCheckpointConflict(
                    "model boundary has unresolved ToolCall IDs: "
                    + ", ".join(sorted(missing))
                )
            normalized.extend(result_messages)
            index += 1
        return tuple(normalized)

    async def _record_model_attempt_events(
        self, task_id: str, turn_id: str, model_round: int,
        updates: list[ModelTransportProgress],
    ) -> None:
        """Persist provider-neutral physical-attempt and recovery facts."""
        if not updates:
            return
        event_data: list[tuple[str, Mapping[str, Any]]] = []
        names = {
            "attempt_started": "model.attempt_started",
            "attempt_completed": "model.attempt_completed",
            "attempt_failed": "model.attempt_failed",
            "recovery_decided": "model.recovery_decided",
        }
        for update in updates:
            event_type = names.get(update.kind)
            if event_type is None:
                continue
            event_data.append((event_type, {
                "turn_id": turn_id, "model_round": model_round,
                "provider_attempt": update.attempt,
                "max_provider_attempts": update.max_attempts,
                "category": update.category,
                "retry_safety": update.retry_safety,
                "diagnostic_code": update.diagnostic_code,
                "diagnostic_detail": update.diagnostic_detail,
                "recovery_action": update.recovery_action,
                "reason_code": update.reason,
                "delay_seconds": update.delay_seconds,
                "visible_output_emitted": update.visible_output_emitted,
                "response_committed": update.response_committed,
                "transport_mode": update.transport_mode,
            }))
        if not event_data:
            return
        for attempt in range(5):
            try:
                await self._append_events(task_id, tuple(event_data))
                return
            except ValueError as error:
                if "version conflict" not in str(error) or attempt == 4:
                    raise
                await asyncio.sleep(0)

    @staticmethod
    def _protocol_correction_text(reason_code: str, detail: str) -> str:
        """The retry text for one recoverable tool-protocol error.

        Table-driven, so a new code adds a row instead of another ladder branch,
        and callable from a test without reading source (the project's phase-boundary rule).
        """
        from .kernel import Kernel

        if reason_code == "invalid_outcome_binding":
            return Kernel._outcome_binding_correction(detail)
        template = _PROTOCOL_CORRECTION_BY_REASON.get(reason_code)
        if template is None:
            return _GENERIC_PROTOCOL_CORRECTION
        return template.format(detail=detail)
