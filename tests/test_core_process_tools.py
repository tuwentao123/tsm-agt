from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreProcessToolProvider, CoreReadOnlyToolProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    ApprovalDecision,
    ApprovalRequired,
    BackgroundProcessState,
    ProjectTrustLevel,
    TaskState,
)
from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    SandboxDecision,
    SandboxRequest,
    ToolCall,
    ToolIdempotency,
    ToolRisk,
    RuntimeStorePort,
)


class _AllowProcessSandbox:
    descriptor = AdapterDescriptor(
        "fixture.process-tool-sandbox", "0.1.0", "SandboxPort", "1.0",
        frozenset({"workspace-process"}),
    )

    async def start(self, context: AdapterContext) -> None:
        pass

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY)

    async def stop(self, deadline) -> None:
        pass

    async def authorize(self, request: SandboxRequest) -> SandboxDecision:
        return SandboxDecision(True, "test permits process")


class CoreProcessToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.application = compose_fixture_application(
            sandbox_adapter=_AllowProcessSandbox(),
            tool_adapters=(CoreReadOnlyToolProvider(), CoreProcessToolProvider()),
        )
        await self.application.registry.start_all()
        await self.application.kernel.set_project_trust(
            Path(self.temporary.name), ProjectTrustLevel.TRUSTED_BUILD
        )
        self.task = await self._executing_task("task-process-tools")

    async def asyncTearDown(self) -> None:
        await self.application.registry.stop_all()
        self.temporary.cleanup()

    async def _executing_task(self, task_id: str):
        task = await self.application.kernel.create_task(
            "manage a background process", Path(self.temporary.name), task_id
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await self.application.kernel.transition_task(
                task.task_id, state, state.value
            )
        return task

    async def _approve_call(self, call: ToolCall, turn_id: str = "turn-command"):
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(self.task.task_id, turn_id, call)
        request = caught.exception.request
        return await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approved exact structured argv",
        )

    async def test_run_command_contract_is_r2_structured_and_non_idempotent(self) -> None:
        tools = {tool.name: tool for tool in await self.application.kernel.list_tools()}
        spec = tools["core.run_command"]
        self.assertEqual(spec.risk, ToolRisk.R2)
        self.assertEqual(spec.idempotency, ToolIdempotency.NON_IDEMPOTENT)
        self.assertEqual(spec.parameters["properties"]["argv"]["type"], "array")

    async def test_run_command_foreground_has_no_shell_and_bounded_output(self) -> None:
        marker = Path(self.temporary.name) / "must-not-exist"
        script = Path(self.temporary.name) / "foreground_command.py"
        script.write_text(
            "import sys\nprint(sys.argv[1])\nprint('x' * 100)\n"
            "print('problem', file=sys.stderr)\n", encoding="utf-8"
        )
        result = await self._approve_call(ToolCall(
            "call-command-fg", "core.run_command", {
                "argv": [sys.executable, script.name, "$(touch must-not-exist)"],
                "mode": "foreground", "max_output_bytes": 40,
            },
        ))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["mode"], "foreground")
        self.assertEqual(result.data["status"], "exited")
        self.assertEqual(result.data["exit_code"], 0)
        self.assertIn("$(touch must-not-exist)", result.data["stdout"]["text"])
        self.assertTrue(result.data["stdout"]["truncated"])
        self.assertEqual(result.data["stderr"]["text"], "problem\n")
        self.assertFalse(marker.exists())

        events = await self.application.registry.require(
            RuntimeStorePort
        ).read_events(self.task.task_id)
        tool_started = next(
            event for event in events
            if event.event_type == "tool.started"
            and event.payload.get("call", {}).get("call_id") == "call-command-fg"
        )
        invocation_id = tool_started.payload["invocation_id"]
        process_events = tuple(
            event for event in events
            if event.event_type in {"process.started", "process.exited"}
            and event.payload.get("invocation_id") == invocation_id
        )
        self.assertEqual(
            [event.event_type for event in process_events],
            ["process.started", "process.exited"],
        )
        projection = await self.application.kernel.get_flow_projection(
            self.task.task_id
        )
        tool = next(
            node for node in projection.nodes
            if node.kind.value == "tool" and node.label == "core.run_command"
        )
        process = next(
            node for node in projection.nodes
            if node.kind.value == "process" and node.parent_id == tool.node_id
        )
        self.assertIn(
            (tool.node_id, process.node_id, "caused_by"),
            {
                (edge.from_node, edge.to_node, edge.relation.value)
                for edge in projection.edges
            },
        )
        diagnostic = projection.inspect_node(tool.node_id)
        self.assertIn(
            "process.result-summary", {fact.code for fact in diagnostic.facts}
        )
        rendered = str(diagnostic.to_data())
        self.assertNotIn("$(touch must-not-exist)", rendered)
        self.assertNotIn("problem", rendered)

    async def test_run_command_background_returns_managed_process(self) -> None:
        script = Path(self.temporary.name) / "background_command.py"
        script.write_text(
            "import time\nprint('service-ready', flush=True)\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        result = await self._approve_call(ToolCall(
            "call-command-bg", "core.run_command", {
                "argv": [
                    sys.executable, script.name,
                ],
                "mode": "background", "max_lifetime_seconds": 30,
                "stop_on_task_end": True,
            },
        ))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["mode"], "background")
        self.assertEqual(result.data["state"], "RUNNING")
        process_id = result.data["process_id"]
        self.assertNotIn("pid", result.data)

        collected = ""
        cursor = 0
        for attempt in range(100):
            logs = await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-bg-logs",
                ToolCall(f"call-bg-logs-{attempt}", "core.process_logs", {
                    "process_id": process_id, "stdout_cursor": cursor,
                }),
            )
            self.assertTrue(logs.ok)
            collected += logs.data["stdout"]["text"]
            cursor = logs.data["stdout"]["next_cursor"]
            if "service-ready" in collected:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(collected, "service-ready\n")
        await self.application.kernel.stop_background_process(
            self.task.task_id, process_id, grace_seconds=0.01
        )

    async def test_run_command_rejects_workspace_escape_and_credential_environment(self) -> None:
        escaped = await self._approve_call(ToolCall(
            "call-command-escape", "core.run_command", {
                "argv": [sys.executable, "-V"], "cwd": "..",
            },
        ), "turn-escape")
        self.assertFalse(escaped.ok)
        self.assertEqual(escaped.error_code, "INVALID_PARAM")

        secret = await self._approve_call(ToolCall(
            "call-command-secret", "core.run_command", {
                "argv": [sys.executable, "-V"],
                "environment": {"API_TOKEN": "must-not-forward"},
            },
        ), "turn-secret")
        self.assertFalse(secret.ok)
        self.assertEqual(secret.error_code, "INVALID_PARAM")

    async def test_user_approval_does_not_bypass_deny_all_sandbox(self) -> None:
        denied_application = compose_fixture_application(
            tool_adapters=(CoreProcessToolProvider(),)
        )
        await denied_application.registry.start_all()
        try:
            await denied_application.kernel.set_project_trust(
                Path(self.temporary.name), ProjectTrustLevel.TRUSTED_BUILD
            )
            task = await denied_application.kernel.create_task(
                "sandbox stays authoritative", Path(self.temporary.name), "task-denied"
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
            ):
                task = await denied_application.kernel.transition_task(
                    task.task_id, state, state.value
                )
            call = ToolCall("call-denied", "core.run_command", {
                "argv": [sys.executable, "-m", "unittest"]
            })
            with self.assertRaises(ApprovalRequired) as caught:
                await denied_application.kernel.invoke_tool(
                    task.task_id, "turn-denied", call
                )
            request = caught.exception.request
            result = await denied_application.kernel.resolve_approval(
                task.task_id, request.request_id, request.payload_hash,
                ApprovalDecision.APPROVE, "approve policy action only",
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "PERMISSION_DENIED")
        finally:
            await denied_application.registry.stop_all()

    async def test_status_and_incremental_logs_are_model_visible_r0_tools(self) -> None:
        process = await self.application.kernel.start_background_process(
            self.task.task_id, "turn-start",
            (sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"),
            max_lifetime_seconds=30,
        )
        stdout_cursor = 0
        stderr_cursor = 0
        collected_stdout = ""
        for attempt in range(100):
            logs = await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-logs",
                ToolCall(f"call-logs-{attempt}", "core.process_logs", {
                    "process_id": process.process_id,
                    "stdout_cursor": stdout_cursor,
                    "stderr_cursor": stderr_cursor,
                }),
            )
            self.assertTrue(logs.ok)
            collected_stdout += logs.data["stdout"]["text"]
            stdout_cursor = logs.data["stdout"]["next_cursor"]
            stderr_cursor = logs.data["stderr"]["next_cursor"]
            if "ready" in collected_stdout:
                break
            await asyncio.sleep(0.005)
        self.assertTrue(logs.ok)
        self.assertEqual(collected_stdout, "ready\n")
        status = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-status",
            ToolCall("call-status", "core.process_status", {
                "process_id": process.process_id
            }),
        )
        self.assertTrue(status.ok)
        self.assertEqual(status.data["state"], "RUNNING")
        self.assertNotIn("pid", status.data)
        await self.application.kernel.stop_background_process(
            self.task.task_id, process.process_id, grace_seconds=0.01
        )

    async def test_stop_requires_approval_then_reuses_exact_authorized_call(self) -> None:
        process = await self.application.kernel.start_background_process(
            self.task.task_id, "turn-start",
            (sys.executable, "-c", "import time; time.sleep(30)"),
            max_lifetime_seconds=30,
        )
        call = ToolCall("call-stop", "core.process_stop", {
            "process_id": process.process_id, "grace_seconds": 0.01
        })
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-stop", call
            )
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approved stopping exact task process",
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["state"], BackgroundProcessState.CANCELLED.value)

    async def test_process_tool_cannot_cross_task_boundary(self) -> None:
        process = await self.application.kernel.start_background_process(
            self.task.task_id, "turn-start",
            (sys.executable, "-c", "import time; time.sleep(30)"),
            max_lifetime_seconds=30,
        )
        other = await self._executing_task("task-other")
        result = await self.application.kernel.invoke_tool(
            other.task_id, "turn-status",
            ToolCall("call-status-other", "core.process_status", {
                "process_id": process.process_id
            }),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")
        await self.application.kernel.stop_background_process(
            self.task.task_id, process.process_id, grace_seconds=0.01
        )


if __name__ == "__main__":
    unittest.main()
