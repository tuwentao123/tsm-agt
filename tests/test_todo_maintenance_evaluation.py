"""The real-model evaluator must not accept a failed or stale check as success."""
from scripts.evaluate_todo_maintenance import measure_trace, permitted
from tests.test_execution_context import plan_execution
from dataclasses import replace

from tsm_agt.ports import ToolEffect, ToolResult


def test_evaluator_accepts_resolved_python_but_not_other_executables():
    import sys
    from pathlib import Path
    from tsm_agt.ports import ToolCall
    for executable in (sys.executable, str(Path(sys.executable).resolve())):
        assert permitted(ToolCall("check", "core.run_command", {
            "argv": [executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"], "cwd": ".",
        }), "implementation")
    assert not permitted(ToolCall("no", "core.run_command", {
        "argv": ["/tmp/python", "-m", "unittest"], "cwd": ".",
    }), "implementation")


def test_trace_requires_a_completed_passing_check_after_the_last_change():
    edit = replace(plan_execution("edit", {}), effect=ToolEffect.MUTATE)
    check = replace(plan_execution("check", {}, name="core.run_command"),
                    result=ToolResult("check", True, {"exit_code": 0, "succeeded": True}))
    rounds = [{"calls": [
        {"call_id": "edit", "name": "core.apply_patch", "arguments": {"path": "numbers.py"}},
        {"call_id": "check", "name": "core.run_command", "arguments": {"argv": ["python", "-m", "unittest"]}},
    ]}]
    assert measure_trace(rounds, (edit, check))["passing_check_after_last_code_change"]
    assert not measure_trace([{"calls": list(reversed(rounds[0]["calls"]))}], (edit, check))["passing_check_after_last_code_change"]
    for data in ({"state": "RUNNING"}, {"exit_code": 1, "succeeded": False}):
        result = replace(check, result=ToolResult("check", True, data))
        assert not measure_trace(rounds, (edit, result))["passing_check_after_last_code_change"]
    # Documentation accuracy does not require another runtime test of unchanged code.
    docs = replace(edit, call=replace(edit.call, call_id="docs"), result=ToolResult("docs", True, {}))
    rounds[0]["calls"].append({"call_id": "docs", "name": "core.apply_patch", "arguments": {"path": "README.md"}})
    assert measure_trace(rounds, (edit, check, docs))["passing_check_after_last_code_change"]


def test_process_start_cannot_justify_completion_before_wait_observes_success():
    edit = replace(plan_execution("edit", {}), effect=ToolEffect.MUTATE)
    check = replace(plan_execution("check", {}, name="core.run_command"),
                    result=ToolResult("check", True, {"state": "RUNNING", "process_id": "p"}))
    wait = replace(plan_execution("wait", {}, name="core.process_wait"),
                   result=ToolResult("wait", True, {"state": "EXITED", "exit_code": 0, "succeeded": True}))
    update = plan_execution("update", {"todos": [{"id": "numbers", "status": "completed"}]}, changed=True)
    calls = [
        {"call_id": "edit", "name": "core.apply_patch", "arguments": {"path": "numbers.py"}},
        {"call_id": "check", "name": "core.run_command", "arguments": {"argv": ["python", "-m", "unittest", "test_numbers"]}},
        {"call_id": "wait", "name": "core.process_wait", "arguments": {"process_id": "p"}},
        {"call_id": "update", "name": "core.todo_list", "arguments": update.call.arguments},
    ]
    safe = measure_trace([{"calls": calls}], (edit, check, wait, update))
    assert safe["passing_check_after_last_code_change"]
    assert safe["premature_completion_marks"] == []
    unsafe = measure_trace([{"calls": [*calls[:2], calls[3], calls[2]]}], (edit, check, wait, update))
    assert unsafe["premature_completion_marks"] == ["numbers"]


def test_full_suite_check_before_a_later_edit_cannot_verify_that_edit():
    edit = replace(plan_execution("edit", {}), effect=ToolEffect.MUTATE)
    check = replace(plan_execution("check", {}), result=ToolResult("check", True, {"exit_code": 0}))
    update = plan_execution("update", {"todos": [{"id": "labels", "status": "completed"}]}, changed=True)
    rounds = [{"calls": [
        {"call_id": "check", "name": "core.run_command", "arguments": {"argv": ["python", "-m", "unittest", "discover"]}},
        {"call_id": "edit", "name": "core.apply_patch", "arguments": {"path": "labels.py"}},
        {"call_id": "update", "name": "core.todo_list", "arguments": update.call.arguments},
    ]}]
    assert measure_trace(rounds, (edit, check, update))["premature_completion_marks"] == ["labels"]
