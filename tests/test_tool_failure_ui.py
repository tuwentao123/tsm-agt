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


def test_successful_command_projects_arguments_and_output_for_the_card():
    requested = _turn_progress_item(RuntimeEvent("request", "task", 1, "tool.requested", {
        "turn_id": "card-turn",
        "call": {"call_id": "card", "name": "core.run_command",
                 "arguments": {"argv": ["grep", "flutter", "Manifest.xml"], "cwd": "."}},
    }))
    assert requested["tool_name"] == "core.run_command"
    assert json.loads(requested["arguments_text"]) == {
        "argv": ["grep", "flutter", "Manifest.xml"], "cwd": ".",
    }
    huge = _turn_progress_item(RuntimeEvent("request", "task", 1, "tool.requested", {
        "turn_id": "card-turn",
        "call": {"call_id": "huge", "name": "core.run_command",
                 "arguments": {"argv": ["x" * 5000]}},
    }))
    assert len(huge["arguments_text"]) == 1200

    completed = _turn_progress_item(RuntimeEvent("done", "task", 2, "tool.completed", {
        "turn_id": "card-turn",
        "result": {
            "call_id": "card", "ok": True,
            "fact_descriptor": {"authority": "managed_process"},
            "data": {"succeeded": True, "exit_code": 0,
                     "stdout": {"text": "日志字节量/天 估算:\n  Credit 179,272,114 行"},
                     "stderr": {"text": ""}},
        },
    }))
    assert completed["output"]["stdout"].startswith("日志字节量")
    assert completed["exit_code"] == 0
    assert "command_failure" not in completed

    read = _turn_progress_item(RuntimeEvent("read", "task", 3, "tool.completed", {
        "turn_id": "card-turn",
        "result": {"call_id": "read", "ok": True,
                   "data": {"succeeded": True, "exit_code": 0, "stdout": {"text": "body"}}},
    }))
    assert "output" not in read
    assert "exit_code" not in read


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


def test_failed_command_projects_redacted_output_excerpts():
    item = _turn_progress_item(RuntimeEvent("evt", "task", 1, "tool.failed", {
        "turn_id": "turn",
        "result": {
            "call_id": "command", "ok": False, "error_code": "PROCESS_EXIT_NON_ZERO",
            "message": "command reached a terminal non-success outcome",
            "fact_descriptor": {"authority": "managed_process"},
            "data": {
                "succeeded": False, "exit_code": 2,
                "stderr": {"text": "x" * 500 + "\ngrep: missing.kt: No such file or directory\npassword=example-secret"},
                "stdout": {"text": "partial results", "has_more": True},
            },
        },
    }))
    assert item["ok"] is False
    assert item["detail"] is None
    failure = item["command_failure"]
    assert failure["exit_code"] == 2
    assert failure["stdout"] == "partial results…"
    assert failure["stderr"].startswith("…")
    assert "No such file or directory" in failure["stderr"]
    assert "password=[REDACTED]" in failure["stderr"]
    assert "example-secret" not in json.dumps(item)
    assert len(failure["stderr"]) <= 401


def test_empty_grep_failure_uses_the_call_from_the_same_turn():
    tool_calls = {}
    _turn_progress_item(RuntimeEvent("request", "task", 1, "tool.requested", {
        "turn_id": "grep-turn",
        "call": {"call_id": "shared", "name": "core.run_command", "arguments": {"argv": ["/usr/bin/grep", "flutter", "Manifest.xml"]}},
    }), tool_calls=tool_calls)
    result = {
        "call_id": "shared", "ok": False, "error_code": "PROCESS_EXIT_NON_ZERO",
        "fact_descriptor": {"authority": "managed_process"},
        "data": {
            "succeeded": False, "exit_code": 1, "output_observed": True,
            "stderr": {"text": "", "total_bytes": 0}, "stdout": {"text": "", "total_bytes": 0},
        },
    }
    grep = _turn_progress_item(RuntimeEvent("first", "task", 2, "tool.failed", {
        "turn_id": "grep-turn", "result": result,
    }), tool_calls=tool_calls)
    other = _turn_progress_item(RuntimeEvent("second", "task", 3, "tool.failed", {
        "turn_id": "other-turn", "result": result,
    }), tool_calls=tool_calls)
    assert grep["command_failure"] == {"exit_code": 1, "output_empty": True, "no_match": True}
    assert other["command_failure"] == {"exit_code": 1, "output_empty": True}


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
