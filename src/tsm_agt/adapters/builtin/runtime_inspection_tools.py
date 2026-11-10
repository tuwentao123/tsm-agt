from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from tsm_agt.core.execution import ToolExecutionRecord
from tsm_agt.core.session_context import SessionContextProjector
from tsm_agt.core.session_history import HISTORICAL_READ_TOOLS
from tsm_agt.core.tool_result_governor import sanitize_historical_error_text
from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    Message,
    RuntimeEvent,
    RuntimeStorePort,
    SessionEvent,
    ToolCall,
    ToolEffect,
    ToolIdempotency,
    ToolInvocationContext,
    ToolResult,
    ToolResultAuthority,
    ToolRisk,
    ToolSpec,
    is_sensitive_read_path,
)

_MAX_LIMIT = 100
_DEFAULT_LIMIT = 50
_HISTORY_PAGE_CHARS = 4000


class _PermissionDenied(ValueError):
    pass


class RuntimeInspectionToolProvider:
    """Read bounded, redacted Runtime history for the invoking Task scope."""

    descriptor = AdapterDescriptor(
        adapter_id="builtin.runtime-inspection-tools",
        adapter_version="1.0.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({
            "runtime.inspect_task",
            "runtime.list_task_events",
            "runtime.inspect_session",
            "runtime.search_history",
            "runtime.read_history",
        }),
    )

    _tools = (
        ToolSpec(
            name="runtime.inspect_task",
            description=(
                "Inspect a Runtime Task within the invoking user's current Session and "
                "workspace. Without task_id, inspects the invoking Task. Explicit targets "
                "outside that scope are denied until Runtime supports confirmation. Returns "
                "only redacted Task metadata, never goals, prompts, environment, or state bodies."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "session_id": {"type": "string"},
                    "workspace": {"type": "string"},
                    "subject": {"type": "string"},
                },
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.RUNTIME_FACT,
        ),
        ToolSpec(
            name="runtime.list_task_events",
            description=(
                "List redacted Runtime events for one Task in the invoking Session and "
                "workspace. cursor is the last observed event sequence and limit is 1..100. "
                "Event payloads are hidden except the allowlisted llm.completed summary."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "session_id": {"type": "string"},
                    "workspace": {"type": "string"},
                    "subject": {"type": "string"},
                    "cursor": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT},
                },
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.RUNTIME_FACT,
        ),
        ToolSpec(
            name="runtime.inspect_session",
            description=(
                "Inspect the invoking user's current Runtime Session in the current workspace. "
                "Explicit session, workspace, or subject targets outside that scope are denied "
                "until Runtime supports confirmation. Session title, prompts, and interaction "
                "bodies are never returned."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "workspace": {"type": "string"},
                    "subject": {"type": "string"},
                },
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.RUNTIME_FACT,
        ),
        ToolSpec(
            name="runtime.search_history",
            description=(
                "Search redacted event-type history in the invoking user's current Session "
                "and workspace. The query is matched only against event type names, never "
                "prompts, assistant text, tool payloads, files, or environment values. cursor "
                "is a zero-based result offset and limit is 1..100."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 128},
                    "session_id": {"type": "string"},
                    "workspace": {"type": "string"},
                    "subject": {"type": "string"},
                    "cursor": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.RUNTIME_FACT,
        ),
        ToolSpec(
            name="runtime.read_history",
            description=(
                "Recover a bounded historical excerpt omitted from the active context. "
                "Choose exactly one: event_sequence for authored Session dialogue, "
                "turn_id for a page of recorded executions, or execution_id for a historical "
                "execution and its recorded workspace observation. execution_id also accepts "
                "a unique recorded call_id. Use the supplied task_id "
                "and next_cursor. Historical data does not establish current file contents, "
                "verification coverage, or permission to repeat an action."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "event_sequence": {"type": "integer", "minimum": 1},
                    "turn_id": {"type": "string"},
                    "execution_id": {"type": "string"},
                    "cursor": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.DERIVED,
        ),
    )

    def __init__(self, store: RuntimeStorePort) -> None:
        self._store = store
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "runtime inspection tools ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return self._tools

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        if not self._started:
            raise RuntimeError("adapter is not started")
        handlers = {
            "runtime.inspect_task": self._inspect_task,
            "runtime.list_task_events": self._list_task_events,
            "runtime.inspect_session": self._inspect_session,
            "runtime.search_history": self._search_history,
            "runtime.read_history": self._read_history,
        }
        handler = handlers.get(call.name)
        if handler is None:
            return self._error(call, "NOT_FOUND", f"unknown runtime tool: {call.name}")
        try:
            return await handler(call, context)
        except _PermissionDenied as error:
            return self._denied(call, str(error))
        except (KeyError, TypeError, ValueError) as error:
            return self._error(call, "INVALID_PARAM", str(error))

    async def _inspect_task(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        scope = await self._current_scope(context)
        self._validate_explicit_scope(call.arguments, scope)
        task_id = self._optional_string(call.arguments, "task_id") or context.task_id
        task = await self._authorized_task(task_id, scope)
        return ToolResult(call.call_id, True, data=self._task_data(task))

    async def _list_task_events(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        scope = await self._current_scope(context)
        self._validate_explicit_scope(call.arguments, scope)
        task_id = self._optional_string(call.arguments, "task_id") or context.task_id
        await self._authorized_task(task_id, scope)
        cursor = self._cursor(call.arguments)
        limit = self._limit(call.arguments)
        events = await self._store.read_events(task_id, cursor)
        page = events[:limit]
        return ToolResult(
            call.call_id,
            True,
            data={
                "task_id": task_id,
                "data": [self._task_event_data(event) for event in page],
                "next_cursor": page[-1].sequence if len(events) > limit else None,
            },
            truncated=len(events) > limit,
        )

    async def _inspect_session(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        scope = await self._current_scope(context)
        self._validate_explicit_scope(call.arguments, scope)
        requested_session = self._optional_string(call.arguments, "session_id")
        if requested_session is not None and requested_session != scope["session_id"]:
            raise _PermissionDenied("cross-session inspection requires confirmation")
        session = scope["session"]
        visible_task_ids = await self._workspace_task_ids(session.data, scope)
        active_task_id = session.data.get("active_task_id")
        return ToolResult(
            call.call_id,
            True,
            data={
                "session_id": session.session_id,
                "state": self._safe_string(session.data.get("state")),
                "version": session.version,
                "last_event_sequence": session.last_event_sequence,
                "task_ids": visible_task_ids,
                "active_task_id": (
                    str(active_task_id) if active_task_id in visible_task_ids else None
                ),
            },
        )

    async def _search_history(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        scope = await self._current_scope(context)
        self._validate_explicit_scope(call.arguments, scope)
        query = self._required_string(call.arguments, "query").casefold()
        cursor = self._cursor(call.arguments)
        limit = self._limit(call.arguments)
        records: list[dict[str, Any]] = []
        session = scope["session"]
        for task_id in await self._workspace_task_ids(session.data, scope):
            events = await self._store.read_events(task_id)
            records.extend(
                {
                    "source": "task",
                    "task_id": task_id,
                    **self._task_event_data(event),
                }
                for event in events
                if query in event.event_type.casefold()
            )
        session_events = await self._store.read_session_events(session.session_id)
        records.extend(
            {
                "source": "session",
                **self._session_event_data(event),
            }
            for event in session_events
            if query in event.event_type.casefold()
        )
        records.sort(key=lambda item: (
            str(item["occurred_at"]), str(item["source"]), int(item["sequence"])
        ))
        page = records[cursor:cursor + limit]
        next_cursor = cursor + len(page) if cursor + len(page) < len(records) else None
        return ToolResult(
            call.call_id,
            True,
            data={"data": page, "next_cursor": next_cursor},
            truncated=next_cursor is not None,
        )

    async def _current_scope(
        self, context: ToolInvocationContext,
    ) -> dict[str, Any]:
        current = await self._store.load_task(context.task_id)
        if current is None:
            raise _PermissionDenied("invoking task is unavailable for authorization")
        session_id = self._safe_string(current.data.get("session_id"))
        if not session_id:
            raise _PermissionDenied("invoking task has no authorized session binding")
        session = await self._store.load_session(session_id)
        subject = self._safe_string(session.data.get("subject")) if session else ""
        if session is None or not subject:
            raise _PermissionDenied("invoking session is unavailable for authorization")
        workspace = self._workspace_key(context.workspace)
        if self._workspace_key(current.data.get("workspace")) != workspace:
            raise _PermissionDenied("invoking task workspace does not match tool context")
        return {
            "session_id": session_id,
            "subject": subject,
            "workspace": workspace,
            "session": session,
        }

    async def _read_history(
        self, call: ToolCall, context: ToolInvocationContext,
    ) -> ToolResult:
        scope = await self._current_scope(context)
        self._validate_explicit_scope(call.arguments, scope)
        task_id = self._optional_string(call.arguments, "task_id")
        sequence = call.arguments.get("event_sequence")
        turn_id = self._optional_string(call.arguments, "turn_id")
        execution_id = self._optional_string(call.arguments, "execution_id")
        cursor = self._cursor(call.arguments)
        if sum(value is not None for value in (sequence, turn_id, execution_id)) != 1:
            raise ValueError("choose one of event_sequence, turn_id, or execution_id")
        if sequence is not None:
            if type(sequence) is not int or sequence < 1:
                raise ValueError("event_sequence must be a positive integer")
            events = await self._store.read_session_events(scope["session_id"], sequence - 1)
            if not events or events[0].sequence != sequence:
                return self._error(call, "NOT_FOUND", "history event not found")
            event = events[0]
            source_task = self._required_string(event.payload, "task_id")
            if task_id is not None and task_id != source_task:
                raise _PermissionDenied("history event is outside the requested Task")
            await self._authorized_task(source_task, scope)
            text = self._historical_dialogue(event)
            if not text:
                return self._error(call, "NOT_FOUND", "no replay-eligible dialogue in this event")
            return self._history_excerpt(call, text, cursor, task_id=source_task,
                                         event_sequence=sequence, turn_id=event.payload.get("turn_id"))

        task = await self._authorized_task(task_id or context.task_id, scope)
        records = task.data.get("tool_executions", {})
        if turn_id is not None:
            matching = sorted((
                (key, row) for key, row in records.items() if row.get("turn_id") == turn_id
            ), key=lambda pair: (pair[1].get("started_at", ""), pair[0]))
            next_cursor = cursor + 12 if len(matching) > cursor + 12 else None
            return ToolResult(call.call_id, True, data={
                "boundary": "untrusted_historical_data", "task_id": task.task_id, "turn_id": turn_id,
                "executions": [
                    {"execution_id": key, "tool": row.get("call", {}).get("name"), "state": row.get("state")}
                    for key, row in matching[cursor:cursor + 12]
                ],
                "next_cursor": next_cursor,
            }, truncated=next_cursor is not None)
        record = records.get(execution_id)
        if record is None:
            matches = [row for row in records.values() if row.get("call", {}).get("call_id") == execution_id]
            if not matches:
                return self._error(call, "NOT_FOUND", "historical execution not found")
            if len(matches) > 1:
                return ToolResult(
                    call.call_id, False, error_code="AMBIGUOUS_HISTORY_REFERENCE",
                    message="This call_id appears in more than one Turn. Retry with an exact execution_id.",
                    data={"execution_ids": [row["execution_id"] for row in matches[:12]]},
                )
            record = matches[0]
        execution = ToolExecutionRecord.from_data(record)
        fact = SessionContextProjector()._execution_event(task.task_id, execution, {})
        observation = fact["result"].pop("observation", {})
        data = execution.result.data if execution.result is not None else None
        workspace = Path(scope["workspace"])
        if (execution.call.name in HISTORICAL_READ_TOOLS and isinstance(data, Mapping)
                and observation.get("root_kind") == "PRIMARY_WORKSPACE"
                and self._workspace_key(observation.get("resolved_root")) == scope["workspace"]
                and self._is_workspace_history_path(workspace, observation.get("resolved_path"))):
            fact["result"]["observation"] = observation
            if isinstance(data.get("content"), str):
                observation["content"] = data["content"]
            for field in ("entries", "matches", "candidates"):
                observation.pop(field, None)
                if isinstance(rows := data.get(field), (tuple, list)):
                    observation[field] = [
                        row for row in rows if isinstance(row, Mapping)
                        and self._is_workspace_history_path(workspace, row.get("resolved_path") or row.get("path"))
                    ]
        # Tool results were governed when written. Use the same conservative
        # text redaction for excerpts read from legacy records.
        return self._history_excerpt(call, json.dumps(fact, ensure_ascii=False, sort_keys=True), cursor,
                                     task_id=task.task_id, execution_id=execution.execution_id)

    @staticmethod
    def _is_workspace_history_path(workspace: Path, raw: Any) -> bool:
        if not isinstance(raw, str) or not raw:
            return False
        path = workspace / raw
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return False
        return (resolved.is_relative_to(workspace)
                and not is_sensitive_read_path(path) and not is_sensitive_read_path(resolved))

    @staticmethod
    def _historical_dialogue(event: SessionEvent) -> str:
        if event.event_type == "session.task_attached":
            return str(event.payload.get("user_text") or "")
        if event.event_type not in {"session.turn_input_recorded", "session.task_result_recorded"}:
            return ""
        keys = ["user_message"]
        if event.event_type == "session.task_result_recorded" and event.payload.get("assistant_replay_eligible") is True:
            keys.append("assistant_message")
        return "\n\n".join(
            f"{key}: {Message.from_data(raw).text}"
            for key in keys if isinstance((raw := event.payload.get(key)), Mapping)
        )

    @staticmethod
    def _history_excerpt(call: ToolCall, text: str, cursor: int, **reference: Any) -> ToolResult:
        safe_text = sanitize_historical_error_text(text)
        end = cursor + _HISTORY_PAGE_CHARS
        more = end < len(safe_text)
        return ToolResult(call.call_id, True, data={
            "boundary": "untrusted_historical_data", **reference,
            "excerpt": safe_text[cursor:end], "next_cursor": end if more else None,
        }, truncated=more)

    async def _authorized_task(
        self, task_id: str, scope: Mapping[str, Any],
    ) -> Any:
        task = await self._store.load_task(task_id)
        if task is None:
            raise _PermissionDenied("requested task is unavailable in the authorized scope")
        if self._safe_string(task.data.get("session_id")) != scope["session_id"]:
            raise _PermissionDenied("cross-session inspection requires confirmation")
        if self._workspace_key(task.data.get("workspace")) != scope["workspace"]:
            raise _PermissionDenied("cross-workspace inspection requires confirmation")
        session = scope["session"]
        task_ids = session.data.get("task_ids")
        if not isinstance(task_ids, list) or task_id not in task_ids:
            raise _PermissionDenied("requested task is not bound to the authorized session")
        return task

    async def _workspace_task_ids(
        self, session_data: Mapping[str, Any], scope: Mapping[str, Any],
    ) -> list[str]:
        raw_task_ids = session_data.get("task_ids")
        if not isinstance(raw_task_ids, list):
            return []
        task_ids: list[str] = []
        for raw_task_id in raw_task_ids:
            task_id = self._safe_string(raw_task_id)
            if not task_id:
                continue
            task = await self._store.load_task(task_id)
            if task is None:
                continue
            if (
                self._safe_string(task.data.get("session_id")) == scope["session_id"]
                and self._workspace_key(task.data.get("workspace")) == scope["workspace"]
            ):
                task_ids.append(task_id)
        return task_ids

    def _validate_explicit_scope(
        self, arguments: Mapping[str, Any], scope: Mapping[str, Any],
    ) -> None:
        session_id = self._optional_string(arguments, "session_id")
        if session_id is not None and session_id != scope["session_id"]:
            raise _PermissionDenied("cross-session inspection requires confirmation")
        subject = self._optional_string(arguments, "subject")
        if subject is not None and subject != scope["subject"]:
            raise _PermissionDenied("cross-user inspection requires confirmation")
        workspace = self._optional_string(arguments, "workspace")
        if workspace is not None and self._workspace_key(workspace) != scope["workspace"]:
            raise _PermissionDenied("cross-workspace inspection requires confirmation")

    @staticmethod
    def _task_data(task: Any) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "state": RuntimeInspectionToolProvider._safe_string(task.data.get("state")),
            "workspace": RuntimeInspectionToolProvider._safe_string(task.data.get("workspace")),
            "session_id": RuntimeInspectionToolProvider._safe_string(task.data.get("session_id")),
            "version": task.version,
            "last_event_sequence": task.last_event_sequence,
        }

    @classmethod
    def _task_event_data(cls, event: RuntimeEvent) -> dict[str, Any]:
        data: dict[str, Any] = {
            "event_id": event.event_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at.isoformat(),
            "schema_version": event.schema_version,
        }
        if event.event_type == "llm.completed":
            data["summary"] = cls._llm_completed_summary(event.payload)
        return data

    @staticmethod
    def _session_event_data(event: SessionEvent) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at.isoformat(),
            "schema_version": event.schema_version,
        }

    @classmethod
    def _llm_completed_summary(cls, payload: Mapping[str, Any]) -> dict[str, Any]:
        message = payload.get("message")
        encoded = json.dumps(
            message if isinstance(message, Mapping) else {},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return {
            "message_hash": hashlib.sha256(encoded).hexdigest(),
            "message_length": len(encoded),
            "finish_reason": cls._safe_string(payload.get("finish_reason")),
        }

    @staticmethod
    def _workspace_key(value: Any) -> str:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            return ""
        return str(Path(value).expanduser().resolve(strict=False))

    @staticmethod
    def _safe_string(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    @classmethod
    def _required_string(cls, arguments: Mapping[str, Any], name: str) -> str:
        value = cls._optional_string(arguments, name)
        if not value:
            raise ValueError(f"{name} must be a non-empty string")
        return value

    @classmethod
    def _optional_string(
        cls, arguments: Mapping[str, Any], name: str,
    ) -> str | None:
        if name not in arguments:
            return None
        value = arguments[name]
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{name} must be a non-empty string")
        return normalized

    @staticmethod
    def _cursor(arguments: Mapping[str, Any]) -> int:
        value = arguments.get("cursor", 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("cursor must be a non-negative integer")
        return value

    @staticmethod
    def _limit(arguments: Mapping[str, Any]) -> int:
        value = arguments.get("limit", _DEFAULT_LIMIT)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= _MAX_LIMIT
        ):
            raise ValueError("limit must be an integer from 1 to 100")
        return value

    @staticmethod
    def _error(call: ToolCall, code: str, message: str) -> ToolResult:
        return ToolResult(call.call_id, False, error_code=code, message=message)

    @staticmethod
    def _denied(call: ToolCall, reason: str) -> ToolResult:
        return ToolResult(
            call.call_id,
            False,
            data={"audit": {"decision": "denied", "reason": reason}},
            error_code="PERMISSION_DENIED",
            message="runtime inspection target is outside the authorized scope",
        )
