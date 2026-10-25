"""Task-scoped todo tool backed by the durable Working Memory plan."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus, ToolCall,
    ToolEffect, ToolIdempotency, ToolInvocationContext, ToolProtocol, ToolResult,
    ToolResultAuthority, ToolRisk, ToolSpec,
)

MAX_TODO_ITEMS = 30
_VALID_STATUSES = {"pending", "in_progress", "completed", "blocked"}
_STATUS_TO_WORKING_MEMORY = {
    "pending": "PENDING",
    "in_progress": "IN_PROGRESS",
    "completed": "COMPLETED",
    "blocked": "BLOCKED",
}
_STATUS_FROM_WORKING_MEMORY = {
    value: key for key, value in _STATUS_TO_WORKING_MEMORY.items()
}
_AUTHORED_STATE_FIELDS = (
    "goal", "constraints", "facts", "decisions", "hypotheses",
    "open_questions", "completed_work", "remaining_work", "evidence",
)

_TODO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "maxItems": MAX_TODO_ITEMS,
            "description": (
                "Task items to write. With merge=true, existing items need only "
                "id and changed fields; new items require content and status."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string", "description": "Task description"},
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending", "in_progress", "completed", "blocked",
                        ],
                    },
                    "completion_criteria": {
                        "type": "string",
                        "description": (
                            "Observable condition that proves this item is complete. "
                            "Defaults to content when omitted."
                        ),
                    },
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        },
        "merge": {
            "type": "boolean",
            "description": (
                "true: update existing items by id and append new ones; "
                "false: replace the complete list"
            ),
            "default": False,
        },
        "expected_revision": {
            "type": "integer",
            "description": (
                "Latest Working Memory revision from current_execution_state or a "
                "memory/todo result. Required when todos is present."
            ),
        },
        "operation_id": {
            "type": "string",
            "description": (
                "Stable id for this logical update. Required when todos is present."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
}

_TODO_DESCRIPTION = (
    "Track progress for complex multi-step work. Use this tool when the task has "
    "3 or more concrete steps, the user requests multiple independent changes, "
    "work spans multiple files or subsystems, or progress must survive interruption "
    "or restart. Skip creating a new plan for explanations, simple searches, "
    "single-command tasks, or one small localized change. Maintain an existing "
    "relevant plan even when this turn's change is small. When resuming work, "
    "reconcile the latest plan with current evidence and the user's scope; "
    "inspection alone need not write an unchanged list. Mark an item in_progress when starting "
    "it, and completed as soon as its completion criteria and appropriate checks "
    "are satisfied. One write can complete the previous item and start the next; "
    "do not defer these updates until the end. Update concrete blockers and scope "
    "changes. Only write when the list changes, not after every read or search. "
    "List order is priority and at most one item may be in_progress. Reuse the "
    "latest known revision as expected_revision with a stable operation_id; "
    "read with no parameters only when state is missing or after a revision "
    "conflict, then reconcile before retrying. With merge=true, send only changed "
    "items/fields; a replacement must include the complete list, never just a "
    "shortened context preview. Every call returns the complete current list. "
    "Updating this record does not perform the work or authorize old tasks."
)


def _todo_from_plan_step(raw: Mapping[str, Any]) -> dict[str, str]:
    status = str(raw.get("status", ""))
    try:
        todo_status = _STATUS_FROM_WORKING_MEMORY[status]
    except KeyError as error:
        raise ValueError(f"unknown Working Memory plan status: {status}") from error
    return {
        "id": str(raw.get("step_id", "")),
        "content": str(raw.get("description", "")),
        "status": todo_status,
        "completion_criteria": str(raw.get("completion_criteria", "")),
    }


def _todos_from_state(state: Mapping[str, Any]) -> list[dict[str, str]]:
    raw_plan = state.get("plan", [])
    if not isinstance(raw_plan, Sequence) or isinstance(raw_plan, (str, bytes)):
        raise ValueError("working memory plan must be an array")
    todos: list[dict[str, str]] = []
    for raw_step in raw_plan:
        if not isinstance(raw_step, Mapping):
            raise ValueError("working memory plan items must be objects")
        todos.append(_todo_from_plan_step(raw_step))
    return todos


def _normalize_todo(raw: Mapping[str, Any], *, partial: bool) -> dict[str, str]:
    allowed = {"id", "content", "status", "completion_criteria"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown todo fields: {', '.join(sorted(unknown))}")
    item_id = str(raw.get("id", "")).strip()
    if not item_id:
        raise ValueError("todo id must not be empty")
    normalized = {"id": item_id}
    if "content" in raw:
        content = str(raw["content"]).strip()
        if not content:
            raise ValueError(f"todo {item_id!r} content must not be empty")
        normalized["content"] = content
    elif not partial:
        raise ValueError(f"todo {item_id!r} requires content")
    if "status" in raw:
        status = str(raw["status"]).strip().lower()
        if status not in _VALID_STATUSES:
            raise ValueError(f"todo {item_id!r} has invalid status {status!r}")
        normalized["status"] = status
    elif not partial:
        raise ValueError(f"todo {item_id!r} requires status")
    if "completion_criteria" in raw:
        completion_criteria = str(raw["completion_criteria"]).strip()
        if not completion_criteria:
            raise ValueError(
                f"todo {item_id!r} completion_criteria must not be empty"
            )
        normalized["completion_criteria"] = completion_criteria
    return normalized


def _replace_todos(raw_todos: Sequence[Any]) -> list[dict[str, str]]:
    if len(raw_todos) > MAX_TODO_ITEMS:
        raise ValueError(f"todos may contain at most {MAX_TODO_ITEMS} items")
    todos: list[dict[str, str]] = []
    seen: set[str] = set()
    active_count = 0
    for raw in raw_todos:
        if not isinstance(raw, Mapping):
            raise ValueError("todo items must be objects")
        todo = _normalize_todo(raw, partial=False)
        if todo["id"] in seen:
            raise ValueError(f"duplicate todo id: {todo['id']}")
        seen.add(todo["id"])
        if todo["status"] == "in_progress":
            active_count += 1
        todo.setdefault("completion_criteria", todo["content"])
        todos.append(todo)
    if active_count > 1:
        raise ValueError("todo list may have at most one in_progress item")
    return todos


def _merge_todos(
    current: list[dict[str, str]], raw_updates: Sequence[Any],
) -> list[dict[str, str]]:
    updates: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_updates:
        if not isinstance(raw, Mapping):
            raise ValueError("todo items must be objects")
        update = _normalize_todo(raw, partial=True)
        if update["id"] in seen:
            raise ValueError(f"duplicate todo id: {update['id']}")
        seen.add(update["id"])
        updates.append(update)

    by_id = {item["id"]: item.copy() for item in current}
    order = [item["id"] for item in current]
    for update in updates:
        item_id = update["id"]
        if item_id in by_id:
            by_id[item_id].update(update)
            continue
        if "content" not in update or "status" not in update:
            raise ValueError(
                f"new todo {item_id!r} requires content and status"
            )
        update.setdefault("completion_criteria", update["content"])
        by_id[item_id] = update
        order.append(item_id)
    return _replace_todos([by_id[item_id] for item_id in order])


def _working_memory_state(
    current: Mapping[str, Any], todos: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    state = {
        field: current[field] for field in _AUTHORED_STATE_FIELDS
        if field != "goal" or field in current
    }
    state["plan"] = [
        {
            "step_id": todo["id"],
            "description": todo["content"],
            "status": _STATUS_TO_WORKING_MEMORY[todo["status"]],
            "completion_criteria": todo["completion_criteria"],
        }
        for todo in todos
    ]
    return state


def _result_data(state: Mapping[str, Any]) -> dict[str, Any]:
    todos = _todos_from_state(state)
    summary = {"total": len(todos)}
    for status in ("pending", "in_progress", "completed", "blocked"):
        summary[status] = sum(1 for item in todos if item["status"] == status)
    active = next(
        (item for item in todos if item["status"] == "in_progress"), None
    )
    next_pending = next(
        (item for item in todos if item["status"] == "pending"), None
    )
    return {
        "todos": todos,
        "revision": int(state["revision"]),
        "summary": summary,
        # Return the canonical list as well as the minimal next-step view.  A
        # model should not have to infer which item is actionable from display
        # ordering after every update or after a resumed turn.
        "plan_state": {
            "active_item_id": active["id"] if active else None,
            "next_pending_item_id": next_pending["id"] if next_pending else None,
            "open_item_ids": [
                item["id"] for item in todos
                if item["status"] in {"pending", "in_progress"}
            ],
        },
    }


class CoreTodoToolProvider:
    """Model-friendly todo view over the Task's durable Working Memory plan."""

    descriptor = AdapterDescriptor(
        "builtin.core-todo-tools", "0.1.0",
        "ToolProviderPort", "1.0",
        frozenset({"core.todo_list"}),
    )

    _tools = (
        ToolSpec(
            "core.todo_list",
            _TODO_DESCRIPTION,
            _TODO_SCHEMA,
            ToolRisk.R0, False, False, ToolIdempotency.KEYED,
            max_result_tokens=4000,
            rollback=(
                "write a corrected list with the latest Working Memory revision; "
                "prior revisions remain auditable"
            ),
            is_internal_state=True,
            effect=ToolEffect.INTERNAL,
            result_authority=ToolResultAuthority.RUNTIME_FACT,
            protocol=ToolProtocol.IMMEDIATE,
        ),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "todo tools ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return self._tools

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext,
    ) -> ToolResult:
        if not self._started:
            raise RuntimeError("adapter is not started")
        if call.name != "core.todo_list":
            return ToolResult(
                call.call_id, False, error_code="NOT_FOUND",
                message=f"unknown todo tool: {call.name}",
            )
        control = context.working_memory_control
        if control is None:
            return ToolResult(
                call.call_id, False, error_code="UNAVAILABLE",
                message="working memory capability is unavailable",
            )
        try:
            current = await control.read()
            plan_changed = False
            raw_todos = call.arguments.get("todos")
            if raw_todos is None:
                data = _result_data(current)
            else:
                if not isinstance(raw_todos, Sequence) or isinstance(
                    raw_todos, (str, bytes)
                ):
                    raise ValueError(
                        f"todos must be an array, got {type(raw_todos).__name__}"
                    )
                merge = call.arguments.get("merge", False)
                if not isinstance(merge, bool):
                    raise ValueError("merge must be a boolean")
                expected_revision = int(call.arguments["expected_revision"])
                operation_id = str(call.arguments["operation_id"]).strip()
                if not operation_id:
                    raise ValueError("operation_id must not be empty")
                current_todos = _todos_from_state(current)
                todos = (
                    _merge_todos(current_todos, raw_todos)
                    if merge else _replace_todos(raw_todos)
                )
                if todos == current_todos:
                    data = _result_data(current)
                else:
                    updated = await control.update(
                        expected_revision,
                        _working_memory_state(current, todos),
                        operation_id,
                    )
                    data = _result_data(updated)
                    plan_changed = _todos_from_state(updated) != current_todos
            return ToolResult(
                call.call_id, True, data=data,
                meta={"runtime_internal_state": True, "plan_changed": plan_changed},
            )
        except (KeyError, TypeError, ValueError) as error:
            return ToolResult(
                call.call_id, False, error_code="INVALID_PARAM", message=str(error),
            )
        except LookupError as error:
            return ToolResult(
                call.call_id, False, error_code="NOT_FOUND", message=str(error),
            )
