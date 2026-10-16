#!/usr/bin/env python3
"""Run one disposable, real-model execution-plan evaluation.

This is deliberately an operator command rather than a unit test: the model's
decision to create/update a plan is a behavior to measure against the provider
currently configured for tsm-agt.  It never fabricates a model response.

Use a disposable workspace.  The evaluated prompt may ask the agent to edit it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from tsm_agt.sdk.runtime import EngineeringAgentClient


MUTATION_TOOLS = frozenset({
    "core.apply_patch", "core.apply_patch_v4a", "core.apply_patches",
    "core.delete_file",
})
VERIFICATION_TOOLS = frozenset({
    "core.run_command", "core.process_wait", "code.diagnostics",
})


def _tool_names(events) -> list[str]:
    names: list[str] = []
    for event in events:
        if event.event_type != "tool.requested":
            continue
        call = event.payload.get("call")
        if isinstance(call, dict) and isinstance(call.get("name"), str):
            names.append(call["name"])
    return names


async def evaluate(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"workspace does not exist: {workspace}")
    if not args.allow_workspace_mutations:
        raise SystemExit(
            "refusing to run a real agent without --allow-workspace-mutations; "
            "use a disposable evaluation workspace"
        )

    client = EngineeringAgentClient(workspace)
    await client.start()
    try:
        session_id = f"execution-plan-eval-{uuid4().hex}"
        command = await client.submit_user_input(
            session_id, args.prompt, input_id=f"eval-{uuid4().hex}",
            workspace=workspace,
        )
        task_data = command.result.get("task")
        if not isinstance(task_data, dict):
            raise RuntimeError("evaluation input did not create a task")
        task_id = str(task_data["task_id"])
        result = await client.wait_task(task_id, timeout=args.timeout)
        events = await client.application.kernel._dependencies.store.read_events(task_id)
        tools = _tool_names(events)
        todos = dict(result.todo_list or {})
        todo_items = todos.get("todos", [])
        open_items = [
            item["id"] for item in todo_items
            if item.get("status") in {"pending", "in_progress"}
        ] if isinstance(todo_items, list) else []
        report = {
            "task_id": task_id,
            "state": result.state,
            "status": result.status,
            "tool_names": tools,
            "plan_created": "core.todo_list" in tools,
            "mutation_attempted": any(name in MUTATION_TOOLS for name in tools),
            "verification_attempted": any(
                name in VERIFICATION_TOOLS for name in tools
            ),
            "todo_list": todos or None,
            "open_todo_ids": open_items,
        }
        expectations = {
            "plan_created": args.expect_plan,
            "mutation_attempted": args.expect_mutation,
            "verification_attempted": args.expect_verification,
        }
        failures = [
            name for name, expected in expectations.items()
            if expected and not report[name]
        ]
        report["expectation_failures"] = failures
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 1 if failures else 0
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--allow-workspace-mutations", action="store_true")
    parser.add_argument("--expect-plan", action="store_true")
    parser.add_argument("--expect-mutation", action="store_true")
    parser.add_argument("--expect-verification", action="store_true")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return asyncio.run(evaluate(args))


if __name__ == "__main__":
    raise SystemExit(main())
