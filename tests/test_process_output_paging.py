from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

import pytest

from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    ApprovalDecision, ApprovalRequired, ContextWindowExceeded, ContextWindowManager,
    ProjectTrustLevel, PromptTemplate,
)
from tsm_agt.ports import Message, MessageRole, TextBlock, ToolCall, ToolCallBlock, ToolResult, ToolResultBlock
from tsm_agt.ports.process import PROCESS_OUTPUT_PAGE_BYTES, process_output_page
from tests.test_conversation_loop import _executing
from tests.test_core_process_tools import _AllowProcessSandbox


class ProcessOutputPagingTest(unittest.IsolatedAsyncioTestCase):
    async def application(self, root, *, database=None):
        options = {"store_adapter": SQLiteRuntimeStore(database)} if database else {}
        app = compose_fixture_application(sandbox_adapter=_AllowProcessSandbox(),
                                          tool_adapters=(CoreProcessToolProvider(),), **options)
        await app.registry.start_all()
        await app.kernel.set_project_trust(root, ProjectTrustLevel.TRUSTED_BUILD)
        return app

    async def command(self, app, task_id, script, *, mode="foreground"):
        call = ToolCall("run", "core.run_command", {
            "argv": [sys.executable, "-c", script], "cwd": ".", "mode": mode,
        })
        with self.assertRaises(ApprovalRequired) as pending:
            await app.kernel.invoke_tool(task_id, "turn-command", call)
        request = pending.exception.request
        return await app.kernel.resolve_approval(task_id, request.request_id, request.payload_hash,
                                                 ApprovalDecision.APPROVE, "synthetic output test")

    async def test_large_foreground_output_is_recoverable_without_reexecuting_failed_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = await self.application(root)
            try:
                task = await _executing(app, root)
                result = await self.command(app, task.task_id,
                    "import sys; sys.stdout.write('x'*700000+'尾🙂'); sys.stderr.write('e'*20000); sys.exit(3)")
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "PROCESS_EXIT_NON_ZERO")
                self.assertEqual(result.data["exit_code"], 3)
                self.assertTrue(result.truncated)
                self.assertIn("Do not rerun", result.hint)
                self.assertFalse(result.data["stdout"]["retention_truncated"])
                streams = {name: result.data[name]["text"] for name in ("stdout", "stderr")}
                current = result
                page_count = 0
                while any(current.data[name]["has_more"] for name in streams):
                    page_count += 1
                    self.assertLess(page_count, 60)
                    current = await app.kernel.invoke_tool(task.task_id, "turn-read", ToolCall(
                        f"logs-{page_count}", "core.process_logs", {
                            "process_id": result.data["process_id"],
                            **{name + "_cursor": current.data[name]["next_cursor"] for name in streams},
                        }))
                    self.assertTrue(current.ok)
                    for name in streams:
                        self.assertLessEqual(len(current.data[name]["text"].encode()), PROCESS_OUTPUT_PAGE_BYTES)
                        streams[name] += current.data[name]["text"]
                self.assertEqual(streams["stdout"], "x" * 700000 + "尾🙂")
                self.assertEqual(streams["stderr"], "e" * 20000)
                self.assertFalse(current.truncated)
                events = await app.kernel.dependencies.store.read_events(task.task_id)
                self.assertEqual(sum(event.event_type == "process.started" for event in events), 1)
                terminal = next(event for event in events if event.event_type == "process.exited")
                self.assertEqual(terminal.payload["stdout"]["text"], streams["stdout"])

                # Another session cannot retrieve this process through its id.
                other = await _executing(app, root, "other-session-task")
                denied = await app.kernel.invoke_tool(other.task_id, "turn-other", ToolCall(
                    "read-other", "core.process_logs", {"process_id": result.data["process_id"]}))
                self.assertFalse(denied.ok)
                self.assertEqual(denied.error_code, "NOT_FOUND")
            finally:
                await app.registry.stop_all()

    async def test_foreground_output_can_be_read_after_sqlite_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first = await self.application(root, database=database)
            try:
                task = await _executing(first, root)
                result = await self.command(first, task.task_id, "import sys; sys.stdout.write('a'*50000)")
                process_id = result.data["process_id"]
                cursor = result.data["stdout"]["next_cursor"]
            finally:
                await first.registry.stop_all()
            restored = await self.application(root, database=database)
            try:
                page = await restored.kernel.invoke_tool(task.task_id, "turn-restored", ToolCall(
                    "read-restored", "core.process_logs", {"process_id": process_id, "stdout_cursor": cursor}))
                self.assertTrue(page.ok)
                self.assertEqual(page.data["stdout"]["cursor"], cursor)
                self.assertEqual(page.data["stdout"]["text"], "a" * PROCESS_OUTPUT_PAGE_BYTES)
                self.assertTrue(page.data["stdout"]["has_more"])
            finally:
                await restored.registry.stop_all()

    async def test_background_wait_and_log_pages_remain_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = await self.application(root)
            try:
                task = await _executing(app, root)
                receipt = await self.command(app, task.task_id, "import sys; sys.stdout.write('b'*60000)", mode="background")
                process_id = receipt.data["process_id"]
                observation = await app.kernel.invoke_tool(task.task_id, "turn-wait", ToolCall(
                    "wait", "core.process_wait", {"process_id": process_id, "wait_seconds": 1}))
                self.assertTrue(observation.ok)
                self.assertTrue(observation.truncated)
                self.assertEqual(len(observation.data["stdout"]["text"].encode()), PROCESS_OUTPUT_PAGE_BYTES)
                logs = await app.kernel.invoke_tool(task.task_id, "turn-log", ToolCall(
                    "read", "core.process_logs", {"process_id": process_id,
                     "stdout_cursor": observation.data["stdout"]["next_cursor"]}))
                self.assertTrue(logs.ok)
                self.assertEqual(logs.data["stdout"]["cursor"], PROCESS_OUTPUT_PAGE_BYTES)
                self.assertEqual(len(logs.data["stdout"]["text"].encode()), PROCESS_OUTPUT_PAGE_BYTES)
            finally:
                await app.registry.stop_all()


def test_utf8_pages_preserve_characters_and_distinguish_capture_loss():
    text = "界🙂" * 10000
    original = {"text": text, "total_bytes": len(text.encode()) + 50, "truncated": True}
    pages, cursor = [], 0
    while True:
        page = process_output_page(original, cursor)
        pages.append(page["text"])
        assert "\ufffd" not in page["text"]
        assert len(page["text"].encode()) <= PROCESS_OUTPUT_PAGE_BYTES
        assert page["retention_truncated"]
        assert page["truncated"]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert "".join(pages) == text
    assert cursor == len(text.encode())
    with pytest.raises(ValueError, match="UTF-8 character boundary"):
        process_output_page(original, 1)


def test_paged_process_result_fits_when_the_full_latest_result_cannot_be_compacted():
    output = {"text": "<html>" + "x" * 730000 + "</html>", "total_bytes": 730013, "truncated": False}
    original = {"process_id": "synthetic-process", "succeeded": True, "exit_code": 0, "stdout": output,
                "stderr": {"text": "", "total_bytes": 0, "truncated": False}}
    paged = CoreProcessToolProvider._page_output(original)
    call = ToolCall("call", "core.run_command", {"argv": ["synthetic"], "cwd": "."})
    def conversation(data):
        return (
            Message("user", MessageRole.USER, (TextBlock("prior protected input " * 9500),)),
            Message("assistant", MessageRole.ASSISTANT, (ToolCallBlock(call),)),
            Message("tool", MessageRole.TOOL, (ToolResultBlock(ToolResult("call", True, data=data)),)),
        )
    manager, template = ContextWindowManager(), PromptTemplate.default()
    options = dict(tools=(), prompt_template=template, context_window=128000, max_output_tokens=8192)
    with pytest.raises(ContextWindowExceeded):
        manager.prepare(conversation=conversation(original), **options)
    prepared = manager.prepare(conversation=conversation(paged), **options)
    assert prepared.budget.estimated_input_tokens < 119808
    assert any(isinstance(block, ToolResultBlock) for message in prepared.messages for block in message.content)
    assert len(json.dumps(paged)) < 20000
