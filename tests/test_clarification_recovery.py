"""Legacy Runtime-authored outcome gates retire without inventing results."""
import asyncio
import json
from pathlib import Path
import tempfile
import importlib
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from tsm_agt.adapters.mcp_python_sdk_client import create_mcp_sdk_client
from tsm_agt.adapters.mcp_tool_provider import MCPToolProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ApprovalDecision, ToolCommitState, TaskState
from tsm_agt.core.agent_loop import AgentTurnCheckpoint, ToolBatchStatus
from tsm_agt.core.clarification import ClarificationRequest, ClarificationKind, ClarificationChoice
from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.ports import MCPClientManager, RuntimeUnitOfWork, RuntimeEvent
from tsm_agt.sdk import EngineeringAgentClient
from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response
from tests.test_mcp_sdk_integration import _p0_server


def test_legacy_outcome_wait_retires_on_restart_and_normal_input_continues():
    async def run():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / "receipt"
            database = root / "runtime.db"

            async def client(model):
                provider = MCPToolProvider(MCPClientManager(
                    (_p0_server("--receipt", str(receipt)),), create_mcp_sdk_client,
                ))
                app = compose_fixture_application(model_adapter=model,
                    tool_adapters=(provider, CoreReadOnlyToolProvider()),
                    store_adapter=SQLiteRuntimeStore(database))
                instance = EngineeringAgentClient(root, application_factory=lambda: app)
                await instance.start()
                return instance

            first = await client(ScriptedModel([
                tool_response("write", "once", "mcp.p0.write", {}),
                text_response("unknown", "The response was lost."),
            ]))
            try:
                task = await _executing(first.application, root)
                approval = await first.application.kernel.run_agent_turn(task.task_id, "Use fixture")
                before = (await first.application.kernel.get_task(task.task_id)).active_agent_checkpoint
                result = await first.application.kernel.resolve_agent_approval(
                    approval.approval_request_id, ApprovalDecision.APPROVE, "approve synthetic fixture",
                )
                store = first.application.kernel.dependencies.store
                stored = await store.load_task(task.task_id)
                live = await first.application.kernel.get_task(task.task_id)
                execution = next(iter(live.tool_executions.values()))
                # Reconstruct the old persisted format; the current loop must
                # never generate this Runtime question itself.
                now = datetime.now(timezone.utc)
                request = ClarificationRequest("legacy-question", task.task_id, result.turn_id,
                    execution.call, "Confirm the external outcome", (ClarificationChoice("CONFIRM_SUCCEEDED", "Succeeded"),),
                    "legacy Runtime gate", True, ClarificationRequest.hash_resume_token("old-token"),
                    now, now + timedelta(hours=24), ClarificationKind.OUTCOME_RECONCILIATION,
                    execution.execution_id)
                checkpoint = replace(AgentTurnCheckpoint.from_data(before),
                    messages=result.messages, pending_tool_calls=(),
                    pending_user_action={"kind": request.kind.value, "request_id": request.request_id,
                        "execution_id": execution.execution_id})
                if checkpoint.tool_batch:
                    checkpoint = replace(checkpoint, tool_batch=checkpoint.tool_batch.with_pending((), status=ToolBatchStatus.CLOSED))
                if live.state is TaskState.AWAITING_INPUT:
                    live = live.transition(TaskState.EXECUTING)
                legacy = live.await_clarification(request).with_agent_checkpoint(checkpoint.to_data())
                await store.commit(RuntimeUnitOfWork(task.task_id, stored.version, legacy.to_data(), (
                    RuntimeEvent("legacy-event", task.task_id, stored.last_event_sequence + 1,
                        "clarification.requested", {"request_id": request.request_id, "turn_id": result.turn_id, "kind": request.kind.value}),
                )))
            finally:
                await first.close()

            model = ScriptedModel([
                tool_response("inspect", "inspect-receipt", "core.read_file", {"path": "receipt"}),
                text_response("answer", "The receipt contains the write."),
            ])
            restored = await client(model)
            try:
                live = await restored.application.kernel.get_task(task.task_id)
                assert live.state is TaskState.INTERRUPTED
                assert live.pending_clarification is None
                assert not model.requests
                # A normal message, with no confirmation token or special
                # resume command, must continue this same conversation.
                accepted = await restored.submit_session_text(task.session_id, "检查之前操作的状态并继续", command_id="continue")
                assert accepted.result["task"]["task_id"] == task.task_id
                final = await restored.wait_task(task.task_id, timeout=5)
                assert final.assistant_text == "The receipt contains the write."
                assert len(model.requests) == 2
                live = await restored.application.kernel.get_task(task.task_id)
                execution = next(e for e in live.tool_executions.values() if e.call.name == "mcp.p0.write")
                assert execution.state is ToolCommitState.UNKNOWN_OUTCOME
                assert execution.reconciled_outcome is None
                assert receipt.read_text() == "write\n"
                events = await restored.application.kernel.dependencies.store.read_events(task.task_id)
                assert sum(e.event_type == "clarification.retired" for e in events) == 1
                assert not any(e.event_type == "clarification.resolved" for e in events)
                assert not any(e.event_type == "tool.outcome_reconciled" for e in events)
                await restored.cancel(task.task_id, command_id="close-fixture", reason="fixture finished")
                closed = await restored.application.kernel.close_session(task.session_id, "fixture finished")
                assert closed.state.value == "CLOSED"
            finally:
                await restored.close()
    asyncio.run(run())


def test_web_recovery_is_bound_to_pending_request_and_reports_background_errors():
    web = importlib.import_module("tsm_agt.web.app")
    request = Request({"type": "http", "method": "POST", "scheme": "http", "path": "/clarifications",
        "query_string": b"", "server": ("localhost", 8080), "client": ("127.0.0.1", 1234),
        "headers": [(b"host", b"localhost:8080"), (b"origin", b"http://localhost:8080")]})
    pending = SimpleNamespace(request_id="pending", choices=[SimpleNamespace(value="KEEP_BLOCKED")])
    task = SimpleNamespace(pending_clarification=pending)
    calls = []

    async def get_task(task_id):
        return task

    async def answer(*args, **kwargs):
        calls.append((args, kwargs))

    def immediate(coroutine):
        try:
            coroutine.send(None)
        except StopIteration as done:
            return done.value
        raise AssertionError("unexpected await in fixture")

    future = Future()
    submitted = []
    def submit(coroutine):
        submitted.append(coroutine)
        return future

    server = SimpleNamespace(_call=immediate, submit_background=submit,
        _client=SimpleNamespace(application=SimpleNamespace(kernel=SimpleNamespace(get_task=get_task)),
                                answer_pending_clarification=answer))

    async def run():
        with patch.object(web, "runtime_server", server), patch.object(web, "_clarification_submissions", {}):
            payload = {"task_id": "task", "request_id": "stale", "command_id": "click", "selected_choice": "KEEP_BLOCKED"}
            try:
                await web.answer_pending_clarification(payload, request)
            except HTTPException as error:
                assert error.status_code == 409
            else:
                raise AssertionError("stale button accepted")
            assert not submitted
            payload["request_id"] = "pending"
            response = await web.answer_pending_clarification(payload, request)
            assert response.status_code == 202
            assert (await web.clarification_submission("click", request))["status"] == "pending"
            await submitted[0]
            future.set_exception(ValueError("private provider detail"))
            failed = await web.clarification_submission("click", request)
            assert failed["status"] == "failed"
            assert "private" not in json.dumps(failed)
            assert calls[0][0][:2] == ("task", "pending")
            assert calls[0][1]["selected_choice"] == "KEEP_BLOCKED"
    asyncio.run(run())
