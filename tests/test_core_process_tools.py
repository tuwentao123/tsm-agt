from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreProcessToolProvider, CoreReadOnlyToolProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    ApprovalDecision, ApprovalPayloadMismatch,
    ApprovalRequired,
    BackgroundProcessState,
    ProjectTrustLevel,
    TaskState,
)
from tsm_agt.core.tool import InvalidToolArguments
from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    SandboxDecision,
    SandboxRequest,
    ToolCall,
    ToolFactCapability,
    ToolIdempotency,
    ToolRecoveryKind,
    ToolResultMaterialization,
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
        self.assertEqual(spec.parameters["required"], ["argv", "cwd"])

    async def test_run_command_rejects_an_omitted_working_directory_before_approval(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "core.run_command is missing required arguments: cwd",
        ):
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-missing-cwd",
                ToolCall("call-missing-cwd", "core.run_command", {
                    "argv": [sys.executable, "-V"],
                }),
            )

    async def test_run_command_starts_managed_process_and_wait_returns_output(self) -> None:
        marker = Path(self.temporary.name) / "must-not-exist"
        script = Path(self.temporary.name) / "foreground_command.py"
        script.write_text(
            "import sys\nprint(sys.argv[1])\nprint('x' * 100)\n"
            "print('problem', file=sys.stderr)\n", encoding="utf-8"
        )
        result = await self._approve_call(ToolCall(
            "call-command-fg", "core.run_command", {
                "argv": [sys.executable, script.name, "$(touch must-not-exist)"],
                "cwd": ".",
                "max_output_bytes": 40,
            },
        ))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["mode"], "managed")
        self.assertTrue(result.data["started"])
        self.assertEqual(
            result.fact_descriptor.materialization,
            ToolResultMaterialization.ACTION_RECEIPT,
        )
        self.assertIn(
            ToolFactCapability.REPORT_ACTION,
            result.fact_descriptor.capabilities,
        )
        waited = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-command-wait",
            ToolCall("call-command-wait", "core.process_wait", {
                "process_id": result.data["process_id"], "wait_seconds": 1,
            }),
        )
        self.assertTrue(waited.ok)
        self.assertEqual(waited.data["status"], "exited")
        self.assertEqual(
            waited.fact_descriptor.materialization,
            ToolResultMaterialization.RUNTIME_OBSERVATION,
        )
        self.assertEqual(waited.data["exit_code"], 0)
        self.assertIn("$(touch must-not-exist)", waited.data["stdout"]["text"])
        self.assertTrue(waited.data["stdout"]["truncated"])
        self.assertEqual(waited.data["stderr"]["text"], "problem\n")
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
            ["process.started"],
        )
        self.assertTrue(any(
            event.event_type == "process.exited"
            and event.payload.get("process_id") == result.data["process_id"]
            for event in events
        ))
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

    async def test_missing_executable_is_a_not_started_result_without_approval(self) -> None:
        result = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-preflight",
            ToolCall("call-command-missing", "core.run_command", {
                "argv": ["tsm-agent-definitely-missing-command"],
                "cwd": ".",
            }),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "EXECUTABLE_NOT_FOUND")
        self.assertEqual(
            result.effective_recovery_kind.value, "retry_after_state_change",
        )
        self.assertEqual(result.recovery_action["execution_state"], "not_started")
        task = await self.application.kernel.get_task(self.task.task_id)
        self.assertIsNone(task.pending_approval)
        events = await self.application.registry.require(RuntimeStorePort).read_events(
            self.task.task_id
        )
        self.assertEqual(events[-1].event_type, "tool.failed")
        self.assertTrue(events[-1].payload["preflight"])

    async def test_preparation_preserves_launcher_identity_when_target_is_a_symlink(self) -> None:
        target = Path(self.temporary.name) / "launcher-target"
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
        launcher = Path(self.temporary.name) / "launcher"
        try:
            launcher.symlink_to(target.name)
        except OSError as error:
            self.skipTest(f"test environment cannot create symlinks: {error}")
        prepared = self.application.kernel._prepare_process_execution(
            self.task, (str(launcher),), ".", {},
        )
        self.assertEqual(prepared.argv[0], str(launcher))
        self.assertEqual(prepared.launch_path, launcher)
        self.assertEqual(prepared.resolved_target, target.resolve())
        self.assertNotEqual(
            prepared.approval_identity()["launch_path"],
            prepared.approval_identity()["resolved_target"],
        )

    async def test_relative_entry_can_target_an_external_program_from_child_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "actual-program"
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
            root = Path(self.temporary.name)
            (root / "package").mkdir()
            launcher = root / "runner"
            try:
                launcher.symlink_to(target)
            except OSError as error:
                self.skipTest(f"cannot create executable symlink: {error}")
            prepared = self.application.kernel._prepare_process_execution(
                self.task, ("../runner",), "package", {},
            )
            self.assertEqual(prepared.launch_path, root.resolve() / "runner")
            self.assertEqual(prepared.resolved_target, target.resolve())

    async def test_invalid_executable_entry_is_diagnosed_before_approval(self) -> None:
        result = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-invalid-entry", ToolCall(
                "call-invalid-entry", "core.run_command",
                {"argv": ["../outside/runner"], "cwd": "."},
            ),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "EXECUTABLE_PATH_INVALID")
        self.assertEqual(result.data["execution_state"], "not_started")
        self.assertEqual(result.data["stage"], "resolve_executable")
        self.assertEqual(result.data["requested_executable"], "../outside/runner")
        self.assertIn("No process was started", result.hint)
        self.assertIsNone((await self.application.kernel.get_task(self.task.task_id)).pending_approval)

    async def test_approval_is_invalidated_when_launcher_target_changes(self) -> None:
        first_target = Path(self.temporary.name) / "first-launcher-target"
        second_target = Path(self.temporary.name) / "second-launcher-target"
        for target in (first_target, second_target):
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
        launcher = Path(self.temporary.name) / "approved-launcher"
        try:
            launcher.symlink_to(first_target.name)
        except OSError as error:
            self.skipTest(f"test environment cannot create symlinks: {error}")
        call = ToolCall("call-command-identity", "core.run_command", {
            "argv": ["./approved-launcher"], "cwd": ".",
        })
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-identity", call,
            )
        request = caught.exception.request
        launcher.unlink()
        launcher.symlink_to(second_target.name)
        with self.assertRaises(ApprovalPayloadMismatch):
            await self.application.kernel.resolve_approval(
                self.task.task_id, request.request_id, request.payload_hash,
                ApprovalDecision.APPROVE, "approve stale launcher",
            )

    async def test_wait_expiry_leaves_managed_process_running(self) -> None:
        script = Path(self.temporary.name) / "slow_command.py"
        script.write_text(
            "import time\nprint('started', flush=True)\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        result = await self._approve_call(ToolCall(
            "call-command-timeout", "core.run_command", {
                "argv": [sys.executable, script.name],
                "cwd": ".",
            },
        ), "turn-timeout")
        self.assertTrue(result.ok)
        waited = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-timeout-wait",
            ToolCall("call-timeout-wait", "core.process_wait", {
                "process_id": result.data["process_id"], "wait_seconds": 0.2,
            }),
        )
        self.assertTrue(waited.ok)
        self.assertEqual(waited.data["state"], "RUNNING")
        self.assertEqual(waited.data["execution_state"], "running")
        task = await self.application.kernel.get_task(self.task.task_id)
        execution = task.tool_executions[
            "turn-timeout:call-command-timeout"
        ]
        self.assertEqual(execution.state.value, "COMMITTED")
        self.assertEqual(
            task.background_processes[result.data["process_id"]].state,
            BackgroundProcessState.RUNNING,
        )
        await self.application.kernel.stop_background_process(
            self.task.task_id, result.data["process_id"], grace_seconds=0.01,
        )

    async def test_wait_range_error_is_recoverable_with_correction_facts(self) -> None:
        result = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-invalid-wait",
            ToolCall("call-invalid-wait", "core.process_wait", {
                "process_id": "process-not-reached", "wait_seconds": 600,
            }),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")
        self.assertTrue(result.retryable)
        self.assertEqual(
            result.recovery_kind,
            ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS,
        )
        self.assertEqual(result.recovery_action, {
            "field": "wait_seconds", "minimum": 0.01,
            "maximum": 30.0, "received": 600,
        })

    async def test_process_lease_is_not_a_model_argument(self) -> None:
        with self.assertRaises(InvalidToolArguments):
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-lease-argument",
                ToolCall("call-lease-argument", "core.run_command", {
                    "argv": [sys.executable, "-V"], "cwd": ".",
                    "max_lifetime_seconds": 10,
                }),
            )

    async def test_wait_expiry_is_not_raced_by_the_outer_tool_deadline(self) -> None:
        """A wait window may finish normally even when it matches caller timeout.

        The process is deliberately still running.  Before the protocol grace,
        the generic outer deadline could win this race and incorrectly report a
        TIMEOUT instead of the observation result.
        """
        script = Path(self.temporary.name) / "deadline_race.py"
        script.write_text(
            "import time\ntime.sleep(30)\n", encoding="utf-8",
        )
        started = await self._approve_call(ToolCall(
            "call-deadline-race-start", "core.run_command", {
                "argv": [sys.executable, script.name], "cwd": ".",
            },
        ), "turn-deadline-race")
        self.assertTrue(started.ok)
        waited = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-deadline-race-wait",
            ToolCall("call-deadline-race-wait", "core.process_wait", {
                "process_id": started.data["process_id"], "wait_seconds": 0.05,
            }),
            # Matches the requested observation window.  Runtime supplies the
            # protocol return grace rather than making this a timeout race.
            timeout_seconds=0.05,
        )
        self.assertTrue(waited.ok)
        self.assertEqual(waited.data["state"], "RUNNING")
        await self.application.kernel.stop_background_process(
            self.task.task_id, started.data["process_id"], grace_seconds=0.01,
        )

    async def test_run_command_returns_managed_process(self) -> None:
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
                "cwd": ".",
            },
        ))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["mode"], "managed")
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

    async def test_turn_scope_close_preserves_task_owned_managed_process(self) -> None:
        """Approval/resume closes a turn scope, not an already-started server."""
        script = Path(self.temporary.name) / "persistent_server.py"
        script.write_text(
            "import time\nprint('ready', flush=True)\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        result = await self._approve_call(ToolCall(
            "call-command-persist", "core.run_command", {
                "argv": [sys.executable, script.name],
                "cwd": ".",
            },
        ), "turn-before-approval")
        process_id = result.data["process_id"]

        # This is the same cleanup operation performed before resuming an
        # approval checkpoint. It may release old model/turn resources but must
        # not stop task-owned process_id.
        await self.application.kernel._close_cancellation_scope(self.task.task_id)

        status = await self.application.kernel.get_background_process_status(
            self.task.task_id, process_id,
        )
        self.assertEqual(status.state, BackgroundProcessState.RUNNING)
        await self.application.kernel.stop_background_process(
            self.task.task_id, process_id, grace_seconds=0.01,
        )

    async def test_run_command_rejects_workspace_escape_and_credential_environment(self) -> None:
        escaped = await self.application.kernel.invoke_tool(self.task.task_id, "turn-escape", ToolCall(
            "call-command-escape", "core.run_command", {
                "argv": [sys.executable, "-V"], "cwd": "..",
            },
        ))
        self.assertFalse(escaped.ok)
        self.assertEqual(escaped.error_code, "INVALID_PARAM")
        self.assertEqual(escaped.data["stage"], "resolve_cwd")

        secret = await self.application.kernel.invoke_tool(self.task.task_id, "turn-secret", ToolCall(
            "call-command-secret", "core.run_command", {
                "argv": [sys.executable, "-V"],
                "cwd": ".",
                "environment": {"API_TOKEN": "must-not-forward"},
            },
        ))
        self.assertFalse(secret.ok)
        self.assertEqual(secret.error_code, "INVALID_PARAM")
        self.assertEqual(secret.data["stage"], "validate_environment")
        self.assertNotIn("must-not-forward", str(secret.to_data()))
        self.assertIsNone((await self.application.kernel.get_task(self.task.task_id)).pending_approval)

    async def test_run_command_normalizes_absolute_cwd_inside_workspace_before_approval(self) -> None:
        workspace = Path(self.temporary.name).resolve()
        call = ToolCall(
            "call-command-absolute-cwd", "core.run_command", {
                "argv": [
                    sys.executable, "-c",
                    "import os; print(os.getcwd())",
                ],
                "cwd": str(workspace),
            },
        )

        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-absolute-cwd", call,
            )
        request = caught.exception.request
        # Approval sees the exact canonical payload that the process will use,
        # rather than a host path that later fails the workspace resolver.
        self.assertEqual(request.call.arguments["cwd"], ".")
        self.assertEqual(
            request.execution_identity["launch_path"], sys.executable,
        )
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve normalized workspace cwd",
        )

        self.assertTrue(result.ok)
        waited = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-absolute-cwd-wait",
            ToolCall("call-absolute-cwd-wait", "core.process_wait", {
                "process_id": result.data["process_id"], "wait_seconds": 1,
            }),
        )
        self.assertEqual(waited.data["stdout"]["text"].strip(), str(workspace))
        events = await self.application.registry.require(RuntimeStorePort).read_events(
            self.task.task_id
        )
        prepared = next(
            event for event in events
            if event.event_type == "tool.prepared"
            and event.payload["call"]["call_id"] == call.call_id
        )
        self.assertEqual(prepared.payload["call"]["arguments"]["cwd"], ".")

    async def test_run_command_rejects_absolute_cwd_outside_workspace_before_approval(self) -> None:
        outside = Path(self.temporary.name).resolve().parent
        call = ToolCall(
            "call-command-outside-absolute-cwd", "core.run_command", {
                "argv": [sys.executable, "-V"], "cwd": str(outside),
            },
        )

        with self.assertRaisesRegex(
            ValueError, "process cwd must stay inside the workspace",
        ):
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-outside-absolute-cwd", call,
            )

    async def test_run_command_rejects_absolute_cwd_through_workspace_symlink(self) -> None:
        workspace = Path(self.temporary.name).resolve()
        outside = workspace.parent
        link = workspace / "outside-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"test environment cannot create directory symlinks: {error}")
        call = ToolCall(
            "call-command-symlink-absolute-cwd", "core.run_command", {
                "argv": [sys.executable, "-V"], "cwd": str(link),
            },
        )

        with self.assertRaisesRegex(
            ValueError, "process cwd must stay inside the workspace",
        ):
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-symlink-absolute-cwd", call,
            )


    async def test_run_command_applies_governed_environment_policy(self) -> None:
        result = await self._approve_call(ToolCall(
            "call-command-governed", "core.run_command", {
                "argv": [sys.executable, "-V"],
                "cwd": ".",
                "environment": {"CUSTOM_FLAG": "enabled"},
            },
        ))
        self.assertTrue(result.ok)
        waited = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-governed-wait",
            ToolCall("call-governed-wait", "core.process_wait", {
                "process_id": result.data["process_id"], "wait_seconds": 1,
            }),
        )
        self.assertTrue(waited.data["succeeded"])

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
                "argv": [sys.executable, "-m", "unittest"], "cwd": ".",
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
            self.assertEqual(result.data["execution_state"], "not_started")
            self.assertEqual(result.data["stage"], "authorize_executable")
            self.assertIn("resolved_target", result.data)
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

    async def test_session_process_list_and_control_respect_ownership(self) -> None:
        kernel = self.application.kernel
        process = await kernel.start_background_process(
            self.task.task_id, "turn-start", (sys.executable, "-c", "import time; time.sleep(30)"),
            max_lifetime_seconds=30,
        )
        other = await self._executing_task("task-foreign-process-list")
        try:
            found = await kernel.invoke_tool(self.task.task_id, "turn-later", ToolCall(
                "list-own", "core.process_list", {}))
            self.assertTrue(found.ok, found.message)
            self.assertEqual(found.data["processes"][0]["process_id"], process.process_id)
            self.assertEqual(found.data["processes"][0]["creator_turn_id"], "turn-start")
            self.assertNotIn("pid", found.data["processes"][0])
            foreign = await kernel.invoke_tool(other.task_id, "turn-other", ToolCall(
                "list-foreign", "core.process_list", {}))
            self.assertEqual(foreign.data["processes"], [])
            denied = await kernel.invoke_tool(other.task_id, "turn-other", ToolCall(
                "status-foreign", "core.process_status", {"process_id": process.process_id}))
            self.assertEqual(denied.error_code, "NOT_FOUND")
        finally:
            await kernel.stop_background_process(self.task.task_id, process.process_id, grace_seconds=0.01)

    async def test_process_list_discovers_and_controls_same_session_other_task(self) -> None:
        kernel = self.application.kernel
        process = await kernel.start_background_process(
            self.task.task_id, "turn-original", (sys.executable, "-c", "import time; time.sleep(30)"),
            max_lifetime_seconds=30,
        )
        other = await kernel.create_task("same session", Path(self.temporary.name),
                                         "task-same-session", session_id=self.task.session_id)
        for state in (TaskState.INTAKE, TaskState.RESOLVING_PROJECT, TaskState.SELECTING_EXTENSIONS,
                      TaskState.ROUTING, TaskState.EXECUTING):
            other = await kernel.transition_task(other.task_id, state, state.value)
        try:
            listing = await kernel.invoke_tool(other.task_id, "turn-new", ToolCall("list-session", "core.process_list", {}))
            self.assertTrue(listing.ok, listing.message)
            self.assertEqual(listing.data["processes"][0]["task_id"], self.task.task_id)
            scoped = await kernel.invoke_tool(other.task_id, "turn-new", ToolCall("list-task", "core.process_list", {"scope": "task"}))
            self.assertEqual(scoped.data["processes"], [])
            status = await kernel.invoke_tool(other.task_id, "turn-new", ToolCall("same-status", "core.process_status", {"process_id": process.process_id}))
            self.assertTrue(status.ok, status.message)
            with self.assertRaises(ApprovalRequired) as pending:
                await kernel.invoke_tool(other.task_id, "turn-new", ToolCall("same-stop", "core.process_stop", {"process_id": process.process_id}))
            request = pending.exception.request
            result = await kernel.resolve_approval(other.task_id, request.request_id, request.payload_hash,
                                                   ApprovalDecision.APPROVE, "stop exact process")
            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.data["state"], "CANCELLED")
        finally:
            await kernel.stop_background_process(self.task.task_id, process.process_id, grace_seconds=0.01)


if __name__ == "__main__":
    unittest.main()
