from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider, NetworkToolProvider
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskState
from tsm_agt.core.document_references import extract_document_references
from tsm_agt.ports import ToolCall


class ExplicitDocumentReferenceTest(unittest.IsolatedAsyncioTestCase):
    async def _executing_task(self, app, root: Path, goal: str, task_id: str, session_id: str | None = None):
        task = await app.kernel.create_task(goal, root, task_id, session_id=session_id)
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await app.kernel.transition_task(task.task_id, state, state.value)
        return task

    def test_extracts_normalized_deduplicated_http_document_urls(self):
        references = extract_document_references(
            "read https://Example.test/doc?q=1. and http://example.test/guide"
        )
        self.assertEqual([item["url"] for item in references], [
            "https://Example.test/doc?q=1", "http://example.test/guide",
        ])
        self.assertEqual(
            [item["reference_type"] for item in references],
            ["DOCUMENT_URL", "DOCUMENT_URL"],
        )

    async def test_unavailable_fetch_is_persistent_and_blocks_local_search(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "document.md").write_text("local decoy", encoding="utf-8")
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(),
                tool_adapters=(CoreReadOnlyToolProvider(),),
            )
            await app.registry.start_all()
            try:
                task = await self._executing_task(
                    app, root, "Read https://docs.example.test/spec", "task-url"
                )
                events = await app.kernel.dependencies.store.read_events(task.task_id)
                created = next(event for event in events if event.event_type == "task.created")
                self.assertEqual(
                    created.payload["document_references"][0]["url"],
                    "https://docs.example.test/spec",
                )
                self.assertEqual(
                    created.payload["document_fetch_failure"]["error_code"],
                    "DOCUMENT_FETCH_UNAVAILABLE",
                )

                result = await app.kernel.invoke_tool(
                    task.task_id, "turn-url", ToolCall(
                        "local-search", "core.search_text", {"query": "spec"}
                    )
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "DOCUMENT_FETCH_UNAVAILABLE")
                self.assertEqual(
                    result.meta["runtime_guard"],
                    "EXPLICIT_DOCUMENT_URL_LOCAL_SEARCH_BLOCKED",
                )
                events = await app.kernel.dependencies.store.read_events(task.task_id)
                self.assertFalse(any(
                    event.event_type in {"tool.prepared", "tool.started"}
                    and event.payload.get("invocation_id")
                    for event in events
                ))
                failed = [event for event in events if event.event_type == "tool.failed"]
                self.assertEqual(failed[-1].payload["result"]["error_code"], "DOCUMENT_FETCH_UNAVAILABLE")
            finally:
                await app.registry.stop_all()

    async def test_advertised_fetch_does_not_persist_unavailable_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "document.md").write_text("local decoy", encoding="utf-8")
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(),
                tool_adapters=(CoreReadOnlyToolProvider(), NetworkToolProvider()),
            )
            await app.registry.start_all()
            try:
                task = await self._executing_task(
                    app, root, "Read https://docs.example.test/spec", "task-fetch"
                )
                events = await app.kernel.dependencies.store.read_events(task.task_id)
                created = next(event for event in events if event.event_type == "task.created")
                self.assertIsNone(created.payload["document_fetch_failure"])
                self.assertIn(
                    "web.fetch_markdown",
                    {tool.name for tool in await app.kernel.list_tools()},
                )

                result = await app.kernel.invoke_tool(
                    task.task_id, "turn-fetch", ToolCall(
                        "local-search-after-fetch", "core.search_text", {"query": "spec"}
                    )
                )
                self.assertNotEqual(result.error_code, "DOCUMENT_FETCH_UNAVAILABLE")
            finally:
                await app.registry.stop_all()

    async def test_follow_up_context_keeps_structured_failure_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=()
            )
            await app.registry.start_all()
            try:
                first = await app.kernel.create_task(
                    "Read https://docs.example.test/spec", root, "task-first"
                )
                second = await app.kernel.create_task(
                    "Why did document search fail?", root, "task-second",
                    session_id=first.session_id,
                )
                message = await app.kernel._document_reference_context_message(second.task_id)
                assert message is not None
                context = json.loads(message.text)
                self.assertEqual(
                    context["references"][0]["url"], "https://docs.example.test/spec"
                )
                self.assertEqual(
                    context["failures"][0]["result"]["error_code"],
                    "DOCUMENT_FETCH_UNAVAILABLE",
                )
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
