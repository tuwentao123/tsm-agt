"""Export an opted-in Web session's execution metadata after each turn."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256

from tsm_agt.core import ToolCommitState
from tsm_agt.ports import FinishReason, RuntimeEvent, RuntimeUnitOfWork, ToolEffect, ToolRecoveryKind
from tsm_agt.ports.langfuse import (
    LangfuseClientPort, LangfuseExportError, LangfuseExporter,
)

logger = logging.getLogger(__name__)
_TURN_STATUS = {
    "turn.completed": "completed",
    "turn.failed": "failed",
    "turn.interrupted": "interrupted",
    "turn.incomplete_boundary": "incomplete",
}
_SPAN_EVENTS = (
    "conversation.model_response_recorded", "tool.started", "tool.completed", "tool.failed",
)
_TOOL_EFFECTS = frozenset(ToolEffect)
_TOOL_COMMIT_STATES = frozenset(ToolCommitState)
_TOOL_RECOVERY_KINDS = frozenset(ToolRecoveryKind)
_FINISH_REASONS = frozenset(FinishReason)


@dataclass(frozen=True)
class TurnRecording:
    session_id: str
    started: RuntimeEvent
    client: LangfuseClientPort


class WebLangfuse:
    """Session consent and pending turns live only for this Web server run."""

    def __init__(self, exporter: LangfuseExporter):
        # Injected by the composition root: this layer never imports an adapter.
        self._exporter = exporter
        self._sessions: dict[str, LangfuseClientPort] = {}
        self._turns: dict[str, TurnRecording] = {}
        self._pending: set[asyncio.Task] = set()

    def enabled(self, session_id: str) -> bool:
        return session_id in self._sessions

    async def configure(self, session_id: str, enabled: bool) -> None:
        if enabled == self.enabled(session_id):
            return
        if enabled:
            client = self._exporter.client_from_config(self._exporter.config_path())
            if (client.endpoint.scheme != "https"
                    and client.endpoint.hostname not in {"127.0.0.1", "::1", "localhost"}):
                raise LangfuseExportError("Langfuse 地址需要使用 HTTPS，本机地址可使用 HTTP。")
            self._sessions[session_id] = client
        else:
            self._sessions.pop(session_id, None)
            for task_id, recording in tuple(self._turns.items()):
                if recording.session_id == session_id:
                    del self._turns[task_id]

    async def close(self) -> None:
        self._sessions.clear()
        self._turns.clear()
        pending = tuple(self._pending)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def observe_commit(self, store, unit: RuntimeUnitOfWork) -> None:
        session_id = unit.next_state.get("session_id")
        client = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if client is None:
            return
        for event in unit.events:
            if event.event_type == "turn.started" and event.turn_id:
                self._turns[unit.task_id] = TurnRecording(session_id, event, client)
                continue
            recording = self._turns.get(unit.task_id)
            if recording is None:
                continue
            status = _TURN_STATUS.get(event.event_type) if event.turn_id == recording.started.turn_id else None
            if event.event_type == "task.state_changed" and event.payload.get("next_state") == "CANCELLED":
                status = "cancelled"
            if status:
                del self._turns[unit.task_id]
                task = asyncio.create_task(self._export(store, recording, event, status))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)

    async def _export(self, store, recording: TurnRecording, ended: RuntimeEvent, status: str) -> None:
        try:
            events = await store.read_events(
                ended.task_id, after_sequence=recording.started.sequence,
                event_types=_SPAN_EVENTS,
            )
            events = [
                event for event in events
                if event.sequence < ended.sequence and event.turn_id == recording.started.turn_id
            ]
            payload = _turn_payload(recording, ended, status, events, self._exporter)

            def send_if_enabled():
                if self._sessions.get(recording.session_id) is recording.client:
                    recording.client.send_trace(payload)

            await asyncio.to_thread(send_if_enabled)
        except Exception as error:
            # A transport or payload failure must not affect the conversation.
            logger.warning("Langfuse conversation export failed (%s)", type(error).__name__)


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _turn_payload(
    recording: TurnRecording, ended: RuntimeEvent, status: str, events: list[RuntimeEvent],
    exporter: LangfuseExporter,
) -> dict:
    start = recording.started
    session = sha256(recording.session_id.encode()).hexdigest()[:32]
    trace = sha256(f"{session}/{start.turn_id}".encode()).hexdigest()[:32]
    root = trace[:16]
    name = "tsm-agt conversation turn"
    spans = []

    def make_span(
        span_id, kind, label, started: datetime, finished: datetime, *,
        input_summary, output_summary, fields,
    ):
        span = {
            "traceId": trace, "spanId": span_id, "name": label, "kind": 1,
            "startTimeUnixNano": str(int(started.timestamp() * 1e6) * 1000),
            "endTimeUnixNano": str(int(max(started, finished).timestamp() * 1e6) * 1000),
            "attributes": exporter.attributes({
                "langfuse.session.id": session,
                "langfuse.environment": "web",
                "langfuse.trace.name": name,
                "langfuse.observation.type": kind,
                "langfuse.observation.input": input_summary,
                "langfuse.observation.output": output_summary,
                **fields,
            }),
        }
        if span_id != root:
            span["parentSpanId"] = root
        return span

    # Preview shows these summaries as input/output. Only copy counts and known
    # enum values from Runtime events, which can also contain private payloads.
    model_responses = 0
    tool_calls = 0
    tool_failures = 0
    tools_started = {}
    for event in events:
        data = event.payload
        span_id = f"{event.sequence:016x}"
        if event.event_type == "conversation.model_response_recorded":
            model_responses += 1
            model_call = _count(data.get("model_call")) or model_responses
            usage = data.get("usage") or {}
            input_tokens = _count(usage.get("input_tokens"))
            output_tokens = _count(usage.get("output_tokens"))
            tokens = {
                key: value for key, value in (("input", input_tokens), ("output", output_tokens))
                if value is not None
            }
            finish_reason = data.get("finish_reason")
            finished = event.occurred_at
            started = max(start.occurred_at, finished - timedelta(seconds=data.get("elapsed_seconds") or 0))
            spans.append(make_span(
                span_id, "generation", f"Model response {model_call}", started, finished,
                input_summary={"model_call": model_call, "input_tokens": input_tokens},
                output_summary={
                    "finish_reason": finish_reason if finish_reason in _FINISH_REASONS else None,
                    "output_tokens": output_tokens,
                    "tool_calls_requested": _count(data.get("tool_call_count")),
                },
                fields={"langfuse.observation.usage_details": tokens or None},
            ))
        elif event.event_type == "tool.started":
            tools_started[data.get("execution_id")] = event
        elif tool_start := tools_started.pop(data.get("execution_id"), None):
            effect = (tool_start.payload.get("tool") or {}).get("effect")
            category = effect if effect in _TOOL_EFFECTS else ToolEffect.UNSPECIFIED.value
            ok = event.event_type == "tool.completed"
            tool_calls += 1
            tool_failures += int(not ok)
            tool_status = "completed" if ok else "failed"
            result = data.get("result") or {}
            commit_state = data.get("commit_state")
            recovery_kind = result.get("recovery_kind")
            spans.append(make_span(
                span_id, "tool", f"Tool call · {category}", tool_start.occurred_at, event.occurred_at,
                input_summary={"category": category},
                output_summary={
                    "status": tool_status,
                    "commit_state": commit_state if commit_state in _TOOL_COMMIT_STATES else None,
                    "recovery_kind": recovery_kind if recovery_kind in _TOOL_RECOVERY_KINDS else None,
                    "retryable": result.get("retryable") is True,
                    "result_truncated": result.get("truncated") is True,
                },
                fields={
                    "langfuse.observation.metadata.effect": category,
                    "langfuse.observation.metadata.status": tool_status,
                    "langfuse.observation.level": None if ok else "ERROR",
                },
            ))
    root_span = make_span(
        root, "agent", name, start.occurred_at, ended.occurred_at,
        input_summary={"source": "tsm-agt Web", "capture": "execution_metadata"},
        output_summary={
            "status": status,
            "model_responses": model_responses,
            "tool_calls": tool_calls,
            "tool_failures": tool_failures,
        },
        fields={
            "langfuse.observation.metadata.status": status,
            "langfuse.observation.level": {"failed": "ERROR", "interrupted": "WARNING"}.get(status),
        },
    )
    return exporter.trace_payload("tsm-agt-web", [root_span, *spans])
