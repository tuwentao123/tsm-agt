#!/usr/bin/env python3
"""Measure production read projection in a disposable workspace; --live is opt-in.

Only authored fixture text is sent to the model. Reports contain neither config
credentials nor arbitrary exception bodies. Offline checks do not claim model
behavior, token billing, parallel execution, or reduced filesystem I/O.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.bootstrap.composition import _physical_model_provider, _platform_workspace_path
from tsm_agt.bootstrap.model_configuration import load_model_configuration
from tsm_agt.core import ContextWindowManager, PromptTemplate
from tsm_agt.core.read_result_reuse import project_read_results
from tsm_agt.ports import (
    AdapterContext, Message, MessageRole, ModelRequest, TextBlock, ToolCall,
    ToolCallBlock, ToolInvocationContext, ToolResultBlock,
)

BODY = "MARKER = 'READ_REUSE_OK'\n" + "".join(
    f"value_{i:03d} = 'fixture source text'\n" for i in range(100)
)


def exchange(call, result):
    return (
        Message("call-" + call.call_id, MessageRole.ASSISTANT, (ToolCallBlock(call),)),
        Message("result-" + call.call_id, MessageRole.TOOL, (ToolResultBlock(result),)),
    )


def result_views(messages):
    return [{"call_id": b.result.call_id, "data": b.result.data,
             "truncated": b.result.truncated}
            for m in messages for b in m.content if isinstance(b, ToolResultBlock)]


async def evaluate(args):
    report = {"schema_version": 1, "kind": "real_file_read_projection",
              "filesystem_cache": False, "live_model": {"attempted": False}}
    paths, provider = _platform_workspace_path(), CoreReadOnlyToolProvider()
    adapter = AdapterContext(config={}, emit_event=lambda *_: None)
    await paths.start(adapter)
    await provider.start(adapter)
    try:
        with tempfile.TemporaryDirectory(prefix="read-reuse-") as directory:
            root = Path(directory)
            path = root / "source.py"
            path.write_text(BODY, encoding="utf-8")

            async def read(call_id):
                call = ToolCall(call_id, "core.read_file", {"path": "source.py"})
                result = await provider.invoke(call, ToolInvocationContext(
                    invocation_id=call_id, task_id="evaluation", turn_id="turn",
                    workspace=root, workspace_path=paths,
                    deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
                ))
                if not result.ok:
                    raise AssertionError("fixture file read failed")
                return exchange(call, result)

            user = Message("user", MessageRole.USER, (TextBlock(
                "The read results below are successive reads of one file. "
                "Use the retained source body and answer only the value of MARKER."
            ),))
            canonical = (user, *await read("a"), *await read("b"), *await read("c"))
            projection = project_read_results(canonical, "evaluation")
            template = PromptTemplate.default()
            tools = tuple(t for t in await provider.list_tools() if t.name == "core.read_file")
            before = ContextWindowManager.estimate_input_tokens(canonical, tools, template)
            after = ContextWindowManager.estimate_input_tokens(
                canonical, tools, template, read_reuse_task_id="evaluation",
            )
            promoted = project_read_results((user, *canonical[3:]), "evaluation")
            assert promoted.stats.references == (("c", "b"),)
            path.write_text(BODY.replace("READ_REUSE_OK", "CHANGED_VALUE"), encoding="utf-8")
            changed = project_read_results((*canonical, *await read("d")), "evaluation")
            assert result_views(changed.messages)[-1]["data"]["content"] != BODY
            assert all(row["data"]["content"] == BODY for row in result_views(canonical))
            report.update({
                "fixture_read_count": 4,
                "three_identical_reads": projection.stats.event_data(),
                "estimated_input_tokens": {"before": before, "after": after,
                    "method": "utf8-bytes-div-4-conservative-v1", "provider_billing": False},
                "promoted_source_references": promoted.stats.event_data()["references"],
                "changed_version_returns_full_body": True,
                "canonical_results_keep_full_bodies": True,
                "model_view_before": result_views(canonical),
                "model_view_after": result_views(projection.messages),
            })
            if args.live:
                cfg = load_model_configuration(args.env_file, os.environ)
                model = _physical_model_provider(
                    base_url=cfg.base_url, model=cfg.model, api_key=cfg.api_key,
                    protocol=cfg.protocol, anthropic_version=cfg.anthropic_version,
                    timeout_seconds=args.timeout,
                    output_token_parameter=cfg.effective_output_token_parameter,
                    strict_tool_schema=cfg.strict_tool_schema, streaming=False,
                    open_api_mode=cfg.open_api_mode, reasoning_effort=cfg.reasoning_effort,
                )
                report["live_model"] = {"attempted": True, "model": cfg.model,
                                        "valid_run": False, "behavior_verified": False}
                await model.start(adapter)
                try:
                    request = ModelRequest(
                        "read-reuse-evaluation", template.assemble(projection.messages, tools).messages,
                        max_output_tokens=512, tools=tools, allow_tool_calls=False,
                    )
                    async with asyncio.timeout(args.timeout + 5):
                        response = await model.complete(request)
                    answer = response.message.text.strip()
                    report["live_model"].update(valid_run=True,
                        behavior_verified="READ_REUSE_OK" in answer, answer=answer)
                except Exception as error:
                    report["live_model"].update(error_type=type(error).__name__)
                    if hasattr(error, "reason_code"):
                        report["live_model"]["reason_code"] = error.reason_code
                finally:
                    await model.stop(datetime.now(timezone.utc) + timedelta(seconds=5))
    finally:
        await provider.stop(datetime.now(timezone.utc) + timedelta(seconds=5))
        await paths.stop(datetime.now(timezone.utc) + timedelta(seconds=5))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(args.report),
                      "reuse": report["three_identical_reads"],
                      "live_model": report["live_model"]}, ensure_ascii=False))
    return 0 if not args.live or report["live_model"].get("behavior_verified") else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--timeout", type=float, default=25)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    raise SystemExit(asyncio.run(evaluate(args)))
