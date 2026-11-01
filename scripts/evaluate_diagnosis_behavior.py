#!/usr/bin/env python3
"""Compare diagnosis prompts against synthetic cases in disposable workspaces.

Uses real model responses and governed local tools, with no completion reviewer.
Only fixture edits and the documented unittest command may be approved. Reports
separate independent behavior checks from model-observed verification and prose.
No production history, business source, raw requests or credentials are exported.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from scripts.evaluate_execution_plan_behavior import MUTATION_TOOLS
from scripts.evaluate_multiturn_behavior import (
    file_state, load_fixture, observe_model_exchanges, tool_result_facts,
)
from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.adapters.builtin.core_tools import CoreReadOnlyToolProvider
from tsm_agt.adapters.builtin.workspace_tools import CoreWorkspaceMutationToolProvider
from tsm_agt.adapters.local_sandbox import LocalWorkspaceSandbox
from tsm_agt.adapters.openai_compatible.model import OpenAICompatibleModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.composition import _platform_workspace_path
from tsm_agt.bootstrap.model_configuration import load_model_configuration
from tsm_agt.core import (
    AgentTurnResult, AgentTurnSuspended, ApprovalDecision,
    PromptTemplate, PromptTemplateSegment, TaskState,
)
from tsm_agt.ports import Message, MessageRole, TextBlock


CASES = ("partial-dataflow", "transient-fix", "already-satisfied")
FIXTURES = Path(__file__).parent / "fixtures"


def read_template(path: Path | None) -> PromptTemplate:
    if path is None:
        return PromptTemplate.default()
    data = json.loads(path.read_text(encoding="utf-8"))
    return PromptTemplate(
        manifest_id=data["manifest_id"], revision=data["revision"],
        static_segments=tuple(PromptTemplateSegment(**{
            **row, "requires_any_tools": tuple(row.get("requires_any_tools", ())),
        }) for row in data["static_segments"]),
    )


def run_tests(root: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-I", "-m", "unittest", "discover", "-s", str(root), "-p", "test_flow.py"],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    return {"passed": result.returncode == 0, "exit_code": result.returncode,
            "output": (result.stdout + result.stderr)[-5000:]}


def permitted(call, fixture: dict) -> bool:
    """Bound approval to the operator's synthetic workspace experiment."""
    editable = set(fixture["editable_files"])
    if call.name == "core.apply_patch_v4a":
        paths = re.findall(r"^\*\*\* (?:Update|Add) File: (.+)$", call.arguments.get("patch", ""), re.M)
        return bool(paths) and set(paths) <= editable and "*** Delete File:" not in call.arguments.get("patch", "")
    if call.name == "core.apply_patch":
        return call.arguments.get("path") in editable
    if call.name == "core.apply_patches":
        patches = call.arguments.get("patches", [])
        return bool(patches) and all(patch.get("path") in editable for patch in patches)
    if call.name == "core.run_command":
        argv = call.arguments.get("argv", [])
        return (call.arguments.get("cwd") == "."
                and len(argv) >= 3 and argv[0] in {sys.executable, "python", "python3"}
                and argv[1:] in (["-m", "unittest"], ["-m", "unittest", "test_flow"],
                                ["-m", "unittest", "discover", "-s", ".", "-p", "test_flow.py"]))
    return False


async def run_case(args, cfg, case: str, trial: int) -> dict:
    fixture = load_fixture(FIXTURES / f"diagnosis-{case}.json")
    template = read_template(args.prompt_template)
    effort = args.reasoning_effort or cfg.reasoning_effort
    model = OpenAICompatibleModelProvider(
        cfg.base_url, cfg.model, cfg.api_key, timeout_seconds=cfg.timeout_seconds,
        max_retries=0, streaming=cfg.streaming,
        output_token_parameter=cfg.effective_output_token_parameter,
        reasoning_effort=effort, strict_tool_schema=cfg.strict_tool_schema,
    )
    workspace_path = _platform_workspace_path()
    app = compose_fixture_application(
        model_adapter=model, tool_adapters=(CoreReadOnlyToolProvider(),
            CoreWorkspaceMutationToolProvider(), CoreProcessToolProvider()),
        workspace_path_adapter=workspace_path,
        sandbox_adapter=LocalWorkspaceSandbox(workspace_path),
    )
    app.kernel._dependencies = replace(app.kernel.dependencies, prompt_template=template)
    exchanges = []
    restore = observe_model_exchanges(model, exchanges)
    report = {"case": case, "trial": trial, "label": args.label,
              "history_source": "authored_synthetic_fixture", "runtime_store": "in_memory",
              "task_goal": "继续",
              "fixture_sha256": hashlib.sha256((FIXTURES / f"diagnosis-{case}.json").read_bytes()).hexdigest(),
              "test_command_supplied_in_request": True,
              "model": cfg.model, "reasoning_effort": effort,
              "wire_reasoning_efforts": [],
              "prompt_revision": template.revision,
              "manifest_hash": template.manifest().manifest_hash,
              "completion_review_mode": "off", "valid_run": False}
    capture_request = model._capture_request
    def capture_effort(request, payload):
        report["wire_reasoning_efforts"].append(payload.get("reasoning_effort"))
        capture_request(request, payload)
    model._capture_request = capture_effort
    await app.registry.start_all()
    try:
        with tempfile.TemporaryDirectory(prefix="tsm-diagnosis-eval-") as directory:
            root = Path(directory).resolve()
            for name, content in fixture["files"].items():
                (root / name).write_text(content, encoding="utf-8")
            before = file_state(root)
            report["initial_tests"] = await asyncio.to_thread(run_tests, root)
            # Match the neutral recorded goal in the investigated conversation;
            # fixture/evaluator labels must not supply a diagnosis instruction.
            task = await app.kernel.create_task(report["task_goal"], root, "diagnosis-eval")
            for state in (TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                          TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING):
                task = await app.kernel.transition_task(task.task_id, state, state.value)
            if effort:
                await app.kernel.update_session_reasoning_effort(task.session_id, effort)
            for index, pair in enumerate(fixture["history"]):
                unit = await app.kernel._session_task_result_unit(
                    task, f"authored-fixture-{index}",
                    Message(f"fixture-user-{index}", MessageRole.USER, (TextBlock(pair["user"]),)),
                    Message(f"fixture-assistant-{index}", MessageRole.ASSISTANT, (TextBlock(pair["assistant"]),)),
                    assistant_replay_eligible=True,
                )
                await app.kernel.dependencies.store.commit_session(unit)
            prompt = fixture["turns"][0]["input"]
            prompt += f" 项目测试命令：{sys.executable} -m unittest discover -s . -p test_flow.py。"
            result = None
            try:
                async with asyncio.timeout(args.timeout):
                    result = await app.kernel.run_agent_turn(
                        task.task_id, prompt, max_model_calls=12, max_tool_calls=30,
                        max_output_tokens=8192,
                    )
                    while isinstance(result, AgentTurnSuspended):
                        call = (await app.kernel.get_task(task.task_id)).pending_approval.call
                        if not permitted(call, fixture):
                            report["unapproved_tool"] = call.name
                            break
                        result = await app.kernel.resolve_agent_approval(
                            result.approval_request_id, ApprovalDecision.APPROVE,
                            "Authorized synthetic fixture edit or documented test command",
                        )
            except Exception as error:
                # Error bodies may contain provider details. Persist only a code.
                report["execution_error"] = {"type": type(error).__name__,
                                             "reason_code": getattr(error, "reason_code", None)}
            report["independent_tests"] = await asyncio.to_thread(run_tests, root)
            after = file_state(root)
            report["changed_files"] = sorted(name for name in before.keys() | after.keys()
                                              if before.get(name) != after.get(name))
            events = await app.kernel.dependencies.store.read_events(task.task_id)
            calls = {event.payload["call"]["call_id"]: event.payload["call"]
                     for event in events if event.event_type == "tool.requested"}
            report["tool_names"] = [call["name"] for call in calls.values()]
            last_edit = -1
            last_pass = -1
            for event in events:
                if event.event_type not in {"tool.completed", "tool.failed"}:
                    continue
                outcome = event.payload["result"]
                name = calls.get(outcome["call_id"], {}).get("name")
                if name in MUTATION_TOOLS:
                    last_edit = event.sequence
                data = outcome.get("data") or {}
                if (name == "core.run_command" and outcome.get("ok")
                        and data.get("succeeded") is True and data.get("exit_code") == 0):
                    last_pass = event.sequence
            report["model_verified_after_last_edit"] = last_pass > last_edit
            report["scope_preserved"] = set(report["changed_files"]) <= set(fixture["editable_files"])
            report["fixture_preserved"] = all(before.get(name) == after.get(name)
                                                for name in fixture["preserve_files"])
            report["no_op_preserved"] = (case != "already-satisfied" or
                (not report["changed_files"] and not set(report["tool_names"]) & MUTATION_TOOLS))
            report["unrequested_edit_observed"] = not report["no_op_preserved"]
            report["model_exchanges"] = exchanges
            report["tool_results"] = tool_result_facts(events)
            report["answer"] = result.assistant_message.text if isinstance(result, AgentTurnResult) else None
            report["answer_accuracy"] = "requires_human_review"
            report["valid_run"] = (isinstance(result, AgentTurnResult) and bool(exchanges)
                and not report.get("execution_error") and not report.get("unapproved_tool")
                and report["initial_tests"]["passed"] == fixture["initial_tests_pass"])
            report["case_passed"] = all((report["valid_run"], report["independent_tests"]["passed"],
                report["model_verified_after_last_edit"], report["scope_preserved"],
                report["fixture_preserved"], report["no_op_preserved"]))
            report["final_files"] = {name: (root / name).read_text(encoding="utf-8")
                                     for name in fixture["editable_files"]}
    finally:
        restore()
        await app.registry.stop_all()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--prompt-template", type=Path, help="serialized baseline PromptTemplate; omit for current")
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--case", choices=(*CASES, "all"), default="all")
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"),
                        help="match a production session override; defaults to provider configuration")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--allow-model-calls", action="store_true")
    args = parser.parse_args()
    if not args.allow_model_calls:
        parser.error("real model calls require --allow-model-calls")
    if args.repeat < 1 or args.timeout <= 0:
        parser.error("repeat and timeout must be positive")
    cfg = load_model_configuration(args.config)
    if cfg.protocol != "open" or cfg.open_api_mode != "chat":
        parser.error("this evaluator requires the configured OpenAI-compatible Chat provider")
    args.report_dir.mkdir(parents=True, exist_ok=True)

    async def evaluate():
        failed = False
        for case in CASES if args.case == "all" else (args.case,):
            for trial in range(1, args.repeat + 1):
                path = args.report_dir / f"{args.label}-{case}-{trial}.json"
                if path.exists():
                    parser.error(f"report already exists: {path}")
                report = await run_case(args, cfg, case, trial)
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(json.dumps({key: report[key] for key in (
                    "case", "trial", "label", "valid_run", "case_passed", "changed_files",
                    "model_verified_after_last_edit", "tool_names",
                )}, ensure_ascii=False), flush=True)
                failed |= not report["case_passed"]
        return int(failed)
    return asyncio.run(evaluate())


if __name__ == "__main__":
    raise SystemExit(main())
