#!/usr/bin/env python3
"""Measure real-model read batching in small, disposable, read-only projects.

Reports measure accepted batches and actual calls, not compliance inferred from
prompt text. No completion reviewer, batch-size gate or production transcript is
used. Compare runs using the same model/profile; a small sample is not a promise
of a particular reduction in production calls.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import time

from scripts.evaluate_multiturn_behavior import observe_model_exchanges
from tsm_agt.adapters.builtin.core_tools import CoreReadOnlyToolProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.composition import _physical_model_provider, _platform_workspace_path
from tsm_agt.bootstrap.model_configuration import load_model_configuration
from tsm_agt.core import AgentTurnResult, PromptTemplate, TaskState


FILES = {
    "src/policy.py": "MAX_RETRIES = 3\n\ndef effective_retry_count():\n    return MAX_RETRIES + 1\n",
    "src/worker.py": "from .policy import effective_retry_count\n\ndef worker_attempts():\n    return effective_retry_count()\n",
    "tests/test_worker.py": "from src.worker import worker_attempts\n\ndef test_attempts():\n    assert worker_attempts() == 4\n",
}
CASES = {
    "known-targets": (
        "分析 src/policy.py、src/worker.py 和 tests/test_worker.py，确认重试次数如何从配置传到 worker，"
        "测试验证了什么。只分析，不修改文件。",
        frozenset(FILES),
    ),
    "search-then-read": (
        "定位 effective_retry_count 的实现，说明它如何影响 worker_attempts 的返回值，"
        "以及对应测试验证了什么。只分析，不修改文件。",
        frozenset(FILES),
    ),
    "location-only": (
        "MAX_RETRIES 定义在哪个文件、哪一行？只给出位置，不分析实现，不修改文件。",
        frozenset(),
    ),
}


def summarize(events, exchanges, final_answer: str | None, required_reads) -> dict:
    calls = [event.payload["call"] for event in events if event.event_type == "tool.requested"]
    results = {event.payload["result"]["call_id"]: event.payload["result"]
               for event in events if event.event_type in {"tool.completed", "tool.failed"}}
    batches = [event.payload["tool_call_count"] for event in events
               if event.event_type == "conversation.model_response_recorded"
               and event.payload["tool_call_count"] > 0]
    reads = [call for call in calls if call["name"] == "core.read_file"]
    read_paths = {
        results[call["call_id"]]["data"].get("path") for call in reads
        if call["call_id"] in results and results[call["call_id"]].get("ok")
    }
    identities = Counter((call["name"], json.dumps(call["arguments"], sort_keys=True)) for call in calls)
    failures = [event.event_type for event in events
                if event.event_type in {"tool.failed", "tool.protocol_rejected", "llm.failed"}]
    return {
        "model_calls": len(exchanges),
        "tool_calls": len(calls), "tool_names": [call["name"] for call in calls],
        "tool_batch_sizes": batches,
        "single_tool_batches": sum(size == 1 for size in batches),
        "tool_requests": [{"tool": call["name"], "arguments": call["arguments"]} for call in calls],
        "read_calls": len(reads), "read_paths": sorted(path for path in read_paths if path),
        "exact_repeated_calls": sum(count - 1 for count in identities.values()),
        "all_required_files_read": required_reads <= read_paths,
        "failures": failures,
        "protocol_rejections": [
            {"reason_code": event.payload.get("reason_code"),
             "detail": str(event.payload.get("detail", ""))[:1000]}
            for event in events if event.event_type == "tool.protocol_rejected"
        ],
        "usage": {key: sum(exchange["usage"][key] for exchange in exchanges)
                  for key in ("input_tokens", "output_tokens")},
        "final_answer": final_answer,
        "answer_accuracy": "requires_human_review",
    }


async def run_case(args, cfg, case: str, trial: int) -> dict:
    prompt, required_reads = CASES[case]
    template = PromptTemplate.default()
    model = _physical_model_provider(
        base_url=cfg.base_url, model=cfg.model, api_key=cfg.api_key,
        protocol=cfg.protocol, anthropic_version=cfg.anthropic_version,
        timeout_seconds=min(cfg.timeout_seconds, args.timeout),
        output_token_parameter=cfg.effective_output_token_parameter,
        strict_tool_schema=cfg.strict_tool_schema, streaming=cfg.streaming,
        open_api_mode=cfg.open_api_mode, reasoning_effort=cfg.reasoning_effort,
        capability_overrides=cfg.capability_overrides,
        request_parameters=cfg.request_parameters,
        send_output_token_limit=cfg.send_output_token_limit,
        max_output_tokens=cfg.max_output_tokens,
    )
    tools = CoreReadOnlyToolProvider()
    app = compose_fixture_application(
        model_adapter=model, tool_adapters=(tools,),
        workspace_path_adapter=_platform_workspace_path(),
    )
    app.kernel._dependencies = replace(app.kernel.dependencies, prompt_template=template)
    exchanges = []
    restore = observe_model_exchanges(model, exchanges)
    report = {
        "case": case, "trial": trial, "label": args.label,
        "model": cfg.model, "profile": cfg.profile_id,
        "prompt_revision": template.revision, "prompt_hash": template.manifest().manifest_hash,
        "tool_contract_hash": hashlib.sha256(json.dumps(
            [(spec.name, spec.model_description(), spec.parameters) for spec in await tools.list_tools()],
            sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest(),
        "fixture_hash": hashlib.sha256(json.dumps(FILES, sort_keys=True).encode()).hexdigest(),
        "batch_size_gate": False,
    }
    started = time.monotonic()
    await app.registry.start_all()
    try:
        with tempfile.TemporaryDirectory(prefix="tsm-read-batching-") as directory:
            root = Path(directory).resolve()
            for name, content in FILES.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            task = await app.kernel.create_task(prompt, root, "read-batching-eval")
            for state in (TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                          TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING):
                task = await app.kernel.transition_task(task.task_id, state, state.value)
            result = None
            try:
                async with asyncio.timeout(args.timeout):
                    result = await app.kernel.run_agent_turn(
                        task.task_id, prompt, max_model_calls=8, max_tool_calls=16,
                        max_output_tokens=8192,
                    )
            except Exception as error:
                # Do not export provider error bodies, endpoints or credentials.
                report["error_type"] = type(error).__name__
            events = await app.kernel.dependencies.store.read_events(task.task_id)
            report["result_type"] = type(result).__name__
            answer = result.assistant_message.text if isinstance(result, AgentTurnResult) else None
            report.update(summarize(events, exchanges, answer, required_reads))
            report["fixture_unchanged"] = all((root / name).read_text() == content for name, content in FILES.items())
            report["valid_run"] = (bool(answer) and not report.get("error_type")
                                   and "llm.failed" not in report["failures"])
            report["checks_passed"] = (report["valid_run"] and report["fixture_unchanged"]
                                       and report["all_required_files_read"])
    finally:
        restore()
        await app.registry.stop_all()
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model-profile")
    parser.add_argument("--case", choices=(*CASES, "all"), default="all")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--allow-model-calls", action="store_true")
    args = parser.parse_args()
    if not args.allow_model_calls:
        parser.error("real model calls require --allow-model-calls")
    if args.repeat < 1 or args.timeout <= 0:
        parser.error("repeat and timeout must be positive")
    cfg = load_model_configuration(args.config, profile_id=args.model_profile)

    async def evaluate(output):
        passed = True
        for case in CASES if args.case == "all" else (args.case,):
            for trial in range(1, args.repeat + 1):
                report = await run_case(args, cfg, case, trial)
                print(json.dumps(report, ensure_ascii=False), file=output, flush=True)
                print(json.dumps({key: report[key] for key in (
                    "label", "case", "checks_passed", "model_calls", "tool_calls", "tool_batch_sizes",
                )}, ensure_ascii=False), flush=True)
                passed &= report["checks_passed"]
                if report.get("error_type"):
                    return 1  # A provider failure is not evidence about model behavior.
        return 0 if passed else 1

    with args.report_path.open("x", encoding="utf-8") as output:
        return asyncio.run(evaluate(output))


if __name__ == "__main__":
    raise SystemExit(main())
