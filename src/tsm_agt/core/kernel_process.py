"""Foreground command execution and the background-process registry for ``Kernel``.

A mixin module in the facade-plus-siblings shape (``run_agent.py`` +
``agent/session_persistence.py``): the facade ``core/kernel.py`` imports
this at module level and adds ``ProcessMixin`` to ``Kernel``'s bases, so
**every method resolves through ``Kernel``'s MRO unchanged** --
``self._dependencies``, ``self._append_events`` and ``Kernel``'s own class
attributes all still resolve. Moved bodies are verbatim: nothing is renamed
and no signature changes.

This module must never bind the facade at module level; late-import it
inside a method instead (the project's module-boundary rule).

Moved from ``core/kernel.py``:

* ``_argv_hash``
* ``_background_record_data``
* ``_cleanup_background_processes_for_task_end``
* ``_commit_background_process``
* ``_complete_background_process``
* ``_get_background_process``
* ``_local_interpreter``
* ``_mark_background_orphaned``
* ``_record_process_result``
* ``_require_process_executor``
* ``_resolve_process_cwd``
* ``_schedule_background_deadline``
* ``_stop_background_record``
* ``_validate_process_environment``
* ``get_background_process_status``
* ``read_background_process_logs``
* ``reconcile_background_processes``
* ``run_foreground_process``
* ``start_background_process``
* ``stop_background_process``
"""

from __future__ import annotations


import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tsm_agt.ports import (
    ProcessCommandResult, ProcessEnvironmentPolicy, ProcessExecutorPort,
    ProcessExitStatus, ProcessLogs, ProcessStartRequest, RuntimeEvent,
    RuntimeUnitOfWork, SandboxRequest
)
from typing import Any
from uuid import uuid4
from .onboarding import ProjectOnboardingScanner
from .process import (
    BackgroundProcessRecord, BackgroundProcessState,
    ProcessSandboxDenied
)
from .task import TaskNotFound, TaskSnapshot, TaskState
from .turn import InvalidTurnState


class ProcessMixin:
    async def run_foreground_process(
        self,
        task_id: str,
        turn_id: str,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 300.0,
        termination_grace_seconds: float = 2.0,
        max_output_bytes: int = 1_000_000,
        invocation_id: str | None = None,
    ) -> ProcessCommandResult:
        if not turn_id.strip():
            raise ValueError("turn_id must not be empty")
        if not argv or any(not item or "\x00" in item for item in argv):
            raise ValueError("argv must contain non-empty values without NUL bytes")
        if timeout_seconds <= 0 or termination_grace_seconds < 0:
            raise ValueError("process timeout must be positive and grace non-negative")
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        executor = self._dependencies.process_executor
        if executor is None:
            raise RuntimeError("no ProcessExecutorPort Adapter is configured")
        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        task = TaskSnapshot.from_data(stored.data)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        resolved_cwd = self._resolve_process_cwd(Path(task.workspace), cwd)
        validated_environment = self._validate_process_environment(environment or {})
        environment_policy = ProcessEnvironmentPolicy(
            inherit_host_environment=True,
            allowed_host_variables=("SSH_AUTH_SOCK", "HOME", "USER"),
            blocked_host_variables=("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"),
            runtime_trust_mode="governed",
        )
        safe_environment = dict(
            executor.prepare_environment(
                validated_environment,
                environment_policy,
            )
        )
        sandbox_request = SandboxRequest(
            argv,
            resolved_cwd,
            safe_environment,
            environment_policy=environment_policy,
        )
        decision = await self._dependencies.sandbox.authorize(sandbox_request)
        if not decision.allowed:
            await self._append_events(
                task_id,
                ((
                    "process.denied",
                    {
                        "turn_id": turn_id,
                        "argv_hash": self._argv_hash(argv),
                        "cwd": str(resolved_cwd),
                        "reason": decision.reason,
                        "invocation_id": invocation_id,
                    },
                ),),
            )
            raise ProcessSandboxDenied(decision.reason)

        process_id = f"process-{uuid4().hex}"
        request = ProcessStartRequest(
            process_id=process_id, argv=argv, cwd=resolved_cwd,
            environment=safe_environment, max_output_bytes=max_output_bytes,
        )
        try:
            handle = await executor.start_process(request)
        except Exception as error:
            await self._append_events(
                task_id,
                ((
                    "process.failed",
                    {
                        "turn_id": turn_id, "process_id": process_id,
                        "argv_hash": self._argv_hash(argv),
                        "cwd": str(resolved_cwd),
                        "error_type": type(error).__name__,
                        "message": "process executor could not start the requested argv",
                        "invocation_id": invocation_id,
                    },
                ),),
            )
            raise
        await self._append_events(
            task_id,
            ((
                "process.started",
                {
                    "turn_id": turn_id, "process_id": handle.process_id,
                    "pid": handle.pid, "pgid": handle.pgid,
                    "birth_marker": handle.birth_marker,
                    "argv_hash": handle.argv_hash, "cwd": handle.cwd,
                    "started_at": handle.started_at.isoformat(),
                    "invocation_id": invocation_id,
                },
            ),),
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await executor.wait_process(handle)
        except TimeoutError:
            result = await executor.stop_process(
                handle, termination_grace_seconds, ProcessExitStatus.TIMED_OUT
            )
        except asyncio.CancelledError:
            result = await asyncio.shield(
                executor.stop_process(
                    handle, termination_grace_seconds, ProcessExitStatus.CANCELLED
                )
            )
            await self._record_process_result(
                task_id, turn_id, result, invocation_id
            )
            raise
        await self._record_process_result(task_id, turn_id, result, invocation_id)
        return ProcessCommandResult(handle, result)

    async def start_background_process(
        self,
        task_id: str,
        turn_id: str,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        environment: Mapping[str, str] | None = None,
        max_lifetime_seconds: float = 3600.0,
        max_output_bytes: int = 1_000_000,
        stop_on_task_end: bool = True,
        invocation_id: str | None = None,
    ) -> BackgroundProcessRecord:
        if not turn_id.strip():
            raise ValueError("turn_id must not be empty")
        if not argv or any(not item or "\x00" in item for item in argv):
            raise ValueError("argv must contain non-empty values without NUL bytes")
        if max_lifetime_seconds <= 0 or max_output_bytes <= 0:
            raise ValueError("background process lifetime and output limit must be positive")
        executor = self._require_process_executor()
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        resolved_cwd = self._resolve_process_cwd(Path(task.workspace), cwd)
        safe_environment = self._validate_process_environment(environment or {})
        environment_policy = ProcessEnvironmentPolicy(
            inherit_host_environment=True,
            allowed_host_variables=("SSH_AUTH_SOCK", "HOME", "USER"),
            blocked_host_variables=("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"),
            runtime_trust_mode="governed",
        )
        decision = await self._dependencies.sandbox.authorize(
            SandboxRequest(argv, resolved_cwd, safe_environment)
        )
        if not decision.allowed:
            await self._append_events(task_id, (("process.denied", {
                "turn_id": turn_id, "argv_hash": self._argv_hash(argv),
                "cwd": str(resolved_cwd), "reason": decision.reason,
            }),))
            raise ProcessSandboxDenied(decision.reason)
        process_id = f"process-{uuid4().hex}"
        request = ProcessStartRequest(
            process_id, argv, resolved_cwd, safe_environment, max_output_bytes
        )
        handle = await executor.start_process(request)
        deadline = handle.started_at + timedelta(seconds=max_lifetime_seconds)
        record = BackgroundProcessRecord(
            process_id, turn_id, handle, BackgroundProcessState.RUNNING,
            max_lifetime_seconds, deadline, stop_on_task_end,
        )
        await self._commit_background_process(
            task_id, record, "process.started", {
                "turn_id": turn_id, "process_id": process_id,
                "pid": handle.pid, "pgid": handle.pgid,
                "birth_marker": handle.birth_marker,
                "argv_hash": handle.argv_hash, "cwd": handle.cwd,
                "started_at": handle.started_at.isoformat(),
                "background": True, "deadline_at": deadline.isoformat(),
                "stop_on_task_end": stop_on_task_end,
                "invocation_id": invocation_id,
            },
        )
        self._schedule_background_deadline(task_id, record)

        async def stop_background() -> None:
            current = await self._get_background_process(
                task_id, record.process_id
            )
            if current.state is BackgroundProcessState.RUNNING:
                await self._stop_background_record(
                    task_id, current, ProcessExitStatus.CANCELLED, 2.0
                )

        # Idempotent with _cleanup_background_processes_for_task_end: it only
        # stops a process that is still RUNNING at scope-close time.
        self._cancellation_scope(task_id).register_cleanup(
            f"process:{process_id}", stop_background
        )
        return record

    async def get_background_process_status(
        self, task_id: str, process_id: str
    ) -> BackgroundProcessRecord:
        record = await self._get_background_process(task_id, process_id)
        if record.state.is_terminal:
            return record
        executor = self._require_process_executor()
        if not executor.owns_process(record.handle):
            return await self._mark_background_orphaned(task_id, record)
        if datetime.now(timezone.utc) >= record.deadline_at:
            return await self._stop_background_record(
                task_id, record, ProcessExitStatus.TIMED_OUT, 2.0
            )
        result = await executor.poll_process(record.handle)
        if result is None:
            return record
        return await self._complete_background_process(task_id, record, result)

    async def read_background_process_logs(
        self, task_id: str, process_id: str, *,
        stdout_cursor: int = 0, stderr_cursor: int = 0,
    ) -> ProcessLogs:
        record = await self._get_background_process(task_id, process_id)
        executor = self._require_process_executor()
        if not executor.owns_process(record.handle):
            if record.state is BackgroundProcessState.RUNNING:
                await self._mark_background_orphaned(task_id, record)
            raise LookupError("process logs are unavailable after executor ownership was lost")
        logs = await executor.read_process_logs(
            record.handle, stdout_cursor, stderr_cursor
        )
        await self._append_events(task_id, (("process.logs_read", {
            "process_id": process_id,
            "stdout_cursor": stdout_cursor,
            "stdout_next_cursor": logs.stdout.next_cursor,
            "stderr_cursor": stderr_cursor,
            "stderr_next_cursor": logs.stderr.next_cursor,
        }),))
        return logs

    async def stop_background_process(
        self, task_id: str, process_id: str, *, grace_seconds: float = 2.0
    ) -> BackgroundProcessRecord:
        if grace_seconds < 0:
            raise ValueError("grace_seconds must not be negative")
        record = await self._get_background_process(task_id, process_id)
        if record.state.is_terminal:
            return record
        await self._append_events(task_id, (("process.cancel_requested", {
            "process_id": process_id, "reason": "explicit stop"
        }),))
        return await self._stop_background_record(
            task_id, record, ProcessExitStatus.CANCELLED, grace_seconds
        )

    async def reconcile_background_processes(
        self, task_id: str
    ) -> tuple[BackgroundProcessRecord, ...]:
        task = TaskSnapshot.from_data((await self._require_stored_task(task_id)).data)
        reconciled: list[BackgroundProcessRecord] = []
        for record in task.background_processes.values():
            if record.state is not BackgroundProcessState.RUNNING:
                reconciled.append(record)
            elif not self._require_process_executor().owns_process(record.handle):
                reconciled.append(await self._mark_background_orphaned(task_id, record))
            else:
                reconciled.append(await self.get_background_process_status(task_id, record.process_id))
        return tuple(reconciled)

    async def _stop_background_record(
        self, task_id: str, record: BackgroundProcessRecord,
        status: ProcessExitStatus, grace_seconds: float,
    ) -> BackgroundProcessRecord:
        executor = self._require_process_executor()
        if not executor.owns_process(record.handle):
            return await self._mark_background_orphaned(task_id, record)
        result = await executor.stop_process(record.handle, grace_seconds, status)
        return await self._complete_background_process(task_id, record, result)

    async def _complete_background_process(
        self, task_id: str, record: BackgroundProcessRecord, result: ProcessResult
    ) -> BackgroundProcessRecord:
        completed = record.finish(result)
        event_type = "process.exited" if result.status is ProcessExitStatus.EXITED else "process.cancelled"
        await self._commit_background_process(task_id, completed, event_type, {
            "turn_id": record.turn_id, "process_id": record.process_id,
            "status": result.status.value, "exit_code": result.exit_code,
            "succeeded": result.succeeded,
            "failure_code": result.failure_code,
            "termination_signal": result.termination_signal,
            "started_at": result.started_at.isoformat(),
            "finished_at": result.finished_at.isoformat(),
            "background": True,
        })
        timer = self._background_deadline_tasks.pop(record.process_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        return completed

    async def _mark_background_orphaned(
        self, task_id: str, record: BackgroundProcessRecord
    ) -> BackgroundProcessRecord:
        orphaned = record.orphan(datetime.now(timezone.utc))
        await self._commit_background_process(task_id, orphaned, "process.orphaned", {
            "process_id": record.process_id,
            "reason": "current executor cannot prove ownership of persisted handle",
        })
        return orphaned

    async def _commit_background_process(
        self, task_id: str, record: BackgroundProcessRecord,
        event_type: str, payload: Mapping[str, Any],
    ) -> None:
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data).with_background_process(record)
        event = RuntimeEvent(
            f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
            event_type, payload,
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, task.to_data(), (event,)
        ))

    async def _get_background_process(
        self, task_id: str, process_id: str
    ) -> BackgroundProcessRecord:
        task = TaskSnapshot.from_data((await self._require_stored_task(task_id)).data)
        try:
            return task.background_processes[process_id]
        except KeyError as error:
            raise LookupError(f"background process not found: {process_id}") from error

    @staticmethod
    def _background_record_data(
        record: BackgroundProcessRecord,
    ) -> dict[str, Any]:
        return {
            "process_id": record.process_id,
            "state": record.state.value,
            "started_at": record.handle.started_at.isoformat(),
            "deadline_at": record.deadline_at.isoformat(),
            "max_lifetime_seconds": record.max_lifetime_seconds,
            "stop_on_task_end": record.stop_on_task_end,
            "exit_code": record.exit_code,
            "termination_signal": record.termination_signal,
            "finished_at": (
                record.finished_at.isoformat() if record.finished_at else None
            ),
        }

    def _schedule_background_deadline(
        self, task_id: str, record: BackgroundProcessRecord
    ) -> None:
        async def enforce() -> None:
            delay = max(0.0, (record.deadline_at - datetime.now(timezone.utc)).total_seconds())
            await asyncio.sleep(delay)
            current = await self._get_background_process(task_id, record.process_id)
            if current.state is BackgroundProcessState.RUNNING:
                await self._stop_background_record(
                    task_id, current, ProcessExitStatus.TIMED_OUT, 2.0
                )
        deadline_task = asyncio.create_task(enforce())
        self._background_deadline_tasks[record.process_id] = deadline_task
        self._cancellation_scope(task_id).register_task(
            deadline_task, name=f"deadline:{record.process_id}"
        )

    def _require_process_executor(self) -> ProcessExecutorPort:
        executor = self._dependencies.process_executor
        if executor is None:
            raise RuntimeError("no ProcessExecutorPort Adapter is configured")
        return executor

    async def _record_process_result(
        self, task_id: str, turn_id: str, result: ProcessResult,
        invocation_id: str | None = None,
    ) -> None:
        event_type = (
            "process.exited"
            if result.status is ProcessExitStatus.EXITED
            else "process.cancelled"
        )
        await self._append_events(
            task_id,
            ((
                event_type,
                {
                    "turn_id": turn_id, "process_id": result.process_id,
                    "status": result.status.value, "exit_code": result.exit_code,
                    "succeeded": result.succeeded,
                    "failure_code": result.failure_code,
                    "termination_signal": result.termination_signal,
                    "stdout": {
                        "text": result.stdout.text,
                        "total_bytes": result.stdout.total_bytes,
                        "truncated": result.stdout.truncated,
                    },
                    "stderr": {
                        "text": result.stderr.text,
                        "total_bytes": result.stderr.total_bytes,
                        "truncated": result.stderr.truncated,
                    },
                    "started_at": result.started_at.isoformat(),
                    "finished_at": result.finished_at.isoformat(),
                    "invocation_id": invocation_id,
                },
            ),),
        )

    def _resolve_process_cwd(self, workspace: Path, cwd: str) -> Path:
        if not cwd.strip() or "\x00" in cwd:
            raise ValueError("process cwd must not be empty or contain NUL")
        try:
            resolved = self._dependencies.workspace_path.resolve_access_path(
                workspace, cwd
            ).path
        except (PermissionError, ValueError) as error:
            raise ValueError("process cwd must stay inside the workspace") from error
        if not resolved.is_dir():
            raise ValueError(f"process cwd is not a directory: {resolved}")
        return resolved

    @staticmethod
    def _validate_process_environment(
        environment: Mapping[str, str]
    ) -> dict[str, str]:
        safe: dict[str, str] = {}
        for name, value in environment.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("process environment names and values must be strings")
            lowered = name.lower()
            if any(
                secret in lowered
                for secret in ("secret", "token", "password", "api_key", "apikey")
            ):
                raise ValueError(f"process environment cannot include credential-like key: {name}")
            safe[name] = value
        return safe

    @staticmethod
    def _argv_hash(argv: tuple[str, ...]) -> str:
        import hashlib

        return hashlib.sha256("\0".join(argv).encode()).hexdigest()

    def _local_interpreter(self, task: TaskSnapshot) -> str:
        """Return the workspace's own interpreter path when one is observed.

        Runtime already publishes local entry points as onboarding facts. A
        verification gap that omits them invites the model to reach for a system
        interpreter, whose environment usually lacks the project's test
        dependencies, which then reads as "the tests failed" instead of "the
        wrong Python was used".
        """
        try:
            facts = ProjectOnboardingScanner(
                self._dependencies.workspace_path
            ).local_executables(Path(task.workspace))
        except OSError:
            return ""
        for fact in facts:
            try:
                data = json.loads(fact.value)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(data, Mapping):
                continue
            path = str(data.get("path", ""))
            if path.endswith(("bin/python", "Scripts/python.exe")):
                return path
        return ""

    async def _cleanup_background_processes_for_task_end(
        self, task_id: str, task: TaskSnapshot
    ) -> None:
        for record in tuple(task.background_processes.values()):
            if (
                record.state is BackgroundProcessState.RUNNING
                and record.stop_on_task_end
            ):
                await self._append_events(task_id, (("process.cancel_requested", {
                    "process_id": record.process_id, "reason": "task ended"
                }),))
                await self._stop_background_record(
                    task_id, record, ProcessExitStatus.CANCELLED, 2.0
                )
