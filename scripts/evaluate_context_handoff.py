#!/usr/bin/env python3
"""Real-model repair in a disposable fixture, with optional compaction stress.

Uses production read/patch/process/Skill tools and the conversation loop, without
business MCP connections or private project content. Approval is limited to the
fixture source and its documented check. Reports distinguish the model's actual
command execution from the evaluator's independent correctness assertion.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider, CoreProcessToolProvider, CoreWorkspaceMutationToolProvider
from tsm_agt.adapters.builtin.skill_tools import CoreSkillToolProvider
from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.adapters.local_sandbox import LocalWorkspaceSandbox
from tsm_agt.adapters.rule_based_artifact_read import RuleBasedArtifactReadPolicy
from tsm_agt.adapters.skill_catalog import SkillCatalog
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.composition import _physical_model_provider, _platform_workspace_path
from tsm_agt.bootstrap.model_configuration import load_model_configuration
from tsm_agt.core import AgentTurnSuspended, ApprovalDecision, ContextWindowManager, TaskState
from tsm_agt.core.skills import SkillRegistry


class FixturePatchProvider(CoreWorkspaceMutationToolProvider):
    async def list_tools(self):
        return tuple(tool for tool in await super().list_tools() if tool.name == "core.apply_patch")


async def evaluate(args):
    cfg = load_model_configuration(args.config, profile_id=args.profile)
    model = _physical_model_provider(
        base_url=cfg.base_url, model=cfg.model, api_key=cfg.api_key, protocol=cfg.protocol,
        anthropic_version=cfg.anthropic_version, timeout_seconds=45,
        output_token_parameter=cfg.effective_output_token_parameter,
        strict_tool_schema=cfg.strict_tool_schema, streaming=cfg.streaming, open_api_mode=cfg.open_api_mode,
        reasoning_effort=cfg.reasoning_effort, capability_overrides=cfg.capability_overrides,
        request_parameters=cfg.request_parameters, send_output_token_limit=cfg.send_output_token_limit,
        max_output_tokens=cfg.max_output_tokens,
    )
    report = {"kind": "real_model_context_handoff", "model": cfg.model,
              "forced_compaction": args.force_compaction, "valid_run": False}
    with tempfile.TemporaryDirectory(prefix="tsm-handoff-eval-") as directory:
        root = Path(directory)
        workspace = root / "project"
        workspace.mkdir()
        (workspace / "calculator.py").write_text("def clamp(value):\n    return value\n")
        (workspace / "check.py").write_text("from calculator import clamp\nassert clamp(-3) == 0\nassert clamp(5) == 5\nprint('FIXTURE_CHECK_PASSED')\n")
        (workspace / "README.md").write_text(
            "# Fixture\nclamp must return zero for negative input and preserve nonnegative input.\n"
            f"Check entry: {sys.executable} check.py (cwd: this project). Only calculator.py needs a change.\n")
        registry = SkillRegistry()
        catalog = SkillCatalog(UserSkillsStore(root / "skills"))
        skill_tools = CoreSkillToolProvider(registry, catalog.user_store, catalog=catalog)
        paths = _platform_workspace_path()
        app = compose_fixture_application(
            model_adapter=model, workspace_path_adapter=paths,
            sandbox_adapter=LocalWorkspaceSandbox(paths), enable_working_memory=True,
            artifact_read_policy_adapter=RuleBasedArtifactReadPolicy(),
            context_manager=ContextWindowManager(semantic_compaction=True,
                latency_soft_input_tokens=1 if args.force_compaction else 0),
            tool_adapters=(CoreReadOnlyToolProvider(), FixturePatchProvider(), CoreProcessToolProvider(), skill_tools),
        )
        app.kernel._dependencies = replace(app.kernel.dependencies, skill_registry=registry, skill_catalog=catalog)
        task = None
        try:
            await app.start()
            task = await app.kernel.create_task("Fixture clamp repair", workspace)
            for state in (TaskState.INTAKE, TaskState.RESOLVING_PROJECT, TaskState.SELECTING_EXTENSIONS,
                          TaskState.ROUTING, TaskState.EXECUTING):
                await app.kernel.transition_task(task.task_id, state, "evaluation setup")
            async with asyncio.timeout(args.timeout):
                outcome = await app.kernel.run_agent_turn(task.task_id,
                    "修复这个小项目的 clamp：负数返回 0，非负数保持原值。请按项目现有约定完成。",
                    max_model_calls=14, max_tool_calls=40)
                while isinstance(outcome, AgentTurnSuspended):
                    stored = await app.kernel.get_task(task.task_id)
                    checkpoint = stored.active_agent_checkpoint
                    # The frozen pending call is the one being approved. No
                    # broad approval for arbitrary commands or outside paths.
                    calls = checkpoint.get("pending_tool_calls", [])
                    call = calls[0] if calls else {}
                    name, arguments = call.get("name"), call.get("arguments", {})
                    argv = arguments.get("argv", [])
                    allowed = ((name == "core.apply_patch" and arguments.get("path") == "calculator.py")
                               or (name == "core.run_command" and argv in (
                                   [sys.executable, "check.py"], ["python3", "check.py"], ["python", "check.py"])
                                   and arguments.get("cwd", ".") in (".", str(workspace))))
                    if not allowed:
                        report.setdefault("denied_extra_tools", []).append(name)
                    outcome = await app.kernel.resolve_agent_approval(
                        outcome.approval_request_id,
                        ApprovalDecision.APPROVE if allowed else ApprovalDecision.DENY,
                        "Disposable fixture source/check only; other actions are outside this evaluation",
                    )
                report["outcome_type"] = type(outcome).__name__
                report["valid_run"] = True
        except Exception as error:
            # Never serialize credentials, provider response bodies or arbitrary
            # exception text from the configured transport.
            report["error_type"] = type(error).__name__
            report["reason_code"] = getattr(error, "reason_code", None)
            failure = getattr(error, "failure", None)
            diagnostics = getattr(failure, "diagnostics", {})
            report["http_status"] = diagnostics.get("status_code") if isinstance(diagnostics, dict) else None
        finally:
            if task is not None:
                current = await app.kernel.get_task(task.task_id)
                events = await app.kernel.dependencies.store.read_events(task.task_id)
                rows = list(current.tool_executions.values())
                commands = [row for row in rows if row.call.name == "core.run_command" and row.result is not None]
                last_edit = max((mutation.created_at for mutation in current.mutation_journal), default=None)
                report.update({
                    "transport_attempt_events": sum(e.event_type == "model.attempt_started" for e in events),
                    "main_model_responses": sum(e.event_type == "conversation.model_response_recorded" for e in events),
                    "summary_model_calls": sum(e.payload.get("model_calls", 0) for e in events if e.event_type == "context.summary_completed"),
                    "compactions": sum(e.event_type == "context.compacted" for e in events),
                    "skill_loads": sum(row.call.name == "skill.load" for row in rows),
                    "file_reads": sum(row.call.name == "core.read_file" for row in rows),
                    "patches": len(current.mutation_journal),
                    "agent_commands": [{"ok": row.result.ok,
                        "exit_code": (row.result.data or {}).get("exit_code"),
                        "after_last_edit": last_edit is not None and row.started_at > last_edit,
                        "output_observed": (row.result.data or {}).get("output_observed")}
                        for row in commands],
                    "active_skill_retention": [e.payload.get("retained_call_ids", []) for e in events
                                               if e.event_type == "context.active_skills_retained"],
                })
                # Independent oracle: this does not count as an agent command.
                oracle = subprocess.run([sys.executable, "check.py"], cwd=workspace, capture_output=True, timeout=5)
                report["independent_fixture_check_passed"] = oracle.returncode == 0
                report["agent_checked_after_edit"] = any(
                    row["ok"] and row["exit_code"] == 0 and row["after_last_edit"] and row["output_observed"]
                    for row in report["agent_commands"])
            await app.registry.stop_all()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if (report.get("outcome_type") == "AgentTurnResult" and report.get("agent_checked_after_edit")
                 and report.get("independent_fixture_check_passed")) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--profile", help="Use an existing configured model profile.")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--force-compaction", action="store_true")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    raise SystemExit(asyncio.run(evaluate(args)))
