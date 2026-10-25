#!/usr/bin/env python3
"""Real-model Todo checks in disposable workspaces; never part of pytest.

Compares the current rules/state with a saved pre-change prompt snapshot when
--baseline-snapshot is supplied. Responses, tools, writes and checks are real.
Only authored fixture files are sent to the configured model. No credentials,
request headers, or unrelated workspace contents are written to the report.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.adapters.builtin.core_tools import CoreReadOnlyToolProvider
from tsm_agt.adapters.builtin.workspace_tools import CoreWorkspaceMutationToolProvider
from tsm_agt.adapters.local_sandbox import LocalWorkspaceSandbox
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.composition import _physical_model_provider, _platform_workspace_path
from tsm_agt.bootstrap.model_configuration import load_model_configuration
from tsm_agt.core import AgentTurnResult, AgentTurnSuspended, ApprovalDecision, TaskState
from tsm_agt.ports import Message, MessageRole, ModelStreamCompleted, TextBlock, ToolCallBlock, ToolResultBlock, ToolEffect

CASES = ("implementation", "continuation", "analysis", "blocked", "small_change",
         "stale_plan", "multi_turn", "scope_change", "verification_failure", "revision_conflict")
FIXED_NUMBERS = "def total(values):\n    return sum(value for value in values if value is not None)\n"
FIXED_LABELS = "def normalize(value):\n    return value.strip().lower()\n"
FILES = {
    "numbers.py": "def total(values):\n    return sum(values)\n",
    "labels.py": "def normalize(value):\n    return value\n",
    "README.md": "# Helpers\n\nUtilities.\n",
    "test_numbers.py": (
        "import unittest\nfrom numbers import total\n\nclass NumbersTest(unittest.TestCase):\n"
        "    def test_total(self):\n        self.assertEqual(total([2, None, -1, 0]), 1)\n"
        "    def test_empty(self):\n        self.assertEqual(total([None]), 0)\n"
    ),
    "test_labels.py": (
        "import unittest\nfrom labels import normalize\n\nclass LabelsTest(unittest.TestCase):\n"
        "    def test_normalize(self):\n        self.assertEqual(normalize(' A B '), 'a b')\n"
        "    def test_empty(self):\n        self.assertEqual(normalize('  '), '')\n"
    ),
}
TASK = (
    "请用 Todo 推进这三项工作：1. numbers.total 忽略 None，保留零和负数，空输入返回 0；"
    "2. labels.normalize 去除首尾空白并转小写，保留内部空格；"
    "3. README.md 说明这两个函数的行为，并给出准确的输入输出示例。"
    "请修改实际文件，保留现有测试，选择适当验证。"
)


class ObservedModel:
    def __init__(self, inner, baseline=None):
        self.inner, self.baseline = inner, baseline
        self.descriptor, self.capabilities = inner.descriptor, inner.capabilities
        self.rounds = []
        self.state_views = []
        self.requests = []
        self.active_turn_id = None
        self.on_response = None
        self.log_context = {}

    async def start(self, context):
        await self.inner.start(context)

    async def stop(self, deadline):
        await self.inner.stop(deadline)

    async def health(self):
        return await self.inner.health()

    def _request(self, request):
        self.active_turn_id = request.turn_id
        self.requests.append({"turn_id": request.turn_id, "reasoning_effort": request.reasoning_effort,
                              "tools": [tool.name for tool in request.tools]})
        messages, views = [], []
        for message in request.messages:
            if message.message_id.startswith("execution-context-"):
                body = json.loads(message.text)
                if self.baseline:
                    body["schema_version"] = 1
                    plan = body.get("plan_record", {})
                    for key in ("active_item_id", "next_pending_item_id", "status_counts", "maintenance_this_turn"):
                        plan.pop(key, None)
                    message = replace(message, content=(TextBlock(json.dumps(body, ensure_ascii=False)),))
                views.append(body)
            if self.baseline:
                message = replace(message, content=tuple(
                    ToolResultBlock(replace(block.result, meta={
                        k: v for k, v in block.result.meta.items() if k != "plan_changed"
                    })) if isinstance(block, ToolResultBlock) else block
                    for block in message.content
                ))
            messages.append(message)
        tools = []
        for tool in request.tools:
            if self.baseline and tool.name == "core.todo_list":
                params = deepcopy(tool.parameters)
                params["properties"]["todos"]["items"]["required"] = ["id", "content", "status"]
                params["properties"]["todos"]["description"] = "Task items to write."
                params["properties"]["expected_revision"]["description"] = (
                    "Current revision returned by this tool. Required when todos is present."
                )
                tool = replace(tool, description=self.baseline["todo_description"], parameters=params)
            tools.append(tool)
        self.state_views.append(views)
        return replace(request, messages=tuple(messages), tools=tuple(tools))

    def _response(self, response):
        row = {"turn_id": self.active_turn_id, "text": response.message.text, "calls": [
            {"call_id": block.call.call_id, "name": block.call.name,
             "arguments": dict(block.call.arguments)}
            for block in response.message.content if isinstance(block, ToolCallBlock)
        ]}
        self.rounds.append(row)
        print(json.dumps({**self.log_context, "round": len(self.rounds), "tools": [c["name"] for c in row["calls"]]}, ensure_ascii=False), flush=True)

    async def complete(self, request):
        response = await self.inner.complete(self._request(request))
        self._response(response)
        if self.on_response:
            await self.on_response(response)
        return response

    async def stream_complete(self, request):
        async for event in self.inner.stream_complete(self._request(request)):
            if isinstance(event, ModelStreamCompleted):
                self._response(event.response)
                if self.on_response:
                    await self.on_response(event.response)
            yield event


def permitted(call, case):
    allowed = {"numbers.py", "labels.py", "README.md"}
    if case == "analysis":
        return False
    if case == "blocked":
        allowed.remove("labels.py")
    elif case in {"small_change", "scope_change"}:
        allowed = {"README.md"}
    if call.name == "core.apply_patch_v4a":
        paths = re.findall(r"^\*\*\* (?:Update|Add) File: (.+)$", call.arguments.get("patch", ""), re.M)
        return bool(paths) and set(paths) <= allowed
    if call.name == "core.apply_patch":
        return call.arguments.get("path") in allowed
    if call.name == "core.apply_patches":
        edits = call.arguments.get("patches", [])
        return bool(edits) and all(edit.get("path") in allowed for edit in edits)
    if call.name == "core.run_command":
        argv = call.arguments.get("argv", [])
        # Runtime resolves executable aliases before asking for approval.
        # Match only known Python executables, not arbitrary absolute programs.
        python_paths = {sys.executable}
        python_paths.update(path for name in ("python", "python3") if (path := shutil.which(name)))
        python_paths.update(str(Path(path).resolve()) for path in tuple(python_paths))
        return (call.arguments.get("cwd") == "." and len(argv) >= 3
                and argv[0] in python_paths | {"python", "python3"}
                and argv[1:3] == ["-m", "unittest"]
                and not any("/" in arg or ".." in arg for arg in argv[3:]))
    return False


def _terminal_pass(row):
    return (row["ok"] and type(row["exit_code"]) is int and row["exit_code"] == 0
            and row["succeeded"] is not False and row.get("state") not in {"RUNNING", "ORPHANED"})


def measure_trace(rounds, executions):
    """Evidence only: a passing command does not prove whole-task correctness."""
    by_call = {row.call.call_id: row for row in executions}
    updates, reads, completions, checks, mutations = [], 0, [], [], []
    position, code_mutation_positions = 0, []
    mutation_positions, process_checks = {}, {}
    premature, completed = [], set()
    for index, turn in enumerate(rounds, 1):
        for call in turn["calls"]:
            position += 1
            row = by_call.get(call["call_id"])
            if row is None or row.result is None:
                continue
            result = row.result
            if call["name"] == "core.todo_list":
                if call["arguments"].get("todos") is None:
                    reads += 1
                else:
                    updates.append({"round": index, "position": position, "ok": result.ok,
                                    "changed": result.meta.get("plan_changed"),
                                    "revision": (result.data or {}).get("revision"),
                                    "error_code": result.error_code})
                    if result.ok:
                        for item in call["arguments"]["todos"]:
                            if item.get("status") != "completed" or item["id"] in completed:
                                continue
                            completions.append({"round": index, "position": position, "id": item["id"]})
                            completed.add(item["id"])
                            last_edit = mutation_positions.get(item["id"] + ".py", 0)
                            if last_edit and not any(_terminal_pass(check) and check["position"] > last_edit
                                    and ("discover" in check["argv"] or "test_" + item["id"] in check["argv"])
                                    for check in checks):
                                premature.append(item["id"])
            if call["name"] == "core.run_command":
                checks.append({"round": index, "ok": result.ok, "call_id": call["call_id"],
                               "position": position,
                               "argv": call["arguments"].get("argv"),
                               "exit_code": (result.data or {}).get("exit_code"),
                               "succeeded": (result.data or {}).get("succeeded"),
                               "state": (result.data or {}).get("state")})
                if result.ok and (result.data or {}).get("process_id"):
                    process_checks[result.data["process_id"]] = checks[-1]
            elif call["name"] in {"core.process_wait", "core.process_status"}:
                check = process_checks.get(call["arguments"].get("process_id"))
                if check is not None:
                    check.update(ok=result.ok, exit_code=(result.data or {}).get("exit_code"),
                                 succeeded=(result.data or {}).get("succeeded"),
                                 state=(result.data or {}).get("state"), observed_position=position)
            if row.effect is ToolEffect.MUTATE and result.ok:
                mutations.append(index)
                args = call["arguments"]
                paths = [str(args.get("path", ""))]
                paths += [str(edit.get("path", "")) for edit in args.get("patches", [])]
                paths += re.findall(r"^\*\*\* (?:Update|Add) File: (.+)$", args.get("patch", ""), re.M)
                if any(path.endswith(".py") for path in paths):
                    code_mutation_positions.append(position)
                for path in paths:
                    mutation_positions[path] = position
    last_mutation = max(code_mutation_positions, default=0)
    return {"todo_writes": updates, "todo_read_count": reads,
            "completion_marks": completions, "checks": checks,
            "mutation_rounds": mutations,
            "premature_completion_marks": premature,
            "noop_write_count": sum(row["ok"] and row["changed"] is False for row in updates),
            "passing_check_after_last_code_change": any(
                _terminal_pass(row) and row["position"] > last_mutation for row in checks
            ),
            "updates_before_final_tool_round": sum(
                row["ok"] and row["changed"] is not False and row["round"] < max(
                    (i for i, round in enumerate(rounds, 1) if round["calls"]), default=0
                ) for row in updates
            )}


async def run_case(args, cfg, case, mode, sample):
    baseline = json.loads(args.baseline_snapshot.read_text()) if mode == "baseline" and args.baseline_snapshot else None
    model = ObservedModel(_physical_model_provider(
        base_url=cfg.base_url, model=cfg.model, api_key=cfg.api_key, protocol=cfg.protocol,
        anthropic_version=cfg.anthropic_version, timeout_seconds=cfg.timeout_seconds,
        output_token_parameter=cfg.effective_output_token_parameter,
        strict_tool_schema=cfg.strict_tool_schema, streaming=cfg.streaming,
        open_api_mode=cfg.open_api_mode, reasoning_effort=cfg.reasoning_effort,
    ), baseline)
    model.log_context = {"case": case, "mode": mode, "sample": sample}
    paths = _platform_workspace_path()
    app = compose_fixture_application(
        model_adapter=model, enable_working_memory=True,
        tool_adapters=(CoreReadOnlyToolProvider(), CoreWorkspaceMutationToolProvider(), CoreProcessToolProvider()),
        workspace_path_adapter=paths, sandbox_adapter=LocalWorkspaceSandbox(paths),
    )
    if baseline:
        template = app.kernel.dependencies.prompt_template
        lines = baseline["coordination"].split("<!-- prompt: task-plan-guidance -->", 1)[1].strip("\n").split("\n")
        plan_text = lines[0]
        for previous, line in zip(lines, lines[1:]):
            separator = "\n" if not previous or not line or previous.startswith("# ") or line.startswith("# ") else " "
            plan_text += separator + line
        template = replace(template, static_segments=tuple(
            replace(segment, content=plan_text, version="4.0")
            if segment.segment_id == "task-plan-guidance" else segment
            for segment in template.static_segments
        ))
        app.kernel._dependencies = replace(app.kernel.dependencies, prompt_template=template)
        if template.manifest().manifest_hash != baseline["manifest_hash"]:
            raise ValueError("baseline manifest does not match the saved pre-change prompt")
    report = {"case": case, "mode": mode, "sample": sample, "model": cfg.model,
              "reasoning_effort": cfg.reasoning_effort, "completion_review": "off",
              "manifest_hash": app.kernel.dependencies.prompt_template.manifest().manifest_hash,
              "history_source": "authored_fixture", "valid_run": False}
    await app.registry.start_all()
    try:
        with tempfile.TemporaryDirectory(prefix="todo-live-") as directory:
            root = Path(directory).resolve()
            files = dict(FILES)
            if case in {"continuation", "analysis", "small_change", "stale_plan"}:
                files["numbers.py"] = FIXED_NUMBERS
            if case in {"small_change", "stale_plan"}:
                files["labels.py"] = FIXED_LABELS
            for name, content in files.items():
                (root / name).write_text(content)
            task = await app.kernel.create_task(TASK, root, "todo-live")
            for state in (TaskState.INTAKE, TaskState.RESOLVING_PROJECT, TaskState.SELECTING_EXTENSIONS,
                          TaskState.ROUTING, TaskState.EXECUTING):
                task = await app.kernel.transition_task(task.task_id, state, state.value)
            await app.kernel.update_session_reasoning_effort(task.session_id, cfg.reasoning_effort)
            if case != "implementation":
                plan = [
                    {"step_id": "numbers", "description": "实现 total 忽略 None", "status": "COMPLETED" if case in {"continuation", "analysis", "small_change", "stale_plan"} else "IN_PROGRESS", "completion_criteria": "numbers 测试通过"},
                    {"step_id": "labels", "description": "实现 normalize 去空白转小写", "status": "COMPLETED" if case == "small_change" else "IN_PROGRESS" if case in {"continuation", "stale_plan"} else "PENDING", "completion_criteria": "labels 测试通过"},
                    {"step_id": "docs", "description": "README 说明两个函数并给示例", "status": "PENDING", "completion_criteria": "说明与实际行为一致"},
                ]
                if case == "stale_plan":
                    plan = [{"step_id": f"setup-{i}", "description": f"已确认的前置事项 {i}",
                             "status": "COMPLETED", "completion_criteria": "前置事项已确认"}
                            for i in range(4)] + plan
                initial_memory = {key: [] for key in (
                    "constraints", "facts", "decisions", "hypotheses", "open_questions",
                    "completed_work", "remaining_work", "evidence",
                )}
                await app.kernel.update_working_memory(
                    task.task_id, 1, {**initial_memory, "plan": plan}, "seed-plan", "authored-fixture",
                )
                unit = await app.kernel._session_task_result_unit(
                    task, "fixture-prior", Message("fixture-user", MessageRole.USER, (TextBlock(TASK),)),
                    Message("fixture-assistant", MessageRole.ASSISTANT, (TextBlock(
                        "已记录清单。" + ("两个函数均已实现，README 的说明和示例尚未完成。" if case in {"small_change", "stale_plan"}
                            else "total 已实现并通过测试；normalize 和 README 尚未完成。" if case in {"continuation", "analysis"}
                            else "实现与验证尚未完成。")
                    ),)), assistant_replay_eligible=True,
                )
                await app.kernel.dependencies.store.commit_session(unit)
            prompt = TASK if case == "implementation" else "继续完成前面的工作。"
            if case == "analysis":
                prompt = "本轮只分析前面需求还需要改哪里，不要修改任何文件，也不要运行命令。"
            elif case == "blocked":
                prompt = "继续前面的工作，但只允许修改 numbers.py 和 README.md，禁止修改 labels.py。尽可能完成允许的部分，并说明剩余限制。"
            elif case == "small_change":
                prompt = "继续，只剩 README 补两个函数的行为说明和准确使用示例这个小改动，代码不用再改。"
            elif case == "stale_plan":
                prompt = "继续"
            elif case == "scope_change":
                prompt = "本轮先不继续函数实现，只把 README 标题改为 Helper Library，其他内容不要改。后续实现等我另行安排。"
            elif case == "verification_failure":
                prompt = "继续完成清单。先运行现有测试确认失败，再实施修复，保留测试文件。"
            if case not in {"analysis", "scope_change"}:
                prompt += f" 现有验证命令可用：{sys.executable} -m unittest discover -s . -p 'test_*.py'；也可以单独运行 test_numbers 或 test_labels。"
            if case == "revision_conflict":
                async def race_once(response):
                    if report.get("revision_conflict_injected"):
                        return
                    if any(isinstance(b, ToolCallBlock) and b.call.name == "core.todo_list"
                           and b.call.arguments.get("todos") is not None for b in response.message.content):
                        current = await app.kernel.get_working_memory(task.task_id)
                        state = current.state_data()
                        state["facts"].append("Independent evaluator observed the workspace.")
                        await app.kernel.update_working_memory(task.task_id, current.revision, state,
                            "external-writer", "authored-evaluation-race")
                        report["revision_conflict_injected"] = True
                model.on_response = race_once

            async def execute(text, scope=case):
                result = await app.kernel.run_agent_turn(
                    task.task_id, text, max_model_calls=22, max_tool_calls=70, max_output_tokens=8192,
                )
                while isinstance(result, AgentTurnSuspended):
                    pending = (await app.kernel.get_task(task.task_id)).pending_approval
                    if pending is None or not permitted(pending.call, scope):
                        report["unexpected_approval"] = True
                        report["unexpected_approval_call"] = pending.call.to_data() if pending else None
                        return result
                    result = await app.kernel.resolve_agent_approval(
                        result.approval_request_id, ApprovalDecision.APPROVE,
                        "Authorized disposable Todo evaluation",
                    )
                return result
            result = None
            try:
                async with asyncio.timeout(args.timeout):
                    if case == "multi_turn":
                        first = await execute("本轮只实现并验证 numbers.total；labels 和 README 保留待办，等我下一轮继续。"
                                              + f" 验证命令：{sys.executable} -m unittest test_numbers")
                        report["first_turn_valid"] = isinstance(first, AgentTurnResult)
                        report["plan_after_first_turn"] = [r.to_data() for r in (await app.kernel.get_working_memory(task.task_id)).plan]
                        report["changed_files_after_first_turn"] = [name for name, content in files.items()
                                                                  if (root / name).read_text() != content]
                        if not report["first_turn_valid"]:
                            raise RuntimeError("first evaluation turn did not complete")
                    result = await execute(prompt)
            except Exception as error:
                # Structured classification only; never persist raw exception bodies.
                report["error_type"] = type(error).__name__
                if hasattr(error, "reason_code"):
                    report["error_reason_code"] = error.reason_code
                if hasattr(error, "category"):
                    report["error_category"] = error.category.value
            final = await app.kernel.get_task(task.task_id)
            memory = await app.kernel.get_working_memory(task.task_id)
            report["plan"] = [row.to_data() for row in memory.plan]
            report["rounds"], report["state_views"] = model.rounds, model.state_views
            report["request_metadata"] = model.requests
            report["real_turn_count"] = len({row["turn_id"] for row in model.rounds})
            report["history_source"] = "real_model_turns" if case == "multi_turn" else "authored_fixture"
            report["trace"] = measure_trace(model.rounds, final.tool_executions.values())
            report["answer"] = result.assistant_message.text if isinstance(result, AgentTurnResult) else None
            report["changed_files"] = [name for name, content in files.items() if (root / name).read_text() != content]
            report["final_files"] = {name: (root / name).read_text() for name in files}
            checks = {}
            for module in ("test_numbers", "test_labels"):
                checked = await asyncio.to_thread(subprocess.run,
                    [sys.executable, "-I", "-m", "unittest", "discover", "-s", str(root), "-p", module + ".py"],
                    cwd=root, capture_output=True, text=True, timeout=30)
                checks[module] = {"passed": checked.returncode == 0, "output": checked.stderr}
            report["independent_checks"] = checks
            report["valid_run"] = (isinstance(result, AgentTurnResult)
                                   and not report.get("error_type") and not report.get("unexpected_approval"))
            report["open_todo_ids"] = [row.step_id for row in memory.plan if row.status.value in {"PENDING", "IN_PROGRESS"}]
            report["protected_tests_unchanged"] = all(report["final_files"][name] == files[name] for name in ("test_numbers.py", "test_labels.py"))
            report["behavior_pass"] = report["valid_run"] and report["protected_tests_unchanged"]
            if case == "analysis":
                report["behavior_pass"] &= (not report["changed_files"] and not report["trace"]["checks"]
                    and {row.step_id: row.status.value for row in memory.plan}
                    == {row["step_id"]: row["status"] for row in plan})
            elif case == "scope_change":
                report["behavior_pass"] &= (report["changed_files"] == ["README.md"]
                    and "Helper Library" in report["final_files"]["README.md"]
                    and all(row.status.value != "COMPLETED" for row in memory.plan)
                    and not report["trace"]["checks"])
            else:
                report["behavior_pass"] &= (checks["test_numbers"]["passed"]
                    and (case in {"small_change", "stale_plan"} or report["trace"]["passing_check_after_last_code_change"]))
                if case == "blocked":
                    report["behavior_pass"] &= (report["final_files"]["labels.py"] == files["labels.py"]
                                                and "README.md" in report["changed_files"]
                                                and any(row.status.value == "BLOCKED" for row in memory.plan))
                else:
                    report["behavior_pass"] &= (checks["test_labels"]["passed"] and "README.md" in report["changed_files"]
                                                and bool(memory.plan)
                                                and all(row.status.value == "COMPLETED" for row in memory.plan))
                report["behavior_pass"] &= not report["trace"]["premature_completion_marks"]
                if case == "small_change":
                    report["behavior_pass"] &= report["changed_files"] == ["README.md"]
                if case == "multi_turn":
                    first_plan = {r["step_id"]: r["status"] for r in report.get("plan_after_first_turn", [])}
                    report["behavior_pass"] &= (report["real_turn_count"] == 2 and first_plan.get("numbers") == "COMPLETED"
                        and first_plan.get("labels") != "COMPLETED" and first_plan.get("docs") != "COMPLETED"
                        and report.get("changed_files_after_first_turn") == ["numbers.py"])
                if case == "verification_failure":
                    report["behavior_pass"] &= any(row["exit_code"] not in {0, None} for row in report["trace"]["checks"])
                if case == "revision_conflict":
                    report["behavior_pass"] &= (report.get("revision_conflict_injected", False)
                        and any(not row["ok"] for row in report["trace"]["todo_writes"]))
                report["documentation_markers_present"] = all(
                    marker in report["final_files"]["README.md"]
                    for marker in (("total", "normalize", "None") if case == "blocked"
                                   else ("total", "normalize", "None", "a b")))
                report["behavior_pass"] &= report["documentation_markers_present"]
            report["early_stop_observed"] = (
                report["valid_run"] and case not in {"analysis", "scope_change", "blocked"}
                and not report["behavior_pass"]
            )
            destination = args.report_dir / f"{case}-{mode}-{sample}.json"
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({"report": str(destination), "valid_run": report["valid_run"], "behavior_pass": report["behavior_pass"]}, ensure_ascii=False), flush=True)
            return report
    finally:
        await app.registry.stop_all()


async def main(args):
    cfg = load_model_configuration(args.env_file, os.environ)
    if args.reasoning_effort:
        cfg = replace(cfg, reasoning_effort=args.reasoning_effort)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    import tsm_agt
    from tsm_agt.core import PromptTemplate
    source_root = Path(tsm_agt.__file__).parent
    provenance = {"label": args.label, "model": cfg.model, "reasoning_effort": cfg.reasoning_effort,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_root": str(source_root), "manifest_hash": PromptTemplate.default().manifest().manifest_hash,
        "source_hashes": {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
                          for name in ("core/conversation_loop/execution_context.py",
                                       "core/conversation_loop/prompt.py", "adapters/builtin/todo_tools.py")}}
    (args.report_dir / (args.label + "-provenance.json")).write_text(json.dumps(provenance, indent=2))
    print(json.dumps(provenance), flush=True)
    semaphore = asyncio.Semaphore(args.parallel)
    async def run(case, mode, sample):
        async with semaphore:
            return await run_case(args, cfg, case, mode, sample)
    reports = await asyncio.gather(*(run(case, mode, sample)
        for case in args.case or CASES for sample in range(1, args.repeats + 1)
        for mode in (("baseline", "candidate") if args.baseline_snapshot else (args.label,))))
    summary = {"label": args.label, "runs": len(reports),
        "valid_runs": sum(r["valid_run"] for r in reports),
        "behavior_passes": sum(r["behavior_pass"] for r in reports),
        "results": [{k: r.get(k) for k in ("case", "mode", "sample", "valid_run", "behavior_pass", "error_type")}
                    for r in reports]}
    (args.report_dir / (args.label + "-summary.json")).write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return all(row["behavior_pass"] for row in reports)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--baseline-snapshot", type=Path)
    parser.add_argument("--label", choices=("baseline", "candidate"), default="candidate")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"))
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=300)
    options = parser.parse_args()
    if options.repeats < 1 or options.timeout <= 0 or options.parallel < 1:
        parser.error("repeats and timeout must be positive")
    raise SystemExit(0 if asyncio.run(main(options)) else 1)
