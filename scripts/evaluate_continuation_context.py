#!/usr/bin/env python3
"""Opt-in live-model evaluation of continuation and V4A conflict recovery.

Authored scenario history is labelled as fixture data. Every new response is
from the configured real provider, and edits/processes use the actual runtime.
Only disposable fixture files may be edited. Credentials and raw requests are
never written to the report. No completion reviewer forces the model to act.
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

from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.adapters.builtin.core_tools import CoreReadOnlyToolProvider
from tsm_agt.adapters.builtin.workspace_tools import CoreWorkspaceMutationToolProvider
from tsm_agt.adapters.http_json import ModelProviderTransportError
from tsm_agt.adapters.openai_compatible.model import OpenAICompatibleModelProvider
from tsm_agt.adapters.local_sandbox import LocalWorkspaceSandbox
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.composition import _platform_workspace_path
from tsm_agt.bootstrap.model_configuration import load_model_configuration
from tsm_agt.core import AgentTurnResult, AgentTurnSuspended, ApprovalDecision, TaskState
from tsm_agt.ports import Message, MessageRole, ModelStreamCompleted, TextBlock, ToolCallBlock
from tsm_agt.core.v4a import apply_v4a_update


FILES = {
    "provider.py": 'def build_payload(message, reasoning_effort="low"):\n'
                   '    return {"message": message, "reasoning_effort": reasoning_effort}\n',
    "runtime.py": 'from provider import build_payload\n\ndef send(message):\n'
                  '    return build_payload(message)\n',
    "web.py": 'from runtime import send\n\ndef submit(form):\n'
              '    return send(form["message"])\n',
    "test_flow.py": 'import unittest\nfrom web import submit\n\nclass FlowTest(unittest.TestCase):\n'
                    '    def test_selected_effort(self):\n'
                    '        self.assertEqual(submit({"message":"hi", "reasoning_effort":"high"})["reasoning_effort"], "high")\n'
                    '    def test_default(self):\n'
                    '        self.assertEqual(submit({"message":"hi"})["reasoning_effort"], "low")\n',
}
PRIOR_USER = (
    "请把网页选择的 reasoning_effort 从 web.submit 经过 runtime.send 传到 provider.build_payload，"
    "并保留不传参数时 low 的默认值。直接修改实际文件，不要修改 provider.py 或 test_flow.py。"
)
PRIOR_ASSISTANT = (
    "需要修改两处：web.submit 读取表单中的 reasoning_effort 并传给 runtime.send；"
    "runtime.send 增加可选参数并传给 provider.build_payload。provider 已经支持该参数。"
    "我还没有写入这两项修改。接下来需要补上参数传递并运行测试。"
)


class V4AOnlyMutationTools(CoreWorkspaceMutationToolProvider):
    async def list_tools(self):
        return tuple(tool for tool in await super().list_tools() if tool.name == "core.apply_patch_v4a")


class ObservedModel(OpenAICompatibleModelProvider):
    def __init__(self, cfg, baseline, inject_v4a_conflict=False, conflict_kind="import"):
        super().__init__(cfg.base_url, cfg.model, cfg.api_key,
            timeout_seconds=cfg.timeout_seconds, max_retries=0, streaming=cfg.streaming,
            output_token_parameter=cfg.effective_output_token_parameter,
            reasoning_effort=cfg.reasoning_effort, strict_tool_schema=cfg.strict_tool_schema)
        self.baseline = baseline
        self.rounds = []
        self.work_state_views = []
        self.inject_v4a_conflict = inject_v4a_conflict
        self.conflict_kind = conflict_kind
        self.faults = []
        self.provider_conflict_inputs = []
        self.provider_patch_inputs = []
        self.fixture_root = None

    def _capture_request(self, request, payload):
        super()._capture_request(request, payload)
        # Record the actual serialized error sent to the real provider, without
        # storing request headers, credentials, or unrelated message bodies.
        for message in payload.get("messages", []):
            if message.get("role") != "tool":
                continue
            result = json.loads(message["content"])
            if result.get("error_code") == "PATCH_CONFLICT":
                self.provider_conflict_inputs.append({
                    "model_call": len(self.rounds) + 1,
                    "tool_call_id": message["tool_call_id"], "result": result,
                })
            if (result.get("data") or {}).get("patch_matches"):
                self.provider_patch_inputs.append({"model_call": len(self.rounds) + 1,
                                                   "tool_call_id": message["tool_call_id"],
                                                   "result": result})

    async def stream_complete(self, request):
        messages = []
        views = []
        for message in request.messages:
            if message.message_id.startswith("session-context-"):
                body = json.loads(message.text)
                if self.baseline:
                    for key in ("recorded_goal", "remaining_work_record_status", "request_completion_status"):
                        body.get("work_state", {}).pop(key, None)
                    message = replace(message, content=(TextBlock(json.dumps(body, ensure_ascii=False)),))
                views.append(body.get("work_state"))
            messages.append(message)
        self.work_state_views.append(views)
        async for event in super().stream_complete(replace(request, messages=tuple(messages))):
            if isinstance(event, ModelStreamCompleted):
                response = event.response
                if self.inject_v4a_conflict and not self.faults:
                    blocks = list(response.message.content)
                    for index, block in enumerate(blocks):
                        if not isinstance(block, ToolCallBlock) or block.call.name != "core.apply_patch_v4a":
                            continue
                        original = block.call.arguments.get("patch", "")
                        header = re.search(r"^\*\*\* Update File: (.+)$", original, re.M)
                        if header is None or header.group(1) not in {"web.py", "runtime.py"}:
                            continue
                        # Controlled input fault, never a fabricated tool result.
                        # The punctuation typo is after a real matching line, so
                        # the matcher can compare the patch's old side with the
                        # actual source. The model chooses its own recovery.
                        if self.conflict_kind == "similarity":
                            # Preserve the real model's intended edit; only
                            # damage one interior old-side line in a whole-file
                            # hunk, as in the historical failure. Real matching
                            # and real writing decide whether it can be applied.
                            source = (self.fixture_root / header.group(1)).read_text()
                            desired = apply_v4a_update(header.group(1), source, original)
                            old = source.replace('# API route: /api/items', '# API route: /api.items')
                            injected = ('*** Begin Patch\n*** Update File: ' + header.group(1) + '\n@@\n'
                                        + '\n'.join('-' + line for line in old.splitlines()) + '\n'
                                        + '\n'.join('+' + line for line in desired.splitlines())
                                        + '\n*** End Patch')
                        elif self.conflict_kind == "ambiguous":
                            fault_hunk = '\n@@\n-# API route: /api/items\n+# API route: /api/items'
                            injected = original[:header.end()] + fault_hunk + original[header.end():]
                        elif self.conflict_kind == "import":
                            fault_hunk = ("\n@@\n-from dataclasses import dataclass, field"
                                          "\n+from dataclasses import dataclass, field")
                            injected = original[:header.end()] + fault_hunk + original[header.end():]
                        else:
                            fault_hunk = "\n@@\n from dataclasses import dataclass, field, replace"
                            if self.conflict_kind == "deletion":
                                fault_hunk += "\n-# API route: /api.items\n+# API route: /api.items"
                            else:
                                fault_hunk += "\n # API route: /api.items"
                            injected = original[:header.end()] + fault_hunk + original[header.end():]
                        arguments = {**block.call.arguments, "patch": injected}
                        blocks[index] = replace(block, call=replace(block.call, arguments=arguments))
                        response = replace(response, message=replace(response.message, content=tuple(blocks)))
                        event = replace(event, response=response)
                        self.faults.append({
                            "kind": "controlled_incorrect_" + self.conflict_kind,
                            "call_id": block.call.call_id,
                            "path": header.group(1),
                            "original_patch_sha256": hashlib.sha256(original.encode()).hexdigest(),
                            "injected_patch_sha256": hashlib.sha256(injected.encode()).hexdigest(),
                        })
                        break
                row = {"text": response.message.text, "finish_reason": response.finish_reason.value,
                       "calls": [{"call_id": block.call.call_id, "tool": block.call.name,
                                  "arguments": dict(block.call.arguments)}
                                 for block in response.message.content if isinstance(block, ToolCallBlock)]}
                self.rounds.append(row)
                print(json.dumps({"round": len(self.rounds), "tools": [c["tool"] for c in row["calls"]],
                                  "note": row["text"][:120]}, ensure_ascii=False), flush=True)
            yield event


def permitted(call, case):
    allowed = {"web.py", "runtime.py"} if case == "implementation" else {"web.py"}
    if case == "analysis":
        return False
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
        return (call.arguments.get("cwd") == "." and bool(argv)
                and argv[0] in {sys.executable, "python", "python3"}
                and "unittest" in argv and "-c" not in argv
                and not any(".." in arg or "/" in arg for arg in argv[1:]))
    return False


async def evaluate(args):
    cfg = load_model_configuration(args.env_file, os.environ)
    inject_conflict = getattr(args, "inject_v4a_conflict", False)
    conflict_kind = getattr(args, "v4a_conflict_kind", "import")
    model = ObservedModel(cfg, bool(args.baseline_segments), inject_conflict, conflict_kind)
    workspace_path = _platform_workspace_path()
    mutations = V4AOnlyMutationTools() if inject_conflict else CoreWorkspaceMutationToolProvider()
    app = compose_fixture_application(model_adapter=model, tool_adapters=(
        CoreReadOnlyToolProvider(), mutations, CoreProcessToolProvider(),
    ), workspace_path_adapter=workspace_path, sandbox_adapter=LocalWorkspaceSandbox(workspace_path))
    if args.baseline_segments:
        segments = json.loads(args.baseline_segments.read_text(encoding="utf-8"))
        template = app.kernel.dependencies.prompt_template
        template = replace(template, static_segments=tuple(
            replace(segment, **segments[segment.segment_id]) for segment in template.static_segments
        ))
        app.kernel._dependencies = replace(app.kernel.dependencies, prompt_template=template)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    report = {"case": args.case, "model": cfg.model, "reasoning_effort": cfg.reasoning_effort,
              "baseline": bool(args.baseline_segments), "history_source": "authored_fixture",
              "sandbox": "LocalWorkspaceSandbox",
              "completion_review_mode": "off",
              "manifest_hash": app.kernel.dependencies.prompt_template.manifest().manifest_hash}
    await app.registry.start_all()
    try:
        with tempfile.TemporaryDirectory(prefix="continuation-live-") as directory:
            root = Path(directory).resolve()
            model.fixture_root = root
            files = dict(FILES)
            if inject_conflict:
                for name in ("web.py", "runtime.py"):
                    prefix = "from dataclasses import dataclass, field, replace\n"
                    if conflict_kind != "import":
                        prefix += "# API route: /api/items\n"
                    if conflict_kind == "ambiguous":
                        prefix += "# API route: /api/items\n"
                    files[name] = prefix + files[name]
            for name, content in files.items():
                (root / name).write_text(content, encoding="utf-8")
            task = await app.kernel.create_task("旧需求：调整日志展示", root, "live-continuation")
            for state in (TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                          TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING):
                task = await app.kernel.transition_task(task.task_id, state, state.value)
            # Seed actual Session records, not a synthetic tool success or model response.
            unit = await app.kernel._session_task_result_unit(
                task, "fixture-prior", Message("fixture-user", MessageRole.USER, (TextBlock(PRIOR_USER),)),
                Message("fixture-assistant", MessageRole.ASSISTANT, (TextBlock(PRIOR_ASSISTANT),)),
                assistant_replay_eligible=True,
            )
            await app.kernel.dependencies.store.commit_session(unit)
            current = "继续实施到完整可跑的标准，直接修改文件并运行现有测试，不要修改测试文件或 provider.py。"
            if args.case == "analysis":
                current = "本轮只分析前面需求需要改哪些地方，不要修改任何文件，也不要运行命令。"
            elif args.case == "restricted":
                current = "继续处理前面的需求，但只能修改 web.py，禁止修改 runtime.py、provider.py 和测试。无法完成就说明具体限制。"
            if args.case == "implementation":
                current += f" 测试命令：{sys.executable} -m unittest discover -s . -p test_flow.py。"
            progress = []
            result = None
            try:
                result = await app.kernel.run_agent_turn(
                    task.task_id, current, max_model_calls=16, max_tool_calls=45, max_output_tokens=8192,
                    on_progress=lambda item: progress.append({"kind": item.kind.value, "text": item.activity})
                    if item.kind.value == "commentary" else None,
                )
                while isinstance(result, AgentTurnSuspended):
                    call = (await app.kernel.get_task(task.task_id)).pending_approval.call
                    if not permitted(call, args.case):
                        report["unexpected_approval"] = {"tool": call.name, "arguments": dict(call.arguments)}
                        break
                    print(json.dumps({"approve_fixture_tool": call.name, "call_id": call.call_id}), flush=True)
                    result = await app.kernel.resolve_agent_approval(
                        result.approval_request_id, ApprovalDecision.APPROVE,
                        "Authorized disposable continuation evaluation",
                    )
            except ModelProviderTransportError as error:
                # Preserve partial evidence and mark this run invalid; a transport
                # outage is never a successful recovery. Avoid persisting raw
                # provider error bodies, which may contain sensitive headers.
                report["execution_error"] = {
                    "type": type(error).__name__, "reason_code": error.reason_code,
                    "category": error.category.value,
                }
            report["changed_files"] = [name for name, content in files.items()
                                       if (root / name).read_text(encoding="utf-8") != content]
            report["final_files"] = {name: (root / name).read_text(encoding="utf-8") for name in files}
            completed = await asyncio.to_thread(
                subprocess.run, [sys.executable, "-I", "-m", "unittest", "discover", "-s", str(root), "-p", "test_flow.py"],
                cwd=root, capture_output=True, text=True, timeout=30,
            )
            report["independent_tests"] = {"passed": completed.returncode == 0, "output": completed.stderr}
            events = await app.kernel.dependencies.store.read_events(task.task_id)
            report["tool_outcomes"] = [
                {"event": event.event_type, "call_id": event.payload["result"].get("call_id"),
                 "ok": event.payload["result"].get("ok"), "error_code": event.payload["result"].get("error_code"),
                 "data": event.payload["result"].get("data")}
                for event in events if event.event_type in {"tool.completed", "tool.failed"}
            ]
            report["answer"] = result.assistant_message.text if isinstance(result, AgentTurnResult) else None
            report["progress"] = progress
            report["outcome_type"] = type(result).__name__
            report["rounds"] = model.rounds
            report["fault_injections"] = model.faults
            report["provider_conflict_inputs"] = model.provider_conflict_inputs
            report["provider_patch_inputs"] = model.provider_patch_inputs
            report["model_work_state_views"] = model.work_state_views
            report["model_observed_test_pass"] = any(
                isinstance(outcome["data"], dict) and outcome["data"].get("succeeded") is True
                and outcome["data"].get("exit_code") == 0 for outcome in report["tool_outcomes"]
            )
            protected = all(report["final_files"][name] == files[name] for name in ("provider.py", "test_flow.py"))
            report["valid"] = (protected and not report.get("unexpected_approval")
                               and not report.get("execution_error") and isinstance(result, AgentTurnResult))
            if args.case == "implementation":
                report["valid"] &= (completed.returncode == 0 and report["model_observed_test_pass"]
                                    and set(report["changed_files"]) == {"web.py", "runtime.py"})
            elif args.case == "analysis":
                report["valid"] &= not report["changed_files"] and not any(
                    call["tool"] in {"core.apply_patch", "core.apply_patch_v4a", "core.apply_patches", "core.run_command"}
                    for row in model.rounds for call in row["calls"]
                )
            else:
                report["valid"] &= set(report["changed_files"]) <= {"web.py"}
            if inject_conflict:
                report["valid"] &= len(model.faults) == 1 and any(
                    outcome["call_id"] == model.faults[0]["call_id"]
                    and (outcome["ok"] is True if conflict_kind == "similarity"
                         else outcome["error_code"] == "PATCH_CONFLICT") for outcome in report["tool_outcomes"]
                )
                # A successful write somewhere else does not prove that the
                # failed edit was recovered. Check this file and the real
                # provider's serialized error input as well.
                report["valid"] &= bool(model.provider_patch_inputs if conflict_kind == "similarity"
                                         else model.provider_conflict_inputs)
                if model.faults:
                    report["valid"] &= model.faults[0]["path"] in report["changed_files"]
                if conflict_kind != "import":
                    report["valid"] &= all(
                        "# API route: /api/items\n" in report["final_files"][name]
                        and "/api.items" not in report["final_files"][name]
                        for name in ("web.py", "runtime.py")
                    )
                if conflict_kind in {"deletion", "context"}:
                    report["valid"] &= any(
                        item["result"]["data"]["patch_conflict"].get("patch_line_kind") == conflict_kind
                        and item["result"]["data"]["patch_conflict"].get("actual_line") == "# API route: /api/items"
                        for item in model.provider_conflict_inputs
                    )
                if conflict_kind == "similarity":
                    report["valid"] &= any(
                        item["tool_call_id"] == model.faults[0]["call_id"]
                        and any(match.get("approximate") for match in item["result"]["data"]["patch_matches"])
                        for item in model.provider_patch_inputs
                    )
                if conflict_kind == "ambiguous":
                    report["valid"] &= any(
                        item["result"]["data"]["patch_conflict"].get("match_failure") == "ambiguous"
                        for item in model.provider_conflict_inputs
                    )
            suffix = "-v4a-conflict-" + conflict_kind if inject_conflict else ""
            path = args.report_dir / (args.case + ("-baseline" if args.baseline_segments else "-candidate") + suffix + ".json")
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"report": str(path), "valid": report["valid"],
                              "changed_files": report["changed_files"], "model_rounds": len(model.rounds),
                              "independent_tests_passed": completed.returncode == 0}, ensure_ascii=False), flush=True)
            return report["valid"]
    finally:
        await app.registry.stop_all()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--case", choices=("implementation", "analysis", "restricted"), default="implementation")
    parser.add_argument("--baseline-segments", type=Path)
    parser.add_argument("--inject-v4a-conflict", action="store_true",
                        help="Inject one incorrect context line into a real model patch; use real tool rejection and model recovery.")
    parser.add_argument("--v4a-conflict-kind", choices=("import", "deletion", "context", "similarity", "ambiguous"), default="import",
                        help="Select an unmatched import or an old-side punctuation typo after matched context.")
    raise SystemExit(0 if asyncio.run(evaluate(parser.parse_args())) else 1)
