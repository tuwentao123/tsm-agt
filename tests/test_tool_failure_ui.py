"""Tool failures and post-call reconciliation must reach the chat UI."""
import asyncio
import json
from pathlib import Path
import tempfile

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.ports import RuntimeEvent
from tsm_agt.sdk import EngineeringAgentClient
from tsm_agt.sdk.ui_events import _TURN_PROGRESS_EVENT_TYPES, _turn_progress_item, subscribe_task_ui
from tests.test_conversation_loop import text_response
from tests.test_evented_ui import BlockingModel


def test_tool_failure_projects_bounded_reason_without_process_output():
    item = _turn_progress_item(RuntimeEvent("evt", "task", 1, "tool.failed", {
        "turn_id": "turn", "result": {
            "call_id": "push", "ok": False, "error_code": "UNKNOWN_OUTCOME",
            "message": "tool exceeded 62 second deadline",
            "data": {"stdout": "private output", "stderr": "private stderr"},
        },
    }))
    assert item["detail"] == "tool exceeded 62 second deadline"
    assert item["error_code"] == "UNKNOWN_OUTCOME"
    assert "private" not in json.dumps(item)
    bounded = _turn_progress_item(RuntimeEvent("evt", "task", 2, "tool.failed", {
        "turn_id": "turn", "result": {"ok": False, "message": "x" * 1000},
    }))
    assert len(bounded["detail"]) <= 200


def test_reconciliation_is_replayed_as_an_explanatory_waiting_event():
    assert "clarification.requested" in _TURN_PROGRESS_EVENT_TYPES
    item = _turn_progress_item(RuntimeEvent("evt", "task", 3, "clarification.requested", {
        "turn_id": "turn", "request_id": "request", "kind": "OUTCOME_RECONCILIATION",
        "question_hash": "opaque", "choice_count": 3,
    }))
    assert item["kind"] == "waiting"
    assert item["waiting_kind"] == "outcome_reconciliation"
    assert "暂停" in item["action"] and "核对" in item["action"]


def test_clarification_only_commit_wakes_stream_and_publishes_snapshot():
    async def run():
        with tempfile.TemporaryDirectory() as directory:
            model = BlockingModel([text_response("answer", "Done")])
            app = compose_fixture_application(model_adapter=model, tool_adapters=())
            client = EngineeringAgentClient(Path(directory), application_factory=lambda: app)
            await client.start()
            stream = None
            try:
                started = await client.submit_task("inspect", command_id="start")
                await model.entered.wait()
                stream = subscribe_task_ui(client, started.task_id, heartbeat=0.1)
                async for packet in stream:
                    if packet["type"] == "task.snapshot":
                        break
                turn_id = packet["turn_id"]
                await app.kernel._append_events(started.task_id, (("clarification.requested", {
                    "turn_id": turn_id, "request_id": "request", "kind": "OUTCOME_RECONCILIATION",
                }),))
                progress = await asyncio.wait_for(anext(stream), 1)
                assert progress["progress"]["waiting_kind"] == "outcome_reconciliation"
                snapshot = await asyncio.wait_for(anext(stream), 1)
                assert snapshot["type"] == "task.snapshot"
                assert snapshot["cursor"] >= progress["sequence"]
            finally:
                if stream is not None:
                    await stream.aclose()
                model.release.set()
                if "started" in locals():
                    await client.wait_task(started.task_id, timeout=5)
                await client.close()
    asyncio.run(run())
