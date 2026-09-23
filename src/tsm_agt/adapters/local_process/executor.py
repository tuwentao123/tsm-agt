"""Async subprocess executor using isolated POSIX process groups."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone
from tsm_agt.bootstrap.process_environment_configuration import (
    ProcessEnvironmentConfiguration,
)

from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    ProcessExitStatus,
    ProcessHandle,
    ProcessLogChunk,
    ProcessLogs,
    ProcessOutput,
    ProcessResult,
    ProcessStartRequest,
)


@dataclass(slots=True)
class _LiveProcess:
    process: asyncio.subprocess.Process
    request: ProcessStartRequest
    handle: ProcessHandle
    stdout_buffer: _LogBuffer
    stderr_buffer: _LogBuffer
    stdout_task: asyncio.Task[None]
    stderr_task: asyncio.Task[None]


@dataclass(slots=True)
class _LogBuffer:
    limit: int
    kept: bytearray = field(default_factory=bytearray)
    total_bytes: int = 0

    def append(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        remaining = self.limit - len(self.kept)
        if remaining > 0:
            self.kept.extend(chunk[:remaining])

    def output(self) -> ProcessOutput:
        return ProcessOutput(
            self.kept.decode("utf-8", errors="replace"),
            self.total_bytes,
            self.total_bytes > len(self.kept),
        )

    def read(self, cursor: int) -> ProcessLogChunk:
        if cursor < 0 or cursor > len(self.kept):
            raise ValueError("log cursor is outside the retained output")
        return ProcessLogChunk(
            text=bytes(self.kept[cursor:]).decode("utf-8", errors="replace"),
            cursor=cursor,
            next_cursor=len(self.kept),
            total_bytes=self.total_bytes,
            truncated=self.total_bytes > len(self.kept),
        )


class LocalProcessExecutor:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.local-process-executor",
        adapter_version="0.1.0",
        port_name="ProcessExecutorPort",
        port_version="1.0",
        capabilities=frozenset(
            {"foreground", "background", "process-group", "bounded-output", "incremental-logs"}
        ),
    )

    def __init__(self) -> None:
        self._started = False
        self._live: dict[str, _LiveProcess] = {}
        self._completed: dict[
            str,
            tuple[ProcessHandle, ProcessResult, _LogBuffer, _LogBuffer],
        ] = {}
        self._environment_configuration = ProcessEnvironmentConfiguration()

    async def start(self, context: AdapterContext) -> None:
        if os.name == "nt":
            raise RuntimeError(
                "POSIX local process executor cannot start on Windows"
            )
        self._started = True

    async def health(self) -> HealthStatus:
        state = HealthState.HEALTHY if self._started else HealthState.UNHEALTHY
        return HealthStatus(
            state, "local process executor ready" if self._started else "not started"
        )

    async def stop(self, deadline: datetime) -> None:
        for live in tuple(self._live.values()):
            await self.stop_process(
                live.handle, grace_seconds=0.2, status=ProcessExitStatus.CANCELLED
            )
        self._started = False

    def prepare_environment(self, environment, policy=None) -> dict[str, str]:
        return self._environment_configuration.build_environment(
            environment,
            policy,
        )

    async def start_process(self, request: ProcessStartRequest) -> ProcessHandle:
        if not self._started:
            raise RuntimeError("process executor is not started")
        if request.process_id in self._live:
            raise ValueError(f"process is already live: {request.process_id}")
        started_at = datetime.now(timezone.utc)
        process = await asyncio.create_subprocess_exec(
            *request.argv,
            cwd=request.cwd,
            env=dict(request.environment),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        pgid = os.getpgid(process.pid)
        argv_hash = hashlib.sha256(
            json.dumps(request.argv, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        handle = ProcessHandle(
            process_id=request.process_id,
            pid=process.pid,
            pgid=pgid,
            birth_marker=f"{process.pid}:{started_at.isoformat()}",
            argv_hash=argv_hash,
            cwd=str(request.cwd),
            started_at=started_at,
        )
        assert process.stdout is not None and process.stderr is not None
        stdout_buffer = _LogBuffer(request.max_output_bytes)
        stderr_buffer = _LogBuffer(request.max_output_bytes)
        self._live[request.process_id] = _LiveProcess(
            process=process,
            request=request,
            handle=handle,
            stdout_buffer=stdout_buffer,
            stderr_buffer=stderr_buffer,
            stdout_task=asyncio.create_task(self._drain(process.stdout, stdout_buffer)),
            stderr_task=asyncio.create_task(self._drain(process.stderr, stderr_buffer)),
        )
        return handle

    async def wait_process(self, handle: ProcessHandle) -> ProcessResult:
        completed = self._completed_result(handle)
        if completed is not None:
            return completed
        live = self._require_live(handle)
        return_code = await live.process.wait()
        return await self._finish(live, return_code, ProcessExitStatus.EXITED, None)

    async def poll_process(self, handle: ProcessHandle) -> ProcessResult | None:
        completed = self._completed_result(handle)
        if completed is not None:
            return completed
        live = self._require_live(handle)
        if live.process.returncode is None:
            return None
        return await self._finish(
            live, live.process.returncode, ProcessExitStatus.EXITED, None
        )

    async def read_process_logs(
        self, handle: ProcessHandle, stdout_cursor: int, stderr_cursor: int
    ) -> ProcessLogs:
        completed = self._completed.get(handle.process_id)
        if completed is not None:
            self._validate_handle(handle, completed[0])
            stdout_buffer, stderr_buffer = completed[2], completed[3]
        else:
            live = self._require_live(handle)
            stdout_buffer, stderr_buffer = live.stdout_buffer, live.stderr_buffer
        return ProcessLogs(
            stdout_buffer.read(stdout_cursor), stderr_buffer.read(stderr_cursor)
        )

    def owns_process(self, handle: ProcessHandle) -> bool:
        live = self._live.get(handle.process_id)
        if live is not None:
            return self._handles_match(handle, live.handle)
        completed = self._completed.get(handle.process_id)
        return completed is not None and self._handles_match(handle, completed[0])

    async def stop_process(
        self, handle: ProcessHandle, grace_seconds: float, status: ProcessExitStatus
    ) -> ProcessResult:
        if grace_seconds < 0:
            raise ValueError("grace_seconds must not be negative")
        if status not in {ProcessExitStatus.CANCELLED, ProcessExitStatus.TIMED_OUT}:
            raise ValueError("stop status must be cancelled or timed_out")
        completed = self._completed_result(handle)
        if completed is not None:
            return completed
        live = self._require_live(handle)
        if live.process.returncode is None:
            self._signal_group(live.handle.pgid, signal.SIGTERM)
            termination_signal = "TERM"
            try:
                await asyncio.wait_for(live.process.wait(), timeout=grace_seconds)
            except TimeoutError:
                self._signal_group(live.handle.pgid, signal.SIGKILL)
                termination_signal = "KILL"
                await live.process.wait()
        else:
            termination_signal = None
        return await self._finish(
            live, live.process.returncode, status, termination_signal
        )

    def _require_live(self, handle: ProcessHandle) -> _LiveProcess:
        live = self._live.get(handle.process_id)
        if live is None:
            raise LookupError(f"live process not found: {handle.process_id}")
        self._validate_handle(handle, live.handle)
        return live

    def _completed_result(self, handle: ProcessHandle) -> ProcessResult | None:
        completed = self._completed.get(handle.process_id)
        if completed is None:
            return None
        self._validate_handle(handle, completed[0])
        return completed[1]

    @staticmethod
    def _handles_match(left: ProcessHandle, right: ProcessHandle) -> bool:
        return (
            left.pid == right.pid
            and left.pgid == right.pgid
            and left.birth_marker == right.birth_marker
            and left.argv_hash == right.argv_hash
            and left.cwd == right.cwd
        )

    @classmethod
    def _validate_handle(cls, supplied: ProcessHandle, owned: ProcessHandle) -> None:
        if not cls._handles_match(supplied, owned):
            raise ValueError("process handle identity does not match the live process")

    async def _finish(
        self,
        live: _LiveProcess,
        return_code: int | None,
        status: ProcessExitStatus,
        termination_signal: str | None,
    ) -> ProcessResult:
        await asyncio.gather(live.stdout_task, live.stderr_task)
        self._live.pop(live.handle.process_id, None)
        result = ProcessResult(
            process_id=live.handle.process_id,
            status=status,
            exit_code=return_code,
            stdout=live.stdout_buffer.output(),
            stderr=live.stderr_buffer.output(),
            started_at=live.handle.started_at,
            finished_at=datetime.now(timezone.utc),
            termination_signal=termination_signal,
        )
        self._completed[live.handle.process_id] = (
            live.handle, result, live.stdout_buffer, live.stderr_buffer
        )
        return result

    @staticmethod
    async def _drain(stream: asyncio.StreamReader, buffer: _LogBuffer) -> None:
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            buffer.append(chunk)

    @staticmethod
    def _signal_group(pgid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
