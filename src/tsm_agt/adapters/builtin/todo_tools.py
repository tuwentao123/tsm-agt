"""Todo list tool: in-memory, revisioned task list for multi-step work.

Ported from Hermes ``tools/todo_tool.py`` (323 lines, ``a4301a3``-era checkout) with the
store kept **verbatim** because it is pure logic with zero Hermes dependencies -- the only
module-level import there is ``tools.registry``.  What is Hermes' and what is ours:

* **Hermes, unchanged**: ``TodoStore`` semantics -- monotonic revision, replace-vs-merge by
  id, duplicate-id collapse keeping the last position, dangling-parent removal plus cycle
  breaking, "lift ``in_progress`` ahead of any earlier ``pending`` placeholder" ordering,
  head-truncation of oversized content, and ``format_for_injection`` (which keeps only
  active items and any ancestor of an active descendant).
* **本项目新增，非 Hermes**: the ``ToolProviderPort`` shell (descriptor / ``list_tools`` /
  ``invoke``), the ``core.todo_list`` dotted name, and the evidence metadata Hermes does
  not carry.  Hermes' ``registry.register()`` has no risk/effect/authority fields at all,
  so ``effect=INTERNAL`` + ``result_authority=RUNTIME_FACT`` are declared here to place the
  tool inside tsm-agt's evidence stack instead of beside it.

State lives in this adapter, bucketed per ``ToolInvocationContext.task_id``.  That is
deliberate: the tool must not add a Kernel capability or touch the turn loop, so it cannot
be a new authority for anything the Runtime already owns.  ``is_concurrency_safe=False``
declares that one task's list must not be mutated concurrently; the contract is published
to the Runtime rather than hidden behind an internal lock.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus, ToolCall,
    ToolEffect, ToolIdempotency, ToolInvocationContext, ToolProtocol, ToolResult,
    ToolResultAuthority, ToolRisk, ToolSpec,
)

VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}
# The list is re-read after every compression (``format_for_injection``), so unbounded
# content/count would defeat the compression it rides through.  Caps apply equally to
# model-authored items and caller-replayed history.
MAX_TODO_CONTENT_CHARS = 4000
MAX_TODO_ITEMS = 256
_TRUNCATION_MARKER = "… [truncated]"
# Persisted as ordinary message content; the compressor keys on this stable header to tell
# the synthetic post-compaction row from a real user message.
TODO_INJECTION_HEADER = "[Your active task list was preserved across context compression]"
_STATUS_MARKERS = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]", "cancelled": "[~]"}
_ACTIVE_STATUSES = {"pending", "in_progress"}


class TodoStore:
    """In-memory todo list, one per Task.  List position is priority; items are
    ``{id, content, status, parent?}`` -- ``parent`` nests a subtask."""

    def __init__(self) -> None:
        self._items: list[dict[str, str]] = []
        self._revision = 0

    def _fresh_items(self, todos: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Validate, dedupe and order a whole new list (replace / restore)."""
        return self._normalize_order([self._validate(t) for t in self._dedupe_by_id(todos)])

    def write(self, todos: list[dict[str, Any]], merge: bool = False) -> list[dict[str, str]]:
        """Replace the list (default) or merge by id; returns the full list after writing."""
        before = self.read()
        if merge:
            self._merge(todos)
        else:
            self._items = self._fresh_items(todos)
        del self._items[MAX_TODO_ITEMS:]  # keep the priority head; replays can't grow unbounded
        self._sanitize_parents(self._items)
        if self._items != before:
            self._revision += 1
        return self.read()

    def _merge(self, todos: list[dict[str, Any]]) -> None:
        """Update existing items only in the fields provided; append new ones (validated)."""
        existing = {item["id"]: item for item in self._items}
        for t in self._dedupe_by_id(todos):
            item_id = str(t.get("id", "")).strip()
            if not item_id:
                continue  # can't merge without an id
            cur = existing.get(item_id)
            if cur is None:
                validated = self._validate(t)
                existing[validated["id"]] = validated
                self._items.append(validated)
                continue
            if t.get("content"):
                cur["content"] = self._cap_content(str(t["content"]).strip())
            if t.get("status") and str(t["status"]).strip().lower() in VALID_STATUSES:
                cur["status"] = str(t["status"]).strip().lower()
            if "parent" in t:
                parent = str(t["parent"] or "").strip()
                if parent:
                    cur["parent"] = parent
                else:
                    cur.pop("parent", None)
        # Rebuild preserving original order for existing items (first occurrence wins).
        rebuilt = {item["id"]: existing.get(item["id"], item) for item in self._items}
        self._items = self._normalize_order(list(rebuilt.values()))

    def read(self) -> list[dict[str, str]]:
        return [item.copy() for item in self._items]

    def has_items(self) -> bool:
        return bool(self._items)

    def snapshot(self) -> dict[str, Any]:
        """Full state clients can reconcile atomically."""
        return {"todos": self.read(), "revision": self._revision}

    def format_for_injection(self) -> str | None:
        """Render the list for post-compression injection, or None if nothing active.  Only
        pending/in_progress items are injected -- finished ones make the model re-do work
        after compression.  A parent is kept (with its real status marker) when any
        descendant is active so subtasks keep context."""
        if not self._items:
            return None
        children: dict[str, list[dict[str, str]]] = {}
        for item in self._items:
            if item.get("parent"):
                children.setdefault(item["parent"], []).append(item)

        def render(item: dict[str, str], depth: int, out: list[str]) -> bool:
            kid_lines: list[str] = []
            has_active_kid = False
            for kid in children.get(item["id"], []):
                has_active_kid |= render(kid, depth + 1, kid_lines)
            keep = item["status"] in _ACTIVE_STATUSES or has_active_kid
            if keep:
                marker = _STATUS_MARKERS.get(item["status"], "[?]")
                out.append(f"{'  ' * depth}- {marker} {item['id']}. "
                           f"{item['content']} ({item['status']})")
                out.extend(kid_lines)
            return keep

        lines = [TODO_INJECTION_HEADER]
        for item in self._items:
            if not item.get("parent"):
                render(item, 0, lines)
        return "\n".join(lines) if len(lines) > 1 else None

    @staticmethod
    def _cap_content(content: str) -> str:
        """Truncate to MAX_TODO_CONTENT_CHARS keeping the head (the actionable part) + marker."""
        if len(content) > MAX_TODO_CONTENT_CHARS:
            return content[:MAX_TODO_CONTENT_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
        return content

    @staticmethod
    def _validate(item: dict[str, Any]) -> dict[str, str]:
        """Normalize one item to ``{id, content, status, parent?}`` (placeholders when missing)."""
        if not isinstance(item, dict):
            return {"id": "?", "content": "(invalid item)", "status": "pending"}
        item_id = str(item.get("id", "")).strip() or "?"
        content = str(item.get("content", "")).strip()
        status = str(item.get("status", "pending")).strip().lower()
        result = {"id": item_id,
                  "content": TodoStore._cap_content(content) if content else "(no description)",
                  "status": status if status in VALID_STATUSES else "pending"}
        parent = str(item.get("parent") or "").strip()
        if parent and parent != item_id:
            result["parent"] = parent
        return result

    @staticmethod
    def _sanitize_parents(items: list[dict[str, str]]) -> None:
        """Drop dangling parent refs and break cycles in place (such items become roots)."""
        by_id = {item["id"]: item for item in items}
        for item in items:
            if item.get("parent") and item["parent"] not in by_id:
                item.pop("parent", None)
        for item in items:
            seen, node = {item["id"]}, item
            while node.get("parent"):
                if node["parent"] in seen:
                    item.pop("parent", None)
                    break
                seen.add(node["parent"])
                node = by_id[node["parent"]]

    @staticmethod
    def _dedupe_by_id(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse duplicate ids, keeping the last occurrence in its position."""
        last_index: dict[str, int] = {}
        for i, item in enumerate(todos):  # non-dicts get a synthetic key; _validate handles them
            key = str(item.get("id", "")).strip() if isinstance(item, dict) else f"__invalid_{i}"
            last_index[key or "?"] = i
        return [todos[i] for i in sorted(last_index.values())]

    @staticmethod
    def _normalize_order(items: list[dict[str, str]]) -> list[dict[str, str]]:
        """Lift the in_progress step ahead of any earlier pending placeholder.  Nested lists
        keep authored order -- reordering would tear a subtask from its siblings."""
        statuses = [item["status"] for item in items]
        if any(item.get("parent") for item in items) or "in_progress" not in statuses:
            return items
        active_index = statuses.index("in_progress")
        if "pending" not in statuses[:active_index]:
            return items
        normalized = items.copy()
        normalized.insert(statuses.index("pending"), normalized.pop(active_index))
        return normalized


_TODO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "maxItems": MAX_TODO_ITEMS,
            "description": "Task items to write.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string", "description": "Task description"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "cancelled"],
                    },
                    "parent": {
                        "type": "string",
                        "description": (
                            "Optional id of another item, making this a nested subtask. "
                            "Omit for top-level."
                        ),
                    },
                },
                "required": ["id", "content", "status"],
                "additionalProperties": False,
            },
        },
        "merge": {
            "type": "boolean",
            "description": (
                "true: update existing items by id, add new ones. "
                "false (default): replace the entire list with a fresh plan."
            ),
            "default": False,
        },
    },
    "required": [],
    "additionalProperties": False,
}

# Behavioral guidance is baked into the (static, cached) description; item shape and merge
# semantics live ONLY in the parameter schema.  Kept verbatim from Hermes' TODO_SCHEMA,
# including "Mark an item completed only after the work is verified done, never based on
# intent" -- that line is why this description is worth porting rather than paraphrasing.
_TODO_DESCRIPTION = (
    "Track a task list for multi-step work (3+ steps). Use for complex tasks "
    "with 3+ steps or when the user provides multiple tasks. "
    "For 'all N items' tasks, enumerate every instance as its own checklist "
    "item so none are silently dropped. "
    "Call with no parameters to read the current list.\n"
    "List order is priority. Only ONE item in_progress at a time. "
    "Break large phases into subtasks via parent. "
    "Mark an item completed only after the work is verified done, never "
    "based on intent. If something fails, cancel it and add a revised "
    "item. Always returns the full current list."
)


class CoreTodoToolProvider:
    """Session-scoped todo list, delegating all semantics to ``TodoStore``."""

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
                "write the corrected list again; the revision counter makes every state "
                "auditable"
            ),
            is_internal_state=True,
            effect=ToolEffect.INTERNAL,
            result_authority=ToolResultAuthority.RUNTIME_FACT,
            protocol=ToolProtocol.IMMEDIATE,
        ),
    )

    def __init__(self) -> None:
        self._started = False
        self._stores: dict[str, TodoStore] = {}

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "todo tools ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False
        self._stores.clear()

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return self._tools

    def store_for(self, task_id: str) -> TodoStore:
        """Per-Task store.  Exposed so the Runtime can read revisions/injection text."""

        return self._stores.setdefault(task_id, TodoStore())

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
        store = self.store_for(context.task_id)
        try:
            arguments = call.arguments
            raw_todos = arguments.get("todos")
            if raw_todos is None:
                items = store.read()
            else:
                if not isinstance(raw_todos, (list, tuple)):
                    raise ValueError(
                        f"todos must be an array, got {type(raw_todos).__name__}"
                    )
                merge = arguments.get("merge", False)
                if not isinstance(merge, bool):
                    raise ValueError("merge must be a boolean")
                items = store.write(list(raw_todos), merge)
        except (KeyError, TypeError, ValueError) as error:
            return ToolResult(
                call.call_id, False, error_code="INVALID_PARAM", message=str(error),
            )
        summary: dict[str, int] = {"total": len(items)}
        for status in ("pending", "in_progress", "completed", "cancelled"):
            summary[status] = sum(1 for item in items if item["status"] == status)
        return ToolResult(
            call.call_id, True,
            data={
                "todos": items,
                "revision": store.snapshot()["revision"],
                "summary": summary,
            },
            meta={"runtime_internal_state": True},
        )
