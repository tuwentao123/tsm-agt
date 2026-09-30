"""Behavior contracts for the ported Hermes todo store and its tsm-agt tool shell.

The store semantics are Hermes'; the shell and the evidence metadata are ours.  These
tests assert relationships (revision monotonicity, parent/cycle invariants, injection
filtering) rather than snapshots, per the repo's testing rules.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.builtin.todo_tools import (
    MAX_TODO_CONTENT_CHARS, MAX_TODO_ITEMS, TODO_INJECTION_HEADER,
    CoreTodoToolProvider, TodoStore,
)
from tsm_agt.ports import (
    AdapterContext, EvidenceCapability, ToolCall, ToolEffect,
    ToolInvocationContext, ToolResultAuthority,
)


def _store_with(*items: dict) -> TodoStore:
    store = TodoStore()
    store.write(list(items))
    return store


def _item(item_id: str, status: str = "pending", content: str | None = None, parent: str | None = None) -> dict:
    out = {"id": item_id, "content": content or f"task {item_id}", "status": status}
    if parent is not None:
        out["parent"] = parent
    return out


class TodoStoreTest(unittest.TestCase):
    def test_replace_and_read_are_copies(self) -> None:
        store = _store_with(_item("a"))
        read = store.read()
        read[0]["content"] = "mutated outside"
        self.assertEqual(store.read()[0]["content"], "task a")

    def test_revision_bumps_only_on_real_change(self) -> None:
        store = TodoStore()
        self.assertEqual(store.snapshot()["revision"], 0)
        store.write([_item("a")])
        self.assertEqual(store.snapshot()["revision"], 1)
        store.write([_item("a")])
        self.assertEqual(store.snapshot()["revision"], 1, "identical write must not bump")
        store.write([_item("a", "completed")])
        self.assertEqual(store.snapshot()["revision"], 2)

    def test_merge_updates_only_provided_fields(self) -> None:
        store = _store_with(_item("a", content="original"))
        store.write([{"id": "a", "status": "completed"}], merge=True)
        item = store.read()[0]
        self.assertEqual(item["content"], "original")
        self.assertEqual(item["status"], "completed")

    def test_merge_appends_unknown_ids_without_dropping_existing(self) -> None:
        store = _store_with(_item("a"))
        store.write([_item("b")], merge=True)
        self.assertEqual([i["id"] for i in store.read()], ["a", "b"])

    def test_duplicate_ids_collapse_keeping_last_position(self) -> None:
        store = _store_with(
            _item("a", content="first"), _item("b"), _item("a", content="second"),
        )
        ids = [i["id"] for i in store.read()]
        self.assertEqual(sorted(ids), ["a", "b"])
        self.assertEqual(
            next(i for i in store.read() if i["id"] == "a")["content"], "second",
        )

    def test_missing_fields_get_placeholders(self) -> None:
        store = _store_with({}, "not-a-dict")
        items = store.read()
        self.assertEqual(len(items), 2)
        self.assertTrue(all(i["status"] == "pending" for i in items))
        self.assertEqual({i["content"] for i in items},
                         {"(no description)", "(invalid item)"})

    def test_invalid_status_normalizes_to_pending(self) -> None:
        store = _store_with(_item("a", "BOGUS"))
        self.assertEqual(store.read()[0]["status"], "pending")

    def test_dangling_parent_is_dropped(self) -> None:
        store = _store_with(_item("a", parent="ghost"))
        self.assertNotIn("parent", store.read()[0])

    def test_parent_cycle_is_broken(self) -> None:
        store = _store_with(_item("a", parent="b"), _item("b", parent="a"))
        parents = [i.get("parent") for i in store.read()]
        self.assertEqual(parents.count(None), 1, "exactly one node must become a root")

    def test_self_parent_is_ignored(self) -> None:
        store = _store_with(_item("a", parent="a"))
        self.assertNotIn("parent", store.read()[0])

    def test_in_progress_is_lifted_ahead_of_earlier_pending(self) -> None:
        store = _store_with(_item("a"), _item("b"), _item("c", "in_progress"))
        self.assertEqual([i["id"] for i in store.read()], ["c", "a", "b"])

    def test_nested_lists_keep_authored_order(self) -> None:
        store = _store_with(
            _item("a"), _item("a1", parent="a"), _item("b", "in_progress"),
        )
        self.assertEqual([i["id"] for i in store.read()], ["a", "a1", "b"])

    def test_content_is_capped_keeping_the_head(self) -> None:
        store = _store_with(_item("a", content="x" * (MAX_TODO_CONTENT_CHARS + 500)))
        content = store.read()[0]["content"]
        self.assertEqual(len(content), MAX_TODO_CONTENT_CHARS)
        self.assertTrue(content.endswith("… [truncated]"))

    def test_item_count_is_capped_keeping_the_priority_head(self) -> None:
        store = _store_with(*[_item(f"t{i}") for i in range(MAX_TODO_ITEMS + 20)])
        self.assertEqual(len(store.read()), MAX_TODO_ITEMS)

    def test_injection_is_none_when_nothing_is_active(self) -> None:
        store = _store_with(_item("a", "completed"))
        self.assertIsNone(store.format_for_injection())

    def test_injection_drops_finished_leaves_but_keeps_active_ancestors(self) -> None:
        store = _store_with(
            _item("done", "completed"),
            _item("done-child", "completed", parent="done"),
            _item("root", "completed"),
            _item("root-child", "in_progress", parent="root"),
        )
        rendered = store.format_for_injection()
        self.assertIsNotNone(rendered)
        self.assertEqual(rendered.splitlines()[0], TODO_INJECTION_HEADER)
        self.assertNotIn("done-child", rendered)
        self.assertNotIn(" done.", rendered)
        self.assertIn("root", rendered)
        self.assertIn("root-child", rendered)
        self.assertIn("[>]", rendered)


class TodoToolProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = CoreTodoToolProvider()
        await self.provider.start(AdapterContext(config={}, emit_event=lambda *a: None))

    def _context(self, task_id: str = "task-1") -> ToolInvocationContext:
        return ToolInvocationContext(
            invocation_id="inv-1", task_id=task_id, turn_id="turn-1",
            workspace=Path("."), deadline=datetime.now(),
        )

    async def _call(self, arguments: dict, task_id: str = "task-1"):
        return await self.provider.invoke(
            ToolCall(call_id="c1", name="core.todo_list", arguments=arguments),
            self._context(task_id),
        )

    async def test_spec_is_internal_state_and_proves_nothing(self) -> None:
        (spec,) = await self.provider.list_tools()
        self.assertTrue(spec.is_internal_state)
        self.assertIs(spec.effect, ToolEffect.INTERNAL)
        self.assertIs(spec.result_authority, ToolResultAuthority.RUNTIME_FACT)
        self.assertEqual(spec.evidence_capabilities, frozenset[EvidenceCapability](),
                         "an internal checklist must not be evidence for any obligation")
        self.assertFalse(spec.requires_evidence_question)

    async def test_read_on_fresh_store_returns_empty_summary(self) -> None:
        result = await self._call({})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["todos"], [])
        self.assertEqual(result.data["summary"]["total"], 0)
        self.assertEqual(result.data["revision"], 0)

    async def test_write_then_read_round_trips(self) -> None:
        written = await self._call({"todos": [_item("a"), _item("b", "in_progress")]})
        self.assertTrue(written.ok)
        self.assertEqual(written.data["summary"]["in_progress"], 1)
        read = await self._call({})
        self.assertEqual(read.data["todos"], written.data["todos"])
        self.assertEqual(read.data["revision"], written.data["revision"])

    async def test_stores_are_isolated_per_task(self) -> None:
        await self._call({"todos": [_item("a")]}, task_id="task-1")
        other = await self._call({}, task_id="task-2")
        self.assertEqual(other.data["todos"], [])

    async def test_non_array_todos_is_invalid_param(self) -> None:
        result = await self._call({"todos": "nope"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")

    async def test_non_boolean_merge_is_invalid_param(self) -> None:
        result = await self._call({"todos": [_item("a")], "merge": "yes"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")

    async def test_unknown_tool_is_not_found(self) -> None:
        result = await self.provider.invoke(
            ToolCall(call_id="c1", name="core.todo_other", arguments={}),
            self._context(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")

    async def test_stop_clears_state_and_blocks_invoke(self) -> None:
        await self._call({"todos": [_item("a")]})
        await self.provider.stop(datetime.now())
        with self.assertRaises(RuntimeError):
            await self._call({})


if __name__ == "__main__":
    unittest.main()
