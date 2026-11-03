#!/usr/bin/env python3
"""Measure implicit verification routing in disposable, synthetic projects.

The user's task contains no test reminder in implicit mode. Explicit mode is a
positive control, not evidence of autonomous routing. No production transcript,
project-instructions file, completion reviewer or verification stop hook is used.
Reports distinguish model-observed checks from independent fixture validation.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

from scripts.evaluate_diagnosis_behavior import read_template
from scripts.evaluate_execution_plan_behavior import MUTATION_TOOLS
from scripts.evaluate_multiturn_behavior import file_state, load_fixture, observe_model_exchanges
from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.adapters.builtin.core_tools import CoreReadOnlyToolProvider
from tsm_agt.adapters.builtin.workspace_tools import CoreWorkspaceMutationToolProvider
from tsm_agt.adapters.local_sandbox import LocalWorkspaceSandbox
from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.adapters.skill_catalog import SkillCatalog
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.composition import _physical_model_provider, _platform_workspace_path
from tsm_agt.bootstrap.model_configuration import load_model_configuration
from tsm_agt.core import AgentTurnResult, AgentTurnSuspended, ApprovalDecision, ProjectTrustLevel, TaskState
from tsm_agt.ports import Message, MessageRole, TextBlock, ToolResultBlock

FIXTURES = Path(__file__).parent / "fixtures"
CASES = ("ui-short", "ui-long", "prompt-followup", "analysis-only", "prose-only")
EXPLICIT_REQUEST = " 请使用 verify-change 工作流，修改后运行相关检查并读取结果。"


class FrozenSkillCatalog(SkillCatalog):
    """Snapshot instruction data, not model decisions, for one measured run."""

    def __init__(self, store):
        super().__init__(store)
        self._skills = super().discover()
        self._resources = {
            (skill.name, resource): super(FrozenSkillCatalog, self).read(skill, resource)
            for skill in self._skills for resource in ("SKILL.md", "references/workflow.md")
        }

    def discover(self):
        return self._skills

    def read(self, skill, resource="SKILL.md"):
        return dict(self._resources[(skill.name, resource)])


def input_for(fixture: dict, mode: str) -> str:
    text = fixture["turns"][0]["input"]
    if mode == "explicit" and fixture["behavior_change"]:
        text += EXPLICIT_REQUEST
    return text


def known_runner(executable: str, names: tuple[str, ...]) -> bool:
    # Preflight expands bare executables to governed absolute launch paths
    # before requesting approval. Accept those observed paths, too.
    return executable in {*names, *(shutil.which(name) for name in names)}


def is_fixture_check(arguments: dict) -> bool:
    argv = arguments.get("argv", [])
    if (arguments.get("cwd") != "." or not argv
            or arguments.get("mode", "foreground") != "foreground"):
        return False
    if known_runner(argv[0], ("pytest",)):
        runner, options = "pytest", argv[1:]
    elif len(argv) >= 3 and known_runner(argv[0], (sys.executable, "python", "python3")) and argv[1] == "-m":
        runner, options = argv[2], argv[3:]
    else:
        return False
    if runner == "pytest":
        # An equivalent existing runner is acceptable; help, collection-only,
        # deselection and arbitrary code invocation are not behavior checks.
        return all(token in {"test_flow.py", ".", "-v", "-q", "-x", "--disable-warnings"}
                   or re.fullmatch(r"test_flow\.py::[A-Za-z0-9_:]+", token)
                   for token in options)
    if runner != "unittest":
        return False
    allowed = {"discover", "-s", ".", "-p", "test_flow.py", "test_flow", "-v", "-q"}
    return all(token in allowed or re.fullmatch(r"test_flow\.[A-Za-z0-9_.]+", token)
               for token in options)


def is_environment_probe(arguments: dict) -> bool:
    """Allow bounded discovery without counting it as behavior verification."""
    argv = arguments.get("argv", [])
    if arguments.get("cwd") != "." or not argv:
        return False
    if (known_runner(argv[0], ("pwd",)) and len(argv) == 1
            or known_runner(argv[0], ("which",)) and argv[1:] in
            (["python"], ["python3"], ["node"], ["pytest"])):
        return True
    if not known_runner(argv[0], (sys.executable, "python", "python3")):
        return False
    if argv[1:] in (["--version"], ["-V"]):
        return True
    if len(argv) != 3 or argv[1] != "-c":
        return False
    try:
        tree = ast.parse(argv[2])
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
            if any(name not in {"sys", "pathlib", "json", "prompt_loader"} for name in modules):
                return False
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if name not in {"print", "Path", "read_text", "exists", "is_file", "loads", "load_sections"}:
                return False
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Lambda, ast.With)):
            return False
    return True


def permitted(call, fixture: dict) -> bool:
    editable = set(fixture["editable_files"])
    if call.name == "core.apply_patch":
        return call.arguments.get("path") in editable
    if call.name == "core.apply_patch_v4a":
        body = call.arguments.get("patch", "")
        paths = re.findall(r"^\*\*\* (?:Update|Add) File: (.+)$", body, re.M)
        return bool(paths) and set(paths) <= editable and "*** Delete File:" not in body
    if call.name == "core.apply_patches":
        changes = call.arguments.get("patches", [])
        return bool(changes) and all(item.get("path") in editable for item in changes)
    return (fixture["behavior_change"] and call.name == "core.run_command"
            and (is_fixture_check(call.arguments) or is_environment_probe(call.arguments)))


def materialize(fixture: dict, root: Path) -> None:
    node = shutil.which("node")
    if "toolchain.json" in fixture["files"] and node is None:
        raise RuntimeError("Node.js is required for the JavaScript fixture")
    for name, content in fixture["files"].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.replace("{{PYTHON}}", sys.executable).replace(
            "{{TOOLCHAIN}}", json.dumps({"node": node}) + "\n"), encoding="utf-8")


def independent_check(root: Path) -> dict:
    result = subprocess.run([sys.executable, "-I", "-m", "unittest", "discover", "-s", str(root),
                             "-p", "test_flow.py"], cwd=root, capture_output=True, text=True, timeout=30)
    # -I intentionally removes cwd; unittest discovery adds its -s directory.
    return {"passed": result.returncode == 0, "exit_code": result.returncode}


def fixture_file_state(root: Path) -> dict:
    return {name: value for name, value in file_state(root).items()
            if Path(name).parts[0] != ".pytest_cache"}


def score_trace(events, observed_result_ids: set[str]) -> dict:
    calls = {event.payload["call"]["call_id"]: event.payload["call"]
             for event in events if event.event_type == "tool.requested"}
    last_edit = -1
    checks = []
    skill_loaded = False
    reference_loaded = False
    for event in events:
        if event.event_type not in {"tool.completed", "tool.failed"}:
            continue
        result = event.payload["result"]
        call = calls.get(result["call_id"], {})
        name, arguments = call.get("name"), call.get("arguments", {})
        if name in MUTATION_TOOLS and result.get("ok"):
            last_edit = event.sequence
        if name == "skill.load" and arguments.get("name") == "verify-change" and result.get("ok"):
            if arguments.get("resource", "SKILL.md") == "SKILL.md":
                skill_loaded = True
            if arguments.get("resource") == "references/workflow.md":
                reference_loaded = True
        if name == "core.run_command":
            data = result.get("data") or {}
            checks.append({"sequence": event.sequence, "call_id": result["call_id"],
                           "relevant_fixture_check": is_fixture_check(arguments),
                           "ok": result.get("ok"), "exit_code": data.get("exit_code"),
                           "succeeded": data.get("succeeded"), "error_code": result.get("error_code"),
                           "observed_by_model": result["call_id"] in observed_result_ids})
    verified = any(last_edit >= 0 and check["sequence"] > last_edit
                   and check["relevant_fixture_check"] and check["ok"]
                   and check["succeeded"] is True and check["exit_code"] == 0
                   and check["observed_by_model"] for check in checks)
    return {"tool_names": [call["name"] for call in calls.values()],
            "tool_calls": [{"tool": call["name"], "arguments": {
                key: value for key, value in call.get("arguments", {}).items()
                if key in {"path", "resource", "argv", "cwd", "mode", "pattern", "query"}
            }} for call in calls.values()],
            "verification_skill_loaded": skill_loaded, "workflow_reference_loaded": reference_loaded,
            "last_successful_edit_sequence": last_edit, "checks": checks,
            "model_verified_after_last_edit": verified}


def history_for(fixture: dict) -> list[dict]:
    history = []
    topics = ("滚动位置", "卡片展开状态", "工具耗时", "键盘操作", "换行与长文本", "错误展示")
    for index in range(fixture.get("history_padding_rounds", 0)):
        topic = topics[index % len(topics)]
        history.append({
            "user": f"界面讨论记录 {index + 1}：{topic}需要保持稳定。工具调用和阶段说明应根据事件事实展示，当前先讨论表达，不修改代码。",
            "assistant": f"关于{topic}，应保留已有状态与用户选择，时间线展示跟随实际事件顺序。模型请求、工具操作、阶段说明和最终回答有不同用途；最终回答保持独立。后续界面调整需要保留这些边界，不从展示顺序推断工具已经执行，也不把历史说明当成本轮执行结果。",
        })
    return history + fixture.get("history", [])


async def run_case(args, cfg, case: str, trial: int) -> dict:
    path = FIXTURES / f"verification-{case}.json"
    fixture = load_fixture(path)
    template = read_template(args.prompt_template)
    model = _physical_model_provider(
        base_url=cfg.base_url, model=cfg.model, api_key=cfg.api_key, protocol=cfg.protocol,
        anthropic_version=cfg.anthropic_version, timeout_seconds=cfg.timeout_seconds,
        output_token_parameter=cfg.effective_output_token_parameter,
        strict_tool_schema=cfg.strict_tool_schema, streaming=cfg.streaming and not args.no_stream,
        open_api_mode=cfg.open_api_mode, reasoning_effort=args.reasoning_effort or cfg.reasoning_effort,
        capability_overrides=cfg.capability_overrides,
    )
    report = {"case": case, "trial": trial, "label": args.label, "mode": args.mode,
              "model": cfg.model, "prompt_revision": template.revision,
              "streaming": cfg.streaming and not args.no_stream,
              "manifest_hash": template.manifest().manifest_hash,
              "fixture_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "history_source": "authored_synthetic_not_production_replay",
              "project_instructions_present": False, "test_command_in_current_input": False,
              "verification_reminder_in_current_input": args.mode == "explicit" and fixture["behavior_change"],
              "completion_review_mode": "off", "verification_stop_hooks": 0}
    exchanges, observed_ids, request_facts = [], set(), []
    restore = observe_model_exchanges(model, exchanges)
    capture = getattr(model, "_capture_request", lambda request, payload: None)
    def observe_request(request, payload):
        observed_ids.update(block.result.call_id for message in request.messages
                            for block in message.content if isinstance(block, ToolResultBlock))
        request_facts.append({"input_characters": sum(len(message.text) for message in request.messages),
                              "skill_catalog_present": any(m.message_id == "tool-catalog-context-skill.load" for m in request.messages)})
        capture(request, payload)
    model._capture_request = observe_request
    with tempfile.TemporaryDirectory(prefix="tsm-verification-eval-") as directory:
        base = Path(directory).resolve()
        root = base / "project"
        root.mkdir()
        materialize(fixture, root)
        before = fixture_file_state(root)
        report["initial_independent_check"] = independent_check(root) if fixture["behavior_change"] else None
        paths = _platform_workspace_path()
        store = UserSkillsStore(base / "isolated-skills")
        catalog = FrozenSkillCatalog(store)
        with patch("tsm_agt.bootstrap.composition.UserSkillsStore", return_value=store), \
                patch("tsm_agt.bootstrap.composition.SkillCatalog", return_value=catalog):
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=(CoreReadOnlyToolProvider(),
                    CoreWorkspaceMutationToolProvider(), CoreProcessToolProvider()),
                workspace_path_adapter=paths, sandbox_adapter=LocalWorkspaceSandbox(paths), enable_skills=True,
            )
        app.kernel._dependencies = replace(app.kernel.dependencies, prompt_template=template)
        await app.start()
        try:
            assert app.kernel.dependencies.completion_reviewer is None
            assert not app.kernel.dependencies.turn_stop_policies
            await app.kernel.set_project_trust(root, ProjectTrustLevel.TRUSTED_BUILD)
            task = await app.kernel.create_task("继续", root)
            for state in (TaskState.INTAKE, TaskState.RESOLVING_PROJECT, TaskState.SELECTING_EXTENSIONS,
                          TaskState.ROUTING, TaskState.EXECUTING):
                task = await app.kernel.transition_task(task.task_id, state, "fixture execution")
            history = history_for(fixture)
            for index, pair in enumerate(history):
                unit = await app.kernel._session_task_result_unit(
                    task, f"fixture-{index}",
                    Message(f"fixture-user-{index}", MessageRole.USER, (TextBlock(pair["user"]),)),
                    Message(f"fixture-assistant-{index}", MessageRole.ASSISTANT, (TextBlock(pair["assistant"]),)),
                    assistant_replay_eligible=True,
                )
                await app.kernel.dependencies.store.commit_session(unit)
            report["history_pairs"] = len(history)
            print(json.dumps({"event": "evaluation.started", "case": case, "label": args.label,
                              "manifest_hash": report["manifest_hash"]}), flush=True)
            result = None
            try:
                async with asyncio.timeout(args.timeout):
                    result = await app.kernel.run_agent_turn(task.task_id, input_for(fixture, args.mode),
                        max_model_calls=12, max_tool_calls=30, max_output_tokens=8192)
                    while isinstance(result, AgentTurnSuspended):
                        call = (await app.kernel.get_task(task.task_id)).pending_approval.call
                        if not permitted(call, fixture):
                            report["out_of_scope_attempt"] = call.name
                            report["unsupported_arguments"] = {
                                key: call.arguments.get(key) for key in ("argv", "cwd", "mode")
                                if key in call.arguments
                            }
                            break
                        result = await app.kernel.resolve_agent_approval(result.approval_request_id,
                            ApprovalDecision.APPROVE, "Authorized disposable fixture edit or relevant local check")
            except Exception as error:
                report["execution_error"] = {"type": type(error).__name__, "reason_code": getattr(error, "reason_code", None)}
            events = await app.kernel.dependencies.store.read_events(task.task_id)
            report.update(score_trace(events, observed_ids))
            after = fixture_file_state(root)
            report["changed_files"] = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
            report["preserved_files_unchanged"] = all(before.get(name) == after.get(name) for name in fixture["preserve_files"])
            report["scope_preserved"] = set(report["changed_files"]) <= set(fixture["editable_files"])
            report["independent_check"] = independent_check(root) if fixture["behavior_change"] else None
            report["reviewer_calls"] = sum(e.event_type == "turn.completion_review_started" for e in events)
            report["valid_run"] = bool(exchanges) and not report.get("execution_error") and not report.get("out_of_scope_attempt")
            report["completed"] = isinstance(result, AgentTurnResult)
            report["case_passed"] = all((report["valid_run"], report["completed"], report["scope_preserved"], report["preserved_files_unchanged"]))
            if fixture["behavior_change"]:
                report["case_passed"] &= bool(report["changed_files"]) and report["model_verified_after_last_edit"] and report["independent_check"]["passed"]
            elif case == "analysis-only":
                report["case_passed"] &= not report["changed_files"] and not report["checks"] and not report["verification_skill_loaded"]
            else:
                report["case_passed"] &= ((root / "README.md").read_text().count('这是一个本地开发工具。') == 1 and not report["checks"] and not report["verification_skill_loaded"])
            report["request_facts"] = request_facts
            report["model_exchanges"] = exchanges
            report["answer"] = result.assistant_message.text if isinstance(result, AgentTurnResult) else None
            skill = next(t for t in app.kernel.dependencies.skill_registry.list_all() if t.name == 'verify-change')
            content = catalog.read(skill)["content"]
            report["skill_content_sha256"] = hashlib.sha256(content.encode()).hexdigest()
        finally:
            restore()
            await app.registry.stop_all()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--prompt-template', type=Path)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--case', choices=(*CASES, 'all'), default='all')
    parser.add_argument('--mode', choices=('implicit', 'explicit'), default='implicit')
    parser.add_argument('--label', default='candidate')
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--reasoning-effort', choices=('low', 'medium', 'high'))
    parser.add_argument('--allow-model-calls', action='store_true')
    parser.add_argument('--no-stream', action='store_true', help='measure complete responses when SSE transport is unreliable')
    args = parser.parse_args()
    if not args.allow_model_calls:
        parser.error('real model calls require --allow-model-calls')
    if args.repeat < 1 or args.timeout <= 0:
        parser.error('repeat and timeout must be positive')
    cfg = load_model_configuration(args.config)
    if cfg.protocol != 'open':
        parser.error('request observation currently requires an OpenAI-compatible provider')
    args.report_dir.mkdir(parents=True, exist_ok=True)
    async def evaluate():
        failed = False
        for case in CASES if args.case == 'all' else (args.case,):
            for trial in range(1, args.repeat + 1):
                path = args.report_dir / f'{args.label}-{case}-{args.mode}-{trial}.json'
                if path.exists():
                    raise ValueError(f'report already exists: {path}')
                report = await run_case(args, cfg, case, trial)
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
                print(json.dumps({k:report[k] for k in ('case','mode','case_passed','verification_skill_loaded','model_verified_after_last_edit','changed_files','tool_names')},ensure_ascii=False),flush=True)
                failed |= not report['case_passed']
        return int(failed)
    return asyncio.run(evaluate())


if __name__ == '__main__':
    raise SystemExit(main())
