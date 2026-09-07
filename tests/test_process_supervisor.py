from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.local_process import LocalProcessExecutor
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import BackgroundProcessState, ProcessSandboxDenied, TaskState
from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    ProcessExecutorPort,
    ProcessExitStatus,
    ProcessStartRequest,
    RuntimeStorePort,
    SandboxDecision,
    SandboxRequest,
)


class AllowProcessSandbox:
    descriptor = AdapterDescriptor(
        adapter_id="fixture.allow-process-sandbox",
        adapter_version="0.1.0",
        port_name="SandboxPort",
        port_version="1.0",
        capabilities=frozenset({"workspace-process"}),
    )

    def __init__(self) -> None:
        self.started = False
        self.requests: list[SandboxRequest] = []

    async def start(self, context: AdapterContext) -> None:
        self.started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self.started else HealthState.UNHEALTHY
        )

    async def stop(self, deadline: datetime) -> None:
        self.started = False

    async def authorize(self, request: SandboxRequest) -> SandboxDecision:
        self.requests.append(request)
        return SandboxDecision(True, "fixture permits process")


class ProcessSupervisorTest(unittest.IsolatedAsyncioTestCase):
    async def _create_executing_task(self, *, allow: bool):
        sandbox = AllowProcessSandbox() if allow else None
        application = compose_fixture_application(sandbox_adapter=sandbox)
        await application.registry.start_all()
        temporary = tempfile.TemporaryDirectory()
        task = await application.kernel.create_task(
            "run a supervised process", Path(temporary.name), task_id="task-process"
        )
        for state in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, state, f"move to {state.value}"
            )
        return application, temporary, task, sandbox

    async def test_default_sandbox_denies_before_process_start(self) -> None:
        application, temporary, task, _sandbox = await self._create_executing_task(
            allow=False
        )
        try:
            with self.assertRaisesRegex(ProcessSandboxDenied, "denies process"):
                await application.kernel.run_foreground_process(
                    task.task_id, "turn-1", (sys.executable, "-c", "print('no')")
                )
            events = await application.registry.require(RuntimeStorePort).read_events(
                task.task_id
            )
            self.assertEqual(events[-1].event_type, "process.denied")
            self.assertNotIn(
                "process.started", [event.event_type for event in events]
            )
        finally:
            temporary.cleanup()
            await application.registry.stop_all()

    async def test_runs_without_shell_and_records_separate_bounded_output(self) -> None:
        application, temporary, task, sandbox = await self._create_executing_task(
            allow=True
        )
        try:
            code = (
                "import sys; "
                "print(sys.argv[1]); "
                "print('x' * 100); "
                "print('problem', file=sys.stderr)"
            )
            result = await application.kernel.run_foreground_process(
                task.task_id,
                "turn-1",
                (sys.executable, "-c", code, "$(touch must-not-run)"),
                environment={"SAFE_FLAG": "yes"},
                max_output_bytes=40,
            )

            self.assertEqual(result.result.status, ProcessExitStatus.EXITED)
            self.assertEqual(result.result.exit_code, 0)
            self.assertIn("$(touch must-not-run)", result.result.stdout.text)
            self.assertTrue(result.result.stdout.truncated)
            self.assertGreater(result.result.stdout.total_bytes, 40)
            self.assertEqual(result.result.stderr.text, "problem\n")
            self.assertEqual(result.handle.pid, result.handle.pgid)
            assert sandbox is not None
            self.assertEqual(sandbox.requests[0].environment["SAFE_FLAG"], "yes")
            self.assertEqual(
                set(sandbox.requests[0].environment), {"PATH", "LANG", "SAFE_FLAG"}
            )
            self.assertFalse((Path(temporary.name) / "must-not-run").exists())

            events = await application.registry.require(RuntimeStorePort).read_events(
                task.task_id
            )
            self.assertEqual(
                [event.event_type for event in events[-2:]],
                ["process.started", "process.exited"],
            )
            self.assertNotIn(sys.executable, str(events[-2].payload))
            self.assertTrue(events[-1].payload["stdout"]["truncated"])
        finally:
            temporary.cleanup()
            await application.registry.stop_all()

    async def test_rejects_workspace_escape_and_credential_environment(self) -> None:
        application, temporary, task, sandbox = await self._create_executing_task(
            allow=True
        )
        try:
            with self.assertRaisesRegex(ValueError, "inside the workspace"):
                await application.kernel.run_foreground_process(
                    task.task_id, "turn-1", (sys.executable, "-V"), cwd=".."
                )
            with self.assertRaisesRegex(ValueError, "credential-like"):
                await application.kernel.run_foreground_process(
                    task.task_id, "turn-1", (sys.executable, "-V"),
                    environment={"API_TOKEN": "do-not-forward"},
                )
            assert sandbox is not None
            self.assertEqual(sandbox.requests, [])
        finally:
            temporary.cleanup()
            await application.registry.stop_all()

    async def test_process_cwd_rejects_linked_directory_outside_workspace(self) -> None:
        application, temporary, task, sandbox = await self._create_executing_task(
            allow=True
        )
        workspace = Path(temporary.name)
        outside = workspace.parent / f"outside-cwd-{workspace.name}"
        outside.mkdir()
        link = workspace / "linked-cwd"
        try:
            os.symlink(outside, link)
            with self.assertRaisesRegex(ValueError, "inside the workspace"):
                await application.kernel.run_foreground_process(
                    task.task_id, "turn-linked-cwd",
                    (sys.executable, "-V"), cwd=link.name,
                )
            assert sandbox is not None
            self.assertEqual(sandbox.requests, [])
        finally:
            link.unlink(missing_ok=True)
            outside.rmdir()
            temporary.cleanup()
            await application.registry.stop_all()

    async def test_invalid_argv_and_start_failure_are_recorded_safely(self) -> None:
        application, temporary, task, sandbox = await self._create_executing_task(
            allow=True
        )
        try:
            with self.assertRaisesRegex(ValueError, "argv"):
                await application.kernel.run_foreground_process(
                    task.task_id, "turn-1", ()
                )
            assert sandbox is not None
            self.assertEqual(sandbox.requests, [])
            with self.assertRaises(FileNotFoundError):
                await application.kernel.run_foreground_process(
                    task.task_id, "turn-1", ("/definitely/missing/tsm-agt-command",)
                )
            events = await application.registry.require(RuntimeStorePort).read_events(
                task.task_id
            )
            self.assertEqual(events[-1].event_type, "process.failed")
            self.assertEqual(events[-1].payload["error_type"], "FileNotFoundError")
            self.assertNotIn("/definitely/missing", str(events[-1].payload))
        finally:
            temporary.cleanup()
            await application.registry.stop_all()

    async def test_timeout_terminates_process_group_and_drains_output(self) -> None:
        application, temporary, task, _sandbox = await self._create_executing_task(
            allow=True
        )
        try:
            code = (
                "import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); "
                "time.sleep(30)"
            )
            supervised = await application.kernel.run_foreground_process(
                task.task_id, "turn-1", (sys.executable, "-c", code),
                # Leave enough time for the child interpreter to install its
                # SIGTERM handler even when the complete suite loads the host.
                timeout_seconds=0.5, termination_grace_seconds=0.02,
            )
            self.assertEqual(supervised.result.status, ProcessExitStatus.TIMED_OUT)
            self.assertEqual(supervised.result.termination_signal, "KILL")
            self.assertIn("ready", supervised.result.stdout.text)
            with self.assertRaises(ProcessLookupError):
                os.killpg(supervised.handle.pgid, 0)
            events = await application.registry.require(RuntimeStorePort).read_events(
                task.task_id
            )
            self.assertEqual(events[-1].event_type, "process.cancelled")
            self.assertEqual(events[-1].payload["status"], "timed_out")
        finally:
            temporary.cleanup()
            await application.registry.stop_all()

    async def test_cancelling_supervisor_reaps_process_before_propagating(self) -> None:
        application, temporary, task, _sandbox = await self._create_executing_task(
            allow=True
        )
        running = asyncio.create_task(
            application.kernel.run_foreground_process(
                task.task_id, "turn-1",
                (sys.executable, "-c", "import time; print('up', flush=True); time.sleep(30)"),
                termination_grace_seconds=0.05,
            )
        )
        try:
            store = application.registry.require(RuntimeStorePort)
            handle_pgid = None
            for _ in range(100):
                events = await store.read_events(task.task_id)
                started = [event for event in events if event.event_type == "process.started"]
                if started:
                    handle_pgid = int(started[-1].payload["pgid"])
                    break
                await asyncio.sleep(0.005)
            self.assertIsNotNone(handle_pgid)
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            assert handle_pgid is not None
            with self.assertRaises(ProcessLookupError):
                os.killpg(handle_pgid, 0)
            events = await store.read_events(task.task_id)
            self.assertEqual(events[-1].event_type, "process.cancelled")
            self.assertEqual(events[-1].payload["status"], "cancelled")
        finally:
            temporary.cleanup()
            await application.registry.stop_all()

    async def test_background_process_supports_incremental_logs_status_and_stop(self) -> None:
        application, temporary, task, _sandbox = await self._create_executing_task(allow=True)
        try:
            code = (
                "import sys,time; "
                "print('first', flush=True); "
                "time.sleep(.08); "
                "print('second', flush=True); "
                "print('warning', file=sys.stderr, flush=True); "
                "time.sleep(30)"
            )
            record = await application.kernel.start_background_process(
                task.task_id, "turn-bg", (sys.executable, "-c", code),
                max_lifetime_seconds=10,
            )
            self.assertEqual(record.state, BackgroundProcessState.RUNNING)
            first = None
            for _ in range(100):
                first = await application.kernel.read_background_process_logs(
                    task.task_id, record.process_id
                )
                if "first" in first.stdout.text:
                    break
                await asyncio.sleep(0.005)
            assert first is not None
            self.assertEqual(first.stdout.text, "first\n")
            second = None
            for _ in range(100):
                second = await application.kernel.read_background_process_logs(
                    task.task_id, record.process_id,
                    stdout_cursor=first.stdout.next_cursor,
                    stderr_cursor=first.stderr.next_cursor,
                )
                if "second" in second.stdout.text:
                    break
                await asyncio.sleep(0.005)
            assert second is not None
            self.assertEqual(second.stdout.text, "second\n")
            self.assertEqual(second.stderr.text, "warning\n")
            stopped = await application.kernel.stop_background_process(
                task.task_id, record.process_id, grace_seconds=0.05
            )
            self.assertEqual(stopped.state, BackgroundProcessState.CANCELLED)
            persisted = await application.kernel.get_task(task.task_id)
            self.assertEqual(
                persisted.background_processes[record.process_id].state,
                BackgroundProcessState.CANCELLED,
            )
            events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
            event_types = [event.event_type for event in events]
            self.assertIn("process.logs_read", event_types)
            self.assertEqual(event_types[-2:], ["process.cancel_requested", "process.cancelled"])
        finally:
            temporary.cleanup()
            await application.registry.stop_all()

    async def test_background_process_natural_exit_is_observed(self) -> None:
        application, temporary, task, _sandbox = await self._create_executing_task(allow=True)
        try:
            record = await application.kernel.start_background_process(
                task.task_id, "turn-bg",
                (sys.executable, "-c", "print('done')"),
                max_lifetime_seconds=10,
            )
            status = record
            for _ in range(100):
                status = await application.kernel.get_background_process_status(
                    task.task_id, record.process_id
                )
                if status.state.is_terminal:
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(status.state, BackgroundProcessState.EXITED)
            self.assertEqual(status.exit_code, 0)
            logs = await application.kernel.read_background_process_logs(
                task.task_id, record.process_id
            )
            self.assertEqual(logs.stdout.text, "done\n")
        finally:
            temporary.cleanup()
            await application.registry.stop_all()

    async def test_background_lifetime_and_task_end_cleanup_reap_processes(self) -> None:
        application, temporary, task, _sandbox = await self._create_executing_task(allow=True)
        try:
            timed = await application.kernel.start_background_process(
                task.task_id, "turn-timeout",
                (sys.executable, "-c", "import time; time.sleep(30)"),
                max_lifetime_seconds=0.03,
            )
            for _ in range(100):
                timed_status = (await application.kernel.get_task(task.task_id)).background_processes[timed.process_id]
                if timed_status.state.is_terminal:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(timed_status.state, BackgroundProcessState.TIMED_OUT)

            owned = await application.kernel.start_background_process(
                task.task_id, "turn-cleanup",
                (
                    sys.executable, "-c",
                    "from pathlib import Path; import time; "
                    "Path('background-change.txt').write_text('kept'); time.sleep(30)",
                ),
                max_lifetime_seconds=10, stop_on_task_end=True,
            )
            changed_file = Path(temporary.name) / "background-change.txt"
            for _ in range(100):
                if changed_file.exists():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(changed_file.exists())
            transitioned = await application.kernel.transition_task(
                task.task_id, TaskState.CANCELLED, "cancel whole task"
            )
            self.assertEqual(transitioned.state, TaskState.CANCELLED)
            self.assertEqual(
                transitioned.background_processes[owned.process_id].state,
                BackgroundProcessState.CANCELLED,
            )
            with self.assertRaises(ProcessLookupError):
                os.killpg(owned.handle.pgid, 0)
            self.assertEqual(changed_file.read_text(), "kept")
        finally:
            temporary.cleanup()
            await application.registry.stop_all()

    async def test_sqlite_restart_marks_unowned_persisted_process_orphaned(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        database = Path(temporary.name) / "runtime.db"
        workspace = Path(temporary.name) / "workspace"
        workspace.mkdir()
        first = compose_fixture_application(
            sandbox_adapter=AllowProcessSandbox(),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await first.registry.start_all()
        task = await first.kernel.create_task("restart process owner", workspace, "task-restart")
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT, TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await first.kernel.transition_task(task.task_id, state, state.value)
        record = await first.kernel.start_background_process(
            task.task_id, "turn-restart",
            (sys.executable, "-c", "import time; time.sleep(30)"),
            max_lifetime_seconds=30,
        )
        await first.registry.stop_all()

        second = compose_fixture_application(
            sandbox_adapter=AllowProcessSandbox(),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            reconciled = await second.kernel.reconcile_background_processes(task.task_id)
            self.assertEqual(reconciled[0].state, BackgroundProcessState.ORPHANED)
            events = await second.registry.require(RuntimeStorePort).read_events(task.task_id)
            self.assertEqual(events[-1].event_type, "process.orphaned")
            self.assertFalse(second.registry.require(ProcessExecutorPort).owns_process(record.handle))
        finally:
            await second.registry.stop_all()
            temporary.cleanup()


class LocalProcessExecutorContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_forged_handle_identity(self) -> None:
        executor = LocalProcessExecutor()
        await executor.start(AdapterContext({}, lambda _type, _payload: None))
        temporary = tempfile.TemporaryDirectory()
        try:
            handle = await executor.start_process(
                ProcessStartRequest(
                    "process-1",
                    (sys.executable, "-c", "import time; time.sleep(30)"),
                    Path(temporary.name).resolve(),
                )
            )
            forged = handle.__class__(
                handle.process_id, handle.pid, handle.pgid, "wrong-birth",
                handle.argv_hash, handle.cwd, handle.started_at,
            )
            with self.assertRaisesRegex(ValueError, "identity"):
                await executor.stop_process(
                    forged, 0.01, ProcessExitStatus.CANCELLED
                )
            result = await executor.stop_process(
                handle, 0.01, ProcessExitStatus.CANCELLED
            )
            self.assertEqual(result.status, ProcessExitStatus.CANCELLED)
        finally:
            await executor.stop(datetime.now())
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
