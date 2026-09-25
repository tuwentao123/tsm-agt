"""Windows process supervision backed by one Job Object per process tree."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
from dataclasses import dataclass, field

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
    ProcessExitStatus, ProcessHandle, ProcessLogChunk, ProcessLogs,
    ProcessOutput, ProcessResult, ProcessStartRequest,
)


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
            self.kept.decode("utf-8", errors="replace"), self.total_bytes,
            self.total_bytes > len(self.kept),
        )

    def read(self, cursor: int) -> ProcessLogChunk:
        if cursor < 0 or cursor > len(self.kept):
            raise ValueError("log cursor is outside the retained output")
        return ProcessLogChunk(
            bytes(self.kept[cursor:]).decode("utf-8", errors="replace"),
            cursor, len(self.kept), self.total_bytes,
            self.total_bytes > len(self.kept),
        )


@dataclass(slots=True)
class _LiveProcess:
    process: asyncio.subprocess.Process
    request: ProcessStartRequest
    handle: ProcessHandle
    job_handle: int
    stdout_buffer: _LogBuffer
    stderr_buffer: _LogBuffer
    stdout_task: asyncio.Task[None]
    stderr_task: asyncio.Task[None]


class WindowsProcessExecutor:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.windows-process-executor",
        adapter_version="0.1.0", port_name="ProcessExecutorPort",
        port_version="1.0",
        capabilities=frozenset({
            "foreground", "background", "windows-job-object",
            "process-tree-kill", "bounded-output", "incremental-logs",
        }),
    )

    def __init__(self) -> None:
        self._started = False
        self._live: dict[str, _LiveProcess] = {}
        # See LocalProcessExecutor: importing bootstrap at adapter module load
        # time creates a cycle when composition imports both platform executors.
        from tsm_agt.bootstrap.process_environment_configuration import (
            ProcessEnvironmentConfiguration,
        )

        self._environment_configuration = ProcessEnvironmentConfiguration()

    async def start(self, context: AdapterContext) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows process executor can start only on Windows")
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "Windows Job Object executor ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        for live in tuple(self._live.values()):
            await self.stop_process(
                live.handle, 0.2, ProcessExitStatus.CANCELLED
            )
        self._started = False

    def prepare_environment(self, environment, policy=None) -> dict[str, str]:
        # Windows environment keys are case-insensitive. Normalize the caller's
        # mapping *before* applying it to the base environment, so Path/PATH
        # obey normal last-write-wins semantics instead of creating two keys.
        requested: dict[str, str] = {}
        requested_names: dict[str, str] = {}
        for name, value in environment.items():
            folded = name.casefold()
            previous_name = requested_names.get(folded)
            if previous_name is not None:
                requested.pop(previous_name, None)
            requested_names[folded] = name
            requested[name] = value

        built = self._environment_configuration.build_environment(
            requested,
            policy,
            host_environment=os.environ,
        )
        normalized: dict[str, str] = {}
        normalized_names: dict[str, str] = {}
        for name, value in built.items():
            folded = name.casefold()
            previous_name = normalized_names.get(folded)
            if previous_name is not None:
                normalized.pop(previous_name, None)
            normalized_names[folded] = name
            normalized[name] = value
        return normalized

    async def start_process(self, request: ProcessStartRequest) -> ProcessHandle:
        if not self._started:
            raise RuntimeError("Windows process executor is not started")
        if request.process_id in self._live:
            raise ValueError(f"process is already live: {request.process_id}")
        creationflags = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        started_at = datetime.now(timezone.utc)
        argv = self._windows_argv(request.argv)
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=request.cwd, env=dict(request.environment),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, creationflags=creationflags,
        )
        try:
            job_handle = self._create_and_assign_job(process.pid)
        except Exception:
            process.kill()
            await process.wait()
            raise
        argv_hash = hashlib.sha256(json.dumps(
            request.argv, ensure_ascii=False, separators=(",", ":")
        ).encode()).hexdigest()
        handle = ProcessHandle(
            request.process_id, process.pid, process.pid,
            f"{process.pid}:{started_at.isoformat()}", argv_hash,
            str(request.cwd), started_at,
        )
        assert process.stdout is not None and process.stderr is not None
        stdout_buffer = _LogBuffer(request.max_output_bytes)
        stderr_buffer = _LogBuffer(request.max_output_bytes)
        self._live[request.process_id] = _LiveProcess(
            process, request, handle, job_handle, stdout_buffer, stderr_buffer,
            asyncio.create_task(self._drain(process.stdout, stdout_buffer)),
            asyncio.create_task(self._drain(process.stderr, stderr_buffer)),
        )
        return handle

    @staticmethod
    def _windows_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
        suffix = os.path.splitext(argv[0])[1].lower()
        if suffix not in {".cmd", ".bat"}:
            return argv
        forbidden = set("&|<>^%!?\r\n")
        if any(character in forbidden for item in argv for character in item):
            raise PermissionError(
                "Windows batch arguments contain command-interpreter metacharacters"
            )
        command = subprocess.list2cmdline(argv)
        comspec = os.environ.get("ComSpec") or os.environ.get("COMSPEC")
        if not comspec:
            raise RuntimeError("Windows ComSpec is unavailable")
        return (comspec, "/d", "/s", "/c", command)


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
        termination_signal: str | None = None
        if live.process.returncode is None:
            try:
                live.process.send_signal(signal.CTRL_BREAK_EVENT)
                termination_signal = "CTRL_BREAK"
            except OSError:
                # A detached/headless process may not share a console even
                # though it owns a process group.  The Job Object remains the
                # authoritative process-tree boundary in that case.
                self._terminate_job(live.job_handle, 1)
                termination_signal = "JOB_TERMINATE"
                await live.process.wait()
            else:
                try:
                    await asyncio.wait_for(
                        live.process.wait(), timeout=grace_seconds
                    )
                except TimeoutError:
                    self._terminate_job(live.job_handle, 1)
                    termination_signal = "JOB_TERMINATE"
                    await live.process.wait()
        return await self._finish(
            live, live.process.returncode, status, termination_signal
        )

    async def _finish(
        self, live: _LiveProcess, return_code: int | None,
        status: ProcessExitStatus, termination_signal: str | None,
    ) -> ProcessResult:
        # Root exit does not prove its descendants exited. Closing a
        # KILL_ON_JOB_CLOSE job makes the tree terminal before logs are finalized.
        self._close_job(live.job_handle)
        await asyncio.gather(live.stdout_task, live.stderr_task)
        self._live.pop(live.handle.process_id, None)
        result = ProcessResult(
            live.handle.process_id, status, return_code,
            live.stdout_buffer.output(), live.stderr_buffer.output(),
            live.handle.started_at, datetime.now(timezone.utc), termination_signal,
        )
        self._completed[live.handle.process_id] = (
            live.handle, result, live.stdout_buffer, live.stderr_buffer
        )
        return result

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
            left.pid == right.pid and left.pgid == right.pgid
            and left.birth_marker == right.birth_marker
            and left.argv_hash == right.argv_hash and left.cwd == right.cwd
        )

    @classmethod
    def _validate_handle(cls, supplied: ProcessHandle, owned: ProcessHandle) -> None:
        if not cls._handles_match(supplied, owned):
            raise ValueError("process handle identity does not match the live process")

    @staticmethod
    async def _drain(stream: asyncio.StreamReader, buffer: _LogBuffer) -> None:
        while chunk := await stream.read(64 * 1024):
            buffer.append(chunk)

    @staticmethod
    def _create_and_assign_job(pid: int) -> int:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount",
            )]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        process_handle = None
        try:
            info = EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle = kernel32.OpenProcess(0x0100 | 0x0400, False, pid)
            if not process_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel32.AssignProcessToJobObject(job, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
            return int(job)
        except Exception:
            kernel32.CloseHandle(job)
            raise
        finally:
            if process_handle:
                kernel32.CloseHandle(process_handle)

    @staticmethod
    def _terminate_job(job_handle: int, exit_code: int) -> None:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        if not kernel32.TerminateJobObject(job_handle, exit_code):
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _close_job(job_handle: int) -> None:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        if not kernel32.CloseHandle(job_handle):
            raise ctypes.WinError(ctypes.get_last_error())
