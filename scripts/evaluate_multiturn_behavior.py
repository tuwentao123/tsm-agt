#!/usr/bin/env python3
"""Opt-in real-model multi-turn comparisons in fresh disposable workspaces.

Nothing is submitted to an existing conversation. Credentials are loaded from
the operator's private YAML configuration, never copied into the disposable
workspace or report.
Reports distinguish observed calls, file assertions, independent tests and the
final answer; they never equate a mutation with completed work.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from scripts.evaluate_execution_plan_behavior import _file_hash, _tool_names, MUTATION_TOOLS
from scripts.export_langfuse_evaluation import LangfuseExporter, LangfuseExportError
from tsm_agt.adapters.openai_compatible.request_diagnostics import TurnRequestDiagnostics
from tsm_agt.bootstrap import compose_openai_compatible_engineering_application_from_config
from tsm_agt.core.verification_command import is_verification_request
from tsm_agt.sdk.runtime import EngineeringAgentClient
from tsm_agt.ports import ModelCallPurpose, ModelStreamCompleted, ToolCallBlock
from tsm_agt.ports.process import process_result_succeeded


def observe_model_exchanges(provider, observations: list):
    """Observe only this disposable app, including non-final tool responses.

    The production event log intentionally does not duplicate intermediate
    response bodies. Do not mistake absent llm.completed rows for absent calls.
    No request text, tool arguments, credentials or response prose is recorded.
    """
    original_complete = provider.complete
    original_stream = getattr(provider, "stream_complete", None)
    seen = set()

    def record(request, response, started):
        if request.purpose is not ModelCallPurpose.AGENT_TURN:
            return
        identity = (request.turn_id, response.message.message_id)
        if identity in seen:
            return  # A streaming fallback may delegate to complete().
        seen.add(identity)
        observations.append({
            "turn_id": request.turn_id, "finish_reason": response.finish_reason.value,
            "started_at_unix_nano": started, "finished_at_unix_nano": time.time_ns(),
            "usage": response.usage.to_data(),
            "advertised_tools": [tool.name for tool in request.tools],
            "allow_tool_calls": request.allow_tool_calls,
            "request_messages_hash": hashlib.sha256(json.dumps(
                [message.to_data() for message in request.messages], ensure_ascii=False, sort_keys=True,
            ).encode()).hexdigest(),
            "proposed_tools": [{"call_id": block.call.call_id, "tool": block.call.name}
                               for block in response.message.content if isinstance(block, ToolCallBlock)],
        })

    async def complete(request):
        started = time.time_ns()
        response = await original_complete(request)
        record(request, response, started)
        return response

    async def stream(request):
        started = time.time_ns()
        async for item in original_stream(request):
            if isinstance(item, ModelStreamCompleted):
                record(request, item.response, started)
            yield item

    provider.complete = complete
    if callable(original_stream):
        provider.stream_complete = stream

    def restore():
        provider.complete = original_complete
        if callable(original_stream):
            provider.stream_complete = original_stream
    return restore


def workspace_path(root: Path, relative: str) -> Path:
    root = root.resolve()
    name = Path(relative)
    path = (root / name).resolve()
    if (name.is_absolute() or path == root or not path.is_relative_to(root)
            or {".agent", ".env", ".git", ".."}.intersection(name.parts)):
        raise ValueError("fixture paths must be relative, inside the disposable workspace and not runtime/config paths")
    return path


def load_fixture(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ValueError("fixture requires a files object")
    if not isinstance(data.get("turns"), list) or not data["turns"]:
        raise ValueError("fixture requires nonempty turns")
    commit = data.get("snapshot_commit")
    if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit)):
        raise ValueError("snapshot_commit must be a full Git commit hash")
    for relative, content in data["files"].items():
        workspace_path(Path("/disposable-evaluation"), relative)
        if not isinstance(content, str):
            raise ValueError("fixture file content must be text")
    preserved = data.get("preserve_files", [])
    if not isinstance(preserved, list) or any(not isinstance(name, str) for name in preserved):
        raise ValueError("preserve_files must be a list of paths")
    for name in preserved:
        workspace_path(Path("/disposable-evaluation"), name)
    if commit is None and any(name not in data["files"] for name in preserved):
        raise ValueError("preserve_files must name initial fixture files")
    for turn in data["turns"]:
        if not isinstance(turn, dict) or not isinstance(turn.get("input"), str) or not turn["input"].strip():
            raise ValueError("each turn requires an input string")
        phrases = turn.get("answer_contains", [])
        if not isinstance(phrases, list) or any(not isinstance(item, str) or not item.strip() for item in phrases):
            raise ValueError("answer_contains must be a list of nonempty strings")
        for field in ("files_equal", "files_contain"):
            if not isinstance(turn.get(field, {}), dict):
                raise ValueError(f"{field} must be an object")
            for relative, content in turn.get(field, {}).items():
                workspace_path(Path("/disposable-evaluation"), relative)
                if not isinstance(content, str):
                    raise ValueError("file expectations must be strings")
    return data


def _snapshot_member(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo:
    if not (member.isfile() or member.isdir()):
        raise ValueError(f"snapshot member must be a regular file or directory: {member.name}")
    workspace_path(Path(destination), member.name)
    return tarfile.data_filter(member, destination)


def materialize_workspace(root: Path, fixture: dict, repo: Path, timeout: float) -> None:
    commit = fixture.get("snapshot_commit")
    if commit:
        with tempfile.TemporaryFile() as archive:
            subprocess.run(
                ["git", "-C", str(repo), "archive", "--format=tar", f"{commit}^{{commit}}"],
                stdout=archive, check=True, timeout=timeout,
            )
            archive.seek(0)
            with tarfile.open(fileobj=archive) as snapshot:
                snapshot.extractall(root, filter=_snapshot_member)
    for relative, content in fixture["files"].items():
        path = workspace_path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def file_state(root: Path) -> dict[str, str | None]:
    state = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if ".agent" in relative.parts or "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            state[str(relative)] = "symlink:" + str(path.readlink())
        elif path.is_file():
            state[str(relative)] = _file_hash(root, str(relative))
    return state


def check_files(root: Path, turn: dict, before: dict) -> dict:
    checks: dict[str, bool] = {}
    for field in ("files_equal", "files_contain"):
        for relative, expected in turn.get(field, {}).items():
            path = workspace_path(root, relative)
            actual = path.read_text(encoding="utf-8") if path.is_file() else None
            checks[f"{field}:{relative}"] = (
                actual == expected if field == "files_equal" else actual is not None and expected in actual
            )
    if turn.get("unchanged"):
        checks["workspace_unchanged"] = file_state(root) == before
    return checks


def tool_result_facts(events) -> list[dict]:
    requested = {}
    started = {}
    facts = []
    for event in events:
        if event.event_type in {"tool.requested", "tool.started"}:
            call = event.payload["call"]
            requested[call["call_id"]] = call["name"]
            if event.event_type == "tool.started":
                started[call["call_id"]] = int(event.occurred_at.timestamp() * 1_000_000_000)
            continue
        if event.event_type not in {"tool.completed", "tool.failed"}:
            continue
        result = event.payload.get("result")
        if not isinstance(result, dict):
            continue
        fact = {name: result.get(name) for name in ("call_id", "ok", "error_code")}
        fact["commit_state"] = event.payload.get("commit_state")
        call_id = result["call_id"]
        if call_id in requested:
            fact["tool"] = requested[call_id]
        if getattr(event, "occurred_at", None) is not None:
            finished = int(event.occurred_at.timestamp() * 1_000_000_000)
            fact["started_at_unix_nano"] = started.get(call_id, finished)
            fact["finished_at_unix_nano"] = finished
        data = result.get("data")
        if isinstance(data, dict):
            fact.update({name: data[name] for name in ("succeeded", "exit_code", "execution_state") if name in data})
        facts.append(fact)
    return facts


def verified_after_last_edit(events) -> bool:
    requests = {}
    last_edit = None
    verified = False
    for event in events:
        if event.event_type == "tool.requested":
            call = event.payload["call"]
            requests[call["call_id"]] = (event.sequence, call)
        elif event.event_type == "tool.completed":
            result = event.payload["result"]
            if result["call_id"] not in requests or not result.get("ok"):
                continue
            started, call = requests[result["call_id"]]
            if call["name"] in MUTATION_TOOLS:
                last_edit = event.sequence
                verified = False
            elif (
                last_edit is not None and started > last_edit
                and call["name"] == "core.run_command"
                and is_verification_request(call["arguments"])
                and process_result_succeeded(result.get("data") or {})
            ):
                verified = True
    return verified


def model_tool_proposals(events) -> list[dict]:
    """What the model requested, separate from accepted/runtime-executed calls."""
    return [
        {"model_call": event.payload.get("model_call"), "call_id": block.get("call_id"), "tool": block.get("name")}
        for event in events if event.event_type == "llm.completed"
        for block in event.payload.get("message", {}).get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_call"
    ]


def run_independent_tests(root: Path, timeout: float) -> int:
    script = """\
import sys
import unittest

suite = unittest.defaultTestLoader.discover(sys.argv[1], pattern="test_*.py")
if not suite.countTestCases():
    sys.exit("No tests discovered")
sys.exit(not unittest.TextTestRunner().run(suite).wasSuccessful())
"""
    completed = subprocess.run(
        [sys.executable, "-IB", "-c", script, str(root)],
        cwd=root, capture_output=True, timeout=timeout,
    )
    return completed.returncode


async def run_trial(args, fixture: dict, index: int) -> dict:
    trial_started = time.time_ns()
    with tempfile.TemporaryDirectory(prefix="tsm-multiturn-eval-") as directory:
        root = Path(directory).resolve()
        materialize_workspace(root, fixture, getattr(args, "snapshot_repo", Path.cwd()), args.timeout)
        protected_files = {name: _file_hash(root, name) for name in fixture.get("preserve_files", [])}
        if any(digest is None for digest in protected_files.values()):
            raise ValueError("preserve_files must exist in the initial replay workspace")
        app = compose_openai_compatible_engineering_application_from_config(
            args.config, root / ".agent" / "runtime.db", session_history_format=args.history_format,
            history_soft_tokens=getattr(args, "history_soft_tokens", None),
        )
        client = EngineeringAgentClient(root, application_factory=lambda: app)
        await client.start()
        capture = TurnRequestDiagnostics() if args.capture_requests else None
        physical = getattr(app.kernel.dependencies.model, "_provider", app.kernel.dependencies.model)
        exchanges = []
        restore_observer = observe_model_exchanges(physical, exchanges)
        previous_capture = getattr(physical, "_request_diagnostics", None)
        if capture is not None:
            physical._request_diagnostics = capture
        session_id = f"multiturn-eval-{uuid4().hex}"
        reports = []
        try:
            for number, turn in enumerate(fixture["turns"], 1):
                turn_started = time.time_ns()
                before = file_state(root)
                exchange_offset = len(exchanges)
                # Track event offsets per Task: a Conversation reuses its Task.
                offsets = {}
                for report in reports:
                    offsets[report["task_id"]] = report["last_event_sequence"]
                key = capture.start(session_id) if capture is not None else None
                token = capture.activate(key) if key else None
                try:
                    command = await client.submit_user_input(
                        session_id, turn["input"], input_id=f"eval-{uuid4().hex}", workspace=root,
                    )
                    task_id = command.result["task"]["task_id"]
                    result = await client.wait_task(task_id, timeout=args.timeout)
                    events = await app.kernel.dependencies.store.read_events(task_id)
                    current = [event for event in events if event.sequence > offsets.get(task_id, 0)]
                    calls = _tool_names(current)
                    completed_turn = any(event.event_type == "turn.completed" for event in current)
                    # The SDK can expose the previous answer during an approval wait.
                    answer = result.assistant_text if completed_turn else None
                    checks = check_files(root, turn, before)
                    if turn.get("require_agent_verification"):
                        checks["agent_verified_after_last_edit"] = verified_after_last_edit(current)
                    for phrase_index, phrase in enumerate(turn.get("answer_contains", ()), start=1):
                        checks[f"answer_contains:{phrase_index}"] = phrase in (answer or "")
                    checks.update({f"fixture_preserved:{name}": _file_hash(root, name) == digest
                                   for name, digest in protected_files.items()})
                    if turn.get("unchanged"):
                        checks["no_mutation_call"] = not any(name in MUTATION_TOOLS for name in calls)
                    independent_tests = None
                    if turn.get("verify_tests") and args.run_tests:
                        # This executes fixture/model-authored code only with an
                        # explicit operator flag; never use an existing project.
                        exit_code = await asyncio.to_thread(run_independent_tests, root, args.timeout)
                        independent_tests = {"exit_code": exit_code, "passed": exit_code == 0}
                        checks["independent_tests"] = exit_code == 0
                    after = file_state(root)
                    report = {
                        "turn": number, "task_id": task_id, "state": result.state,
                        "started_at_unix_nano": turn_started, "finished_at_unix_nano": time.time_ns(),
                        "last_event_sequence": max((event.sequence for event in events), default=0),
                        "tool_names": calls,
                        "model_tool_proposals": [
                            {"model_call": model_call, **call}
                            for model_call, exchange in enumerate(exchanges[exchange_offset:], 1)
                            for call in exchange["proposed_tools"]
                        ],
                        "model_exchanges": exchanges[exchange_offset:],
                        "tool_results": tool_result_facts(current),
                        "protocol_rejections": [
                            {name: event.payload.get(name) for name in ("turn_id", "reason_code")}
                            for event in current if event.event_type == "tool.protocol_rejected"
                        ],
                        "runtime_failures": [
                            {"event_type": event.event_type,
                             **{name: event.payload[name] for name in (
                                 "error_type", "error_code", "failure_code", "reason", "message",
                             ) if name in event.payload}}
                            for event in current if event.event_type in {"llm.failed", "turn.interrupted"}
                        ],
                        "changed_files": sorted(name for name in before.keys() | after.keys()
                                                if before.get(name) != after.get(name)),
                        "checks": checks, "independent_tests": independent_tests,
                        "automated_checks_passed": all(checks.values()) if checks else None,
                        "turn_completed": completed_turn,
                        "final_answer": answer,
                        "final_answer_accuracy": "requires_human_review",
                        "request_receipts": [
                            {name: value for name, value in event.payload.items()
                             if name.startswith("prompt_") or name in {
                                 "turn_id", "model_call", "finish_reason", "usage", "context_budget", "response_diagnostics",
                             }}
                            for event in current if event.event_type == "llm.completed"
                        ],
                        "verification_not_run": bool(turn.get("verify_tests") and not args.run_tests),
                    }
                    if key:
                        report["request_diagnostics"] = capture.read(key, session_id)
                    reports.append(report)
                    if result.approval or result.clarification:
                        report["blocked_by_explicit_user_action"] = True
                        break  # No blanket approval or simulated user response.
                    if result.state in {"INTERRUPTED", "FAILED", "CANCELLED"}:
                        break  # A transport/runtime failure is not model behavior.
                finally:
                    if token is not None:
                        capture.reset(token)
                    if key:
                        capture.delete(key)
            all_turns_run = len(reports) == len(fixture["turns"])
            case_passed = all_turns_run and all(
                item["turn_completed"] and item["automated_checks_passed"] is not False
                and not item["verification_not_run"] and not item["runtime_failures"]
                and not item.get("blocked_by_explicit_user_action")
                for item in reports
            )
            return {"case": args.fixture.stem, "trial": index, "label": args.label,
                    "started_at_unix_nano": trial_started, "finished_at_unix_nano": time.time_ns(),
                    "case_passed": case_passed, "snapshot_commit": fixture.get("snapshot_commit"),
                    "history_format": args.history_format,
                    "context_policy": app.kernel.dependencies.context_manager.snapshot_data(),
                    "fixture_hash": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
                    "model": {name: value for name, value in
                              app.kernel.dependencies.configuration_metadata.get("model", {}).items()
                              if name in {"provider", "model", "protocol"}},
                    "turns": reports, "all_turns_run": all_turns_run,
                    "task_completion": "not_inferred_from_tool_counts_or_file_changes"}
        finally:
            restore_observer()
            if capture is not None:
                physical._request_diagnostics = previous_capture
            await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, action="append", required=True,
                        help="fixture JSON, repeat for multiple cases")
    parser.add_argument("--snapshot-repo", type=Path, default=Path.cwd(),
                        help="local Git repository containing pinned snapshots")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--history-format", choices=("native", "json"), default="native")
    parser.add_argument("--history-soft-tokens", type=int, help="override shared history allocation; 0 disables only the soft cap")
    parser.add_argument("--report-path", type=Path, help="new output file (refuses to overwrite an existing report)")
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--run-tests", action="store_true", help="explicitly allow executing fixture/model-authored tests")
    parser.add_argument("--langfuse", action="store_true", help="export evaluation traces and scores to Langfuse")
    parser.add_argument("--langfuse-config", type=Path, help="Langfuse YAML file (default: ~/.tsm-agt/langfuse.yaml)")
    parser.add_argument("--capture-requests", action="store_true")
    parser.add_argument("--acknowledge-sensitive-data", action="store_true")
    args = parser.parse_args()
    if not args.allow_model_calls:
        parser.error("real model calls require --allow-model-calls (may incur provider charges)")
    if args.capture_requests and not args.acknowledge_sensitive_data:
        parser.error("capturing request bodies requires --acknowledge-sensitive-data")
    if args.langfuse_config is not None and not args.langfuse:
        parser.error("--langfuse-config requires --langfuse")
    if args.repeat < 1 or args.timeout <= 0:
        parser.error("repeat and timeout must be positive")
    if args.history_soft_tokens is not None and args.history_soft_tokens < 0:
        parser.error("history-soft-tokens must be zero or positive")
    if args.report_path is not None and args.report_path.exists():
        parser.error("report-path already exists; choose a new path")
    fixtures = [(path, load_fixture(path)) for path in args.fixture]
    if len({path.stem for path, _ in fixtures}) != len(fixtures):
        parser.error("fixture filenames must have distinct stems")
    try:
        exporter = LangfuseExporter.from_config(args.langfuse_config) if args.langfuse else None
    except LangfuseExportError as error:
        parser.error(str(error))

    async def run(output):
        failed = False
        reports_to_export = []
        for path, fixture in fixtures:
            case_args = argparse.Namespace(**{**vars(args), "fixture": path})
            passed = 0
            for index in range(1, args.repeat + 1):
                report = await run_trial(case_args, fixture, index)
                print(json.dumps(report, ensure_ascii=False, sort_keys=True), file=output, flush=True)
                if exporter is not None:
                    reports_to_export.append(report)
                if args.report_path:
                    print(json.dumps({
                        "case": path.stem, "trial": index, "label": args.label,
                        "case_passed": report["case_passed"], "all_turns_run": report["all_turns_run"],
                        "turns": [{"turn": item["turn"], "tool_names": item["tool_names"],
                                   "checks": item["checks"], "state": item["state"],
                                   "runtime_failures": item["runtime_failures"]}
                                  for item in report["turns"]],
                    }, ensure_ascii=False), flush=True)
                passed += report["case_passed"]
            print(f"{path.stem}: {passed}/{args.repeat} passed", file=sys.stderr, flush=True)
            failed |= passed != args.repeat
        if exporter is not None:
            try:
                links = await asyncio.to_thread(exporter.export, reports_to_export)
            except LangfuseExportError as error:
                print(f"Langfuse export failed: {error}", file=sys.stderr)
                return 2
            for link in links:
                print(f"Langfuse {link}", file=sys.stderr)
        return int(failed)
    if args.report_path:
        with args.report_path.open("x", encoding="utf-8") as output:
            return asyncio.run(run(output))
    return asyncio.run(run(sys.stdout))


if __name__ == "__main__":
    raise SystemExit(main())
