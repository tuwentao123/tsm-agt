"""A resumed approval/question still owns the active conversation runner."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ApprovalDecision, TaskState
from tsm_agt.ports import (
    FinishReason, Message, MessageRole, ModelResponse, ModelUsage,
    RuntimeStorePort, TextBlock, ToolCall, ToolCallBlock, ToolResultBlock,
)
from tsm_agt.sdk import EngineeringAgentClient
from tests.test_sdk_runtime import ApprovalModel, ApprovalTool


class ResumedModel(ApprovalModel):
    def __init__(self, question=False):
        super().__init__()
        self.question = question
        self.resumed = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_done = asyncio.Event()
        self.new_entered = asyncio.Event()
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        latest_user = next(m.text for m in reversed(request.messages) if m.role is MessageRole.USER)
        if latest_user == "new context":
            self.new_entered.set()
            return ModelResponse(
                Message("new-final", MessageRole.ASSISTANT, (TextBlock("new turn completed"),)),
                FinishReason.STOP, ModelUsage(1, 1),
            )
        if any(isinstance(b, ToolResultBlock) for m in request.messages for b in m.content):
            self.resumed.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.set()
                await self.cleanup_release.wait()
                self.cleanup_done.set()
                raise
        if self.question:
            return ModelResponse(
                Message("question", MessageRole.ASSISTANT, (ToolCallBlock(ToolCall(
                    "question", "core.request_input", {"question": "Which option?", "reason": "fixture"},
                )),)), FinishReason.TOOL_CALL, ModelUsage(1, 1),
            )
        return await super().complete(request)


class ResumedTurnOwnershipTest(unittest.IsolatedAsyncioTestCase):
    async def test_completed_runner_waiting_for_cleanup_does_not_drop_next_input(self):
        from tests.test_conversation_loop import ScriptedModel, text_response

        with tempfile.TemporaryDirectory() as directory:
            model = ScriptedModel([text_response("first", "one"), text_response("next", "two")])
            app = compose_fixture_application(model_adapter=model, tool_adapters=())
            async with EngineeringAgentClient(Path(directory), application_factory=lambda: app) as client:
                first = await client.submit_user_input("conv", "one", input_id="first")
                task_id = first.result["task"]["task_id"]
                await client.wait_task(task_id, timeout=3)
                # Hold the real scheduling window where the Future is done but
                # its identity-safe registry callback has not removed it yet.
                completed = asyncio.create_task(client.get_task_result(task_id))
                await completed
                client._run_tasks[task_id] = completed
                await client.submit_user_input("conv", "two", input_id="next")
                result = await client.wait_task(task_id, timeout=3)
                self.assertEqual(result.assistant_text, "two")
                self.assertEqual(len(model.requests), 2)

    async def test_new_input_cancels_and_joins_approval_or_question_resume(self):
        for mode in ("approval", "pending_question", "token_question"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                model = ResumedModel(question=mode != "approval")
                database = Path(directory) / "runtime.db"
                app = compose_fixture_application(
                    model_adapter=model, tool_adapters=(ApprovalTool(),),
                    store_adapter=SQLiteRuntimeStore(database),
                )
                client = EngineeringAgentClient(Path(directory), application_factory=lambda: app, poll_interval=0.001)
                resumed = duplicate = None
                await client.start()
                try:
                    accepted = await client.submit_user_input("conv", "first", input_id="first", workspace=Path(directory))
                    task_id = accepted.result["task"]["task_id"]
                    waiting = await client.wait_task(task_id, timeout=3)
                    if mode == "approval":
                        resumed = asyncio.create_task(client.resolve_approval(
                            waiting.approval["request_id"], ApprovalDecision.APPROVE,
                            "approve fixture", command_id="resume",
                        ))
                        duplicate = asyncio.create_task(client.resolve_approval(
                            waiting.approval["request_id"], ApprovalDecision.APPROVE,
                            "duplicate reply", command_id="duplicate",
                        ))
                    elif mode == "pending_question":
                        resumed = asyncio.create_task(client.answer_pending_clarification(
                            task_id, waiting.clarification["request_id"], "option one", command_id="resume",
                        ))
                    else:
                        resumed = asyncio.create_task(client.answer_clarification(
                            waiting.clarification["request_id"], waiting.clarification["resume_token"],
                            "option one", command_id="resume",
                        ))
                    await asyncio.wait_for(model.resumed.wait(), 3)
                    redirected = await asyncio.wait_for(client.submit_user_input(
                        "conv", "new context", input_id="second", workspace=Path(directory),
                    ), 3)
                    self.assertEqual(redirected.result["routed_task_id"], task_id)
                    await asyncio.wait_for(model.cancelled.wait(), 1)
                    self.assertFalse(model.new_entered.is_set())
                    model.cleanup_release.set()
                    final = await client.wait_task(task_id, timeout=3)
                    self.assertTrue(model.cleanup_done.is_set())
                    self.assertTrue(model.new_entered.is_set())
                    self.assertEqual(final.assistant_text, "new turn completed")
                    self.assertEqual((await app.kernel.get_task(task_id)).state, TaskState.AWAITING_INPUT)
                    with self.assertRaises(asyncio.CancelledError):
                        await resumed
                    with sqlite3.connect(database) as connection:
                        status, data = connection.execute(
                            "select status,result_json from runtime_commands where command_id='resume'"
                        ).fetchone()
                    self.assertEqual(status, "failed")
                    self.assertIn("RUNTIME_COMMAND_CANCELLED", data)
                    events = await app.registry.require(RuntimeStorePort).read_events(task_id)
                    starts = [e for e in events if e.event_type == "turn.started"]
                    self.assertEqual(len(starts), 2)
                    self.assertFalse(any(
                        e.event_type == "checkpoint.saved" and e.payload.get("turn_id") == starts[0].payload["turn_id"]
                        for e in events if e.sequence > starts[1].sequence
                    ))
                    self.assertFalse(any(e.event_type == "turn.failed" for e in events))
                finally:
                    model.cleanup_release.set()
                    if resumed is not None:
                        resumed.cancel()
                        await asyncio.gather(resumed, return_exceptions=True)
                    if duplicate is not None:
                        duplicate.cancel()
                        await asyncio.gather(duplicate, return_exceptions=True)
                    await client.close()

    async def test_read_failure_returns_to_model_without_killing_turn(self):
        from tests.test_conversation_loop import ScriptedModel, tool_response, text_response

        with tempfile.TemporaryDirectory() as directory:
            model = ScriptedModel([
                tool_response("list", "missing", "core.list_files", {"path": "missing"}),
                text_response("explain", "Directory was not found."),
            ])
            app = compose_fixture_application(model_adapter=model, tool_adapters=(CoreReadOnlyToolProvider(),))
            async with EngineeringAgentClient(Path(directory), application_factory=lambda: app) as client:
                accepted = await client.submit_task("check directory", command_id="read-failure")
                result = await client.wait_task(accepted.task_id, timeout=3)
                self.assertNotEqual(result.state, "FAILED")
                failures = [b.result for m in model.requests[-1].messages for b in m.content if isinstance(b, ToolResultBlock)]
                self.assertEqual(failures[-1].error_code, "NOT_FOUND")


def test_cancelled_background_resume_does_not_log_callback_error(caplog):
    from concurrent.futures import Future
    from unittest.mock import Mock, patch
    from tsm_agt.local_api import LocalEventApiServer

    server = object.__new__(LocalEventApiServer)
    server._loop = Mock()
    future = Future()
    work = asyncio.sleep(0)
    try:
        with patch("asyncio.run_coroutine_threadsafe", return_value=future):
            server.submit_background(work)
        future.cancel()
        self_errors = [record for record in caplog.records if record.levelname == "ERROR"]
        assert not self_errors
    finally:
        work.close()
