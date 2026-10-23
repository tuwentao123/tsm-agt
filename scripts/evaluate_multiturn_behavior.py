#!/usr/bin/env python3
"""Opt-in real-model multi-turn comparisons in fresh disposable workspaces.

Nothing is submitted to an existing conversation. Credentials are loaded from
the operator's env file, never copied into the disposable workspace or report.
Reports distinguish observed calls, file assertions, independent tests and the
final answer; they never equate a mutation with completed work.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from scripts.evaluate_execution_plan_behavior import _file_hash, _tool_names, MUTATION_TOOLS
from tsm_agt.adapters.openai_compatible.request_diagnostics import TurnRequestDiagnostics
from tsm_agt.bootstrap import compose_openai_compatible_engineering_application_from_env
from tsm_agt.sdk.runtime import EngineeringAgentClient
from tsm_agt.ports import ModelCallPurpose, ModelStreamCompleted, ToolCallBlock


def observe_model_exchanges(provider, observations: list):
    """Observe only this disposable app, including non-final tool responses.

    The production event log intentionally does not duplicate intermediate
    response bodies. Do not mistake absent llm.completed rows for absent calls.
    No request text, tool arguments, credentials or response prose is recorded.
    """
    original_complete = provider.complete
    original_stream = getattr(provider, "stream_complete", None)
    seen = set()

    def record(request, response):
        if request.purpose is not ModelCallPurpose.AGENT_TURN:
            return
        identity = (request.turn_id, response.message.message_id)
        if identity in seen:
            return  # A streaming fallback may delegate to complete().
        seen.add(identity)
        observations.append({
            "turn_id": request.turn_id, "finish_reason": response.finish_reason.value,
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
        response = await original_complete(request)
        record(request, response)
        return response

    async def stream(request):
        async for item in original_stream(request):
            if isinstance(item, ModelStreamCompleted):
                record(request, item.response)
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
    path = (root / relative).resolve()
    if (Path(relative).is_absolute() or path == root or not path.is_relative_to(root)
            or not Path(relative).parts or Path(relative).parts[0] in {".agent", ".env", ".git"}):
        raise ValueError("fixture paths must be relative, inside the disposable workspace and not runtime/config paths")
    return path


def load_fixture(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ValueError("fixture requires a files object")
    if not isinstance(data.get("turns"), list) or not data["turns"]:
        raise ValueError("fixture requires nonempty turns")
    for relative, content in data["files"].items():
        workspace_path(Path("/disposable-evaluation"), relative)
        if not isinstance(content, str):
            raise ValueError("fixture file content must be text")
    preserved = data.get("preserve_files", [])
    if not isinstance(preserved, list) or any(name not in data["files"] for name in preserved):
        raise ValueError("preserve_files must name initial fixture files")
    for turn in data["turns"]:
        if not isinstance(turn, dict) or not isinstance(turn.get("input"), str) or not turn["input"].strip():
            raise ValueError("each turn requires an input string")
        for field in ("files_equal", "files_contain"):
            if not isinstance(turn.get(field, {}), dict):
                raise ValueError(f"{field} must be an object")
            for relative, content in turn.get(field, {}).items():
                workspace_path(Path("/disposable-evaluation"), relative)
                if not isinstance(content, str):
                    raise ValueError("file expectations must be strings")
    return data


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
    facts = []
    for event in events:
        if event.event_type not in {"tool.completed", "tool.failed"}:
            continue
        result = event.payload.get("result")
        if not isinstance(result, dict):
            continue
        fact = {name: result.get(name) for name in ("call_id", "ok", "error_code")}
        fact["commit_state"] = event.payload.get("commit_state")
        data = result.get("data")
        if isinstance(data, dict):
            fact.update({name: data[name] for name in ("succeeded", "exit_code", "execution_state") if name in data})
        facts.append(fact)
    return facts


def model_tool_proposals(events) -> list[dict]:
    """What the model requested, separate from accepted/runtime-executed calls."""
    return [
        {"model_call": event.payload.get("model_call"), "call_id": block.get("call_id"), "tool": block.get("name")}
        for event in events if event.event_type == "llm.completed"
        for block in event.payload.get("message", {}).get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_call"
    ]


async def run_trial(args, fixture: dict, index: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="tsm-multiturn-eval-") as directory:
        root = Path(directory).resolve()
        for relative, content in fixture["files"].items():
            path = workspace_path(root, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        app = compose_openai_compatible_engineering_application_from_env(
            args.env_file, root / ".agent" / "runtime.db", session_history_format=args.history_format,
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
        protected_files = {name: _file_hash(root, name) for name in fixture.get("preserve_files", [])}
        try:
            for number, turn in enumerate(fixture["turns"], 1):
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
                    checks = check_files(root, turn, before)
                    checks.update({f"fixture_preserved:{name}": _file_hash(root, name) == digest
                                   for name, digest in protected_files.items()})
                    if turn.get("unchanged"):
                        checks["no_mutation_call"] = not any(name in MUTATION_TOOLS for name in calls)
                    independent_tests = None
                    if turn.get("verify_tests") and args.run_tests:
                        # This executes fixture/model-authored code only with an
                        # explicit operator flag; never use an existing project.
                        completed = await asyncio.to_thread(
                            subprocess.run,
                            [sys.executable, "-I", "-m", "unittest", "discover", "-s", str(root), "-p", "test_*.py"],
                            cwd=root, capture_output=True, timeout=args.timeout,
                        )
                        independent_tests = {"exit_code": completed.returncode,
                                             "passed": completed.returncode == 0}
                        checks["independent_tests"] = completed.returncode == 0
                    after = file_state(root)
                    report = {
                        "turn": number, "task_id": task_id, "state": result.state,
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
                        # SDK may expose the previous archived answer while a
                        # new Turn awaits approval. It is not this Turn's final.
                        "final_answer": (result.assistant_text if any(
                            event.event_type == "turn.completed" for event in current
                        ) else None),
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
            return {"trial": index, "label": args.label, "history_format": args.history_format,
                    "context_policy": app.kernel.dependencies.context_manager.snapshot_data(),
                    "fixture_hash": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
                    "model": {name: value for name, value in
                              app.kernel.dependencies.configuration_metadata.get("model", {}).items()
                              if name in {"provider", "model", "protocol"}},
                    "turns": reports, "all_turns_run": len(reports) == len(fixture["turns"]),
                    "task_completion": "not_inferred_from_tool_counts_or_file_changes"}
        finally:
            restore_observer()
            if capture is not None:
                physical._request_diagnostics = previous_capture
            await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--history-format", choices=("native", "json"), default="native")
    parser.add_argument("--history-soft-tokens", type=int, help="override shared history allocation; 0 disables only the soft cap")
    parser.add_argument("--report-path", type=Path, help="new output file (refuses to overwrite an existing report)")
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--run-tests", action="store_true", help="explicitly allow executing fixture/model-authored tests")
    parser.add_argument("--capture-requests", action="store_true")
    parser.add_argument("--acknowledge-sensitive-data", action="store_true")
    args = parser.parse_args()
    if not args.allow_model_calls:
        parser.error("real model calls require --allow-model-calls (may incur provider charges)")
    if args.capture_requests and not args.acknowledge_sensitive_data:
        parser.error("capturing request bodies requires --acknowledge-sensitive-data")
    if args.repeat < 1 or args.timeout <= 0:
        parser.error("repeat and timeout must be positive")
    if args.history_soft_tokens is not None and args.history_soft_tokens < 0:
        parser.error("history-soft-tokens must be zero or positive")
    if args.report_path is not None and args.report_path.exists():
        parser.error("report-path already exists; choose a new path")
    fixture = load_fixture(args.fixture)

    async def run(output):
        failed = False
        for index in range(1, args.repeat + 1):
            report = await run_trial(args, fixture, index)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True), file=output, flush=True)
            if args.report_path:
                print(json.dumps({"trial": index, "label": args.label,
                                  "all_turns_run": report["all_turns_run"],
                                  "turns": [{"turn": item["turn"], "tool_names": item["tool_names"],
                                             "checks": item["checks"], "state": item["state"],
                                             "runtime_failures": item["runtime_failures"]}
                                            for item in report["turns"]]}, ensure_ascii=False), flush=True)
            failed |= (not report["all_turns_run"] or any(
                item["automated_checks_passed"] is False or item["verification_not_run"]
                or item["runtime_failures"]
                or item.get("blocked_by_explicit_user_action") for item in report["turns"]
            ))
        return int(failed)
    if args.report_path:
        with args.report_path.open("x", encoding="utf-8") as output:
            return asyncio.run(run(output))
    return asyncio.run(run(sys.stdout))


if __name__ == "__main__":
    raise SystemExit(main())
