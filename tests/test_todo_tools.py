"""Behavior contracts for the Working Memory-backed todo tool."""

from __future__ import annotations

from copy import deepcopy
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.builtin.todo_tools import CoreTodoToolProvider
from tsm_agt.ports import (
    AdapterContext, ToolCall, ToolEffect,
    ToolInvocationContext, ToolResultAuthority,
)


def _item(
    item_id: str, status: str = "pending", content: str | None = None,
    completion_criteria: str | None = None,
) -> dict[str, str]:
    item = {
        "id": item_id,
        "content": content or f"task {item_id}",
        "status": status,
    }
    if completion_criteria is not None:
        item["completion_criteria"] = completion_criteria
    return item


class _WorkingMemoryControl:
    def __init__(self, task_id: str = "task-1") -> None:
        self.state = {
            "schema_version": 1,
            "task_id": task_id,
            "revision": 1,
            "goal": "complete the task",
            "constraints": [],
            "facts": [],
            "decisions": [],
            "hypotheses": [],
            "open_questions": [],
            "plan": [],
            "completed_work": [],
            "remaining_work": [],
            "evidence": [],
        }
        self.operations: list[str] = []

    async def read(self) -> dict:
        return deepcopy(self.state)

    async def update(
        self, expected_revision: int, state: dict, operation_id: str,
    ) -> dict:
        if expected_revision != self.state["revision"]:
            raise ValueError("working memory revision conflict")
        self.operations.append(operation_id)
        self.state = {
            **self.state,
            **deepcopy(state),
            "revision": expected_revision + 1,
        }
        return deepcopy(self.state)


class TodoToolProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = CoreTodoToolProvider()
        await self.provider.start(
            AdapterContext(config={}, emit_event=lambda *args: None)
        )
        self.control = _WorkingMemoryControl()

    def _context(
        self, control: _WorkingMemoryControl | None = None,
    ) -> ToolInvocationContext:
        return ToolInvocationContext(
            invocation_id="inv-1",
            task_id="task-1",
            turn_id="turn-1",
            workspace=Path("."),
            deadline=datetime.now(),
            working_memory_control=control,
        )

    async def _call(self, arguments: dict):
        return await self.provider.invoke(
            ToolCall(call_id="c1", name="core.todo_list", arguments=arguments),
            self._context(self.control),
        )

    async def test_spec_is_internal_state_and_proves_nothing(self) -> None:
        (spec,) = await self.provider.list_tools()
        self.assertTrue(spec.is_internal_state)
        self.assertIs(spec.effect, ToolEffect.INTERNAL)
        self.assertIs(spec.result_authority, ToolResultAuthority.RUNTIME_FACT)

    async def test_read_on_fresh_working_memory_returns_empty_list(self) -> None:
        result = await self._call({})
        self.assertTrue(result.ok)
        self.assertEqual(
            result.data,
            {
                "todos": [],
                "revision": 1,
                "summary": {
                    "total": 0,
                    "pending": 0,
                    "in_progress": 0,
                    "completed": 0,
                    "blocked": 0,
                },
                "plan_state": {
                    "active_item_id": None,
                    "next_pending_item_id": None,
                    "open_item_ids": [],
                },
            },
        )

    async def test_write_updates_working_memory_plan(self) -> None:
        result = await self._call({
            "todos": [_item("a"), _item("b", "in_progress")],
            "expected_revision": 1,
            "operation_id": "create-plan",
        })
        self.assertTrue(result.ok)
        self.assertEqual(result.data["revision"], 2)
        self.assertEqual(result.data["summary"]["in_progress"], 1)
        self.assertEqual(result.data["plan_state"], {
            "active_item_id": "b",
            "next_pending_item_id": "a",
            "open_item_ids": ["a", "b"],
        })
        self.assertEqual(
            self.control.state["plan"],
            [
                {
                    "step_id": "a",
                    "description": "task a",
                    "status": "PENDING",
                    "completion_criteria": "task a",
                },
                {
                    "step_id": "b",
                    "description": "task b",
                    "status": "IN_PROGRESS",
                    "completion_criteria": "task b",
                },
            ],
        )
        self.assertEqual(self.control.operations, ["create-plan"])

    async def test_merge_updates_by_id_without_dropping_other_items(self) -> None:
        await self._call({
            "todos": [_item("a"), _item("b")],
            "expected_revision": 1,
            "operation_id": "create-plan",
        })
        result = await self._call({
            "todos": [{"id": "a", "status": "completed"}],
            "merge": True,
            "expected_revision": 2,
            "operation_id": "finish-a",
        })
        self.assertTrue(result.ok)
        self.assertEqual(
            [(item["id"], item["status"]) for item in result.data["todos"]],
            [("a", "completed"), ("b", "pending")],
        )

    async def test_more_than_one_in_progress_is_invalid(self) -> None:
        result = await self._call({
            "todos": [_item("a", "in_progress"), _item("b", "in_progress")],
            "expected_revision": 1,
            "operation_id": "invalid-plan",
        })
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")

    async def test_write_requires_revision_and_operation_id(self) -> None:
        result = await self._call({"todos": [_item("a")]})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")

    async def test_changes_noops_and_conflicts_have_distinct_observations(self) -> None:
        created = await self._call({
            "todos": [_item("a", "in_progress"), _item("b")],
            "expected_revision": 1, "operation_id": "create",
        })
        self.assertTrue(created.meta["plan_changed"])
        advanced = await self._call({
            "todos": [{"id": "a", "status": "completed"},
                      {"id": "b", "status": "in_progress"}],
            "merge": True, "expected_revision": created.data["revision"],
            "operation_id": "advance-without-preread",
        })
        self.assertTrue(advanced.meta["plan_changed"])
        unchanged = await self._call({
            "todos": [{"id": "b", "status": "in_progress"}],
            "merge": True, "expected_revision": advanced.data["revision"],
            "operation_id": "noop",
        })
        self.assertFalse(unchanged.meta["plan_changed"])
        self.assertEqual(unchanged.data["revision"], advanced.data["revision"])
        stale = await self._call({
            "todos": [{"id": "b", "status": "completed"}],
            "merge": True, "expected_revision": 1, "operation_id": "stale",
        })
        self.assertFalse(stale.ok)
        self.assertIn("revision conflict", stale.message)
        self.assertNotIn("plan_changed", stale.meta)
        self.assertFalse((await self._call({})).meta["plan_changed"])

    async def test_working_memory_notes_are_not_reported_as_plan_progress(self) -> None:
        from tsm_agt.adapters.builtin.working_memory_tools import CoreWorkingMemoryToolProvider

        provider = CoreWorkingMemoryToolProvider()
        await provider.start(AdapterContext(config={}, emit_event=lambda *args: None))
        for index, (plan, changed) in enumerate((
            ([], False),
            ([{"step_id": "a", "description": "Implement", "status": "PENDING",
               "completion_criteria": "Appropriate check passes"}], True),
        ), 1):
            state = deepcopy(self.control.state)
            state["facts"] = [f"Note {index}"]
            state["plan"] = plan
            result = await provider.invoke(ToolCall(f"wm-{index}", "core.working_memory_update", {
                "state": state, "expected_revision": self.control.state["revision"],
                "operation_id": f"wm-{index}",
            }), self._context(self.control))
            self.assertTrue(result.ok)
            self.assertIs(result.meta["plan_changed"], changed)

    async def test_missing_working_memory_control_is_unavailable(self) -> None:
        result = await self.provider.invoke(
            ToolCall(call_id="c1", name="core.todo_list", arguments={}),
            self._context(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "UNAVAILABLE")

    async def test_unknown_tool_is_not_found(self) -> None:
        result = await self.provider.invoke(
            ToolCall(call_id="c1", name="core.todo_other", arguments={}),
            self._context(self.control),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")

    async def test_stop_blocks_invoke(self) -> None:
        await self.provider.stop(datetime.now())
        with self.assertRaises(RuntimeError):
            await self._call({})


if __name__ == "__main__":
    unittest.main()
