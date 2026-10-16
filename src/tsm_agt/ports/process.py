"""Provider-neutral contracts for supervised operating-system processes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .adapter import RuntimeAdapter


class ProcessExitStatus(StrEnum):
    EXITED = "exited"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    FAILED_TO_START = "failed_to_start"


def process_result_succeeded(data: Mapping[str, object]) -> bool:
    """Read command success from current or legacy serialized process data."""
    explicit = data.get("succeeded")
    if isinstance(explicit, bool):
        return explicit
    return data.get("status") == ProcessExitStatus.EXITED.value and data.get(
        "exit_code"
    ) == 0


@dataclass(frozen=True, slots=True)
class ProcessEnvironmentPolicy:
    inherit_host_environment: bool = False
    allowed_host_variables: tuple[str, ...] = ()
    blocked_host_variables: tuple[str, ...] = ()
    runtime_trust_mode: Literal["isolated", "governed"] = "isolated"


@dataclass(frozen=True, slots=True)
class ProcessStartRequest:
    process_id: str
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str] = field(default_factory=dict)
    environment_policy: ProcessEnvironmentPolicy = field(default_factory=ProcessEnvironmentPolicy)
    max_output_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        if not self.process_id.strip():
            raise ValueError("process_id must not be empty")
        if not self.argv or any(not item or "\x00" in item for item in self.argv):
            raise ValueError("argv must contain non-empty values without NUL bytes")
        if not self.cwd.is_absolute():
            raise ValueError("process cwd must be absolute")
        if self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        for name, value in self.environment.items():
            if not name or "=" in name or "\x00" in name or "\x00" in value:
                raise ValueError("process environment contains an invalid entry")


@dataclass(frozen=True, slots=True)
class ProcessHandle:
    process_id: str
    pid: int
    # On POSIX LocalProcessExecutor this is the child PID because it starts a
    # new session. Windows keeps the same value for its CREATE_NEW_PROCESS_GROUP
    # handle. It remains an int: a handle always has a known group leader.
    pgid: int
    birth_marker: str
    argv_hash: str
    cwd: str
    started_at: datetime


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    text: str
    total_bytes: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ProcessResult:
    process_id: str
    status: ProcessExitStatus
    exit_code: int | None
    stdout: ProcessOutput
    stderr: ProcessOutput
    started_at: datetime
    finished_at: datetime
    termination_signal: str | None = None

    @property
    def succeeded(self) -> bool:
        """Whether the completed operating-system process exited successfully."""
        return self.status is ProcessExitStatus.EXITED and self.exit_code == 0

    @property
    def failure_code(self) -> str | None:
        if self.succeeded:
            return None
        if self.status is ProcessExitStatus.TIMED_OUT:
            return "PROCESS_TIMED_OUT"
        if self.status is ProcessExitStatus.CANCELLED:
            return "PROCESS_CANCELLED"
        if self.status is ProcessExitStatus.FAILED_TO_START:
            return "PROCESS_FAILED_TO_START"
        return "PROCESS_EXIT_NON_ZERO"

    def to_data(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "execution_state": (
                "succeeded" if self.succeeded else "failed_known"
            ),
            "succeeded": self.succeeded,
            "failure_code": self.failure_code,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "termination_signal": self.termination_signal,
            "stdout": {
                "text": self.stdout.text,
                "total_bytes": self.stdout.total_bytes,
                "truncated": self.stdout.truncated,
            },
            "stderr": {
                "text": self.stderr.text,
                "total_bytes": self.stderr.total_bytes,
                "truncated": self.stderr.truncated,
            },
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ProcessCommandResult:
    """A definitive foreground command outcome returned to a process tool."""

    handle: ProcessHandle
    result: ProcessResult

    @property
    def succeeded(self) -> bool:
        return self.result.succeeded

    def to_data(self) -> dict[str, object]:
        return {"mode": "foreground", **self.result.to_data()}


@dataclass(frozen=True, slots=True)
class BackgroundProcessStartResult:
    """Proof that a process was started under Runtime supervision.

    ``core.run_command`` always returns this start fact.  A process may exit
    between ``start_process`` and the Runtime's first non-blocking poll, so
    ``state`` is deliberately not restricted to ``RUNNING``.
    """

    process_id: str
    state: str
    started_at: datetime
    deadline_at: datetime
    max_lifetime_seconds: float
    stop_on_task_end: bool
    exit_code: int | None = None
    termination_signal: str | None = None
    finished_at: datetime | None = None

    @property
    def started(self) -> bool:
        # This result is constructed only after the operating system accepted
        # the spawn.  A fast natural exit does not erase that fact.
        return True

    @property
    def execution_state(self) -> str:
        if self.state == "RUNNING":
            return "running"
        if self.state == "EXITED" and self.exit_code == 0:
            return "succeeded"
        return "failed_known"

    def to_data(self) -> dict[str, object]:
        return {
            "mode": "managed",
            "execution_state": self.execution_state,
            "started": self.started,
            "process_id": self.process_id,
            "state": self.state,
            "started_at": self.started_at.isoformat(),
            "deadline_at": self.deadline_at.isoformat(),
            "max_lifetime_seconds": self.max_lifetime_seconds,
            "stop_on_task_end": self.stop_on_task_end,
            "exit_code": self.exit_code,
            "termination_signal": self.termination_signal,
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at else None
            ),
        }


@dataclass(frozen=True, slots=True)
class ProcessWaitResult:
    """One bounded observation of a Runtime-managed process."""

    process_id: str
    state: str
    waited_seconds: float
    stdout: ProcessOutput
    stderr: ProcessOutput
    result: ProcessResult | None = None

    def to_data(self) -> dict[str, object]:
        if self.result is not None:
            data = self.result.to_data()
            data["state"] = self.state
        else:
            data = {
                "process_id": self.process_id,
                "execution_state": "running",
                "succeeded": None,
                "failure_code": None,
                "status": None,
                "exit_code": None,
                "termination_signal": None,
                "stdout": {
                    "text": self.stdout.text,
                    "total_bytes": self.stdout.total_bytes,
                    "truncated": self.stdout.truncated,
                },
                "stderr": {
                    "text": self.stderr.text,
                    "total_bytes": self.stderr.total_bytes,
                    "truncated": self.stderr.truncated,
                },
            }
            data["state"] = self.state
        data["waited_seconds"] = self.waited_seconds
        return data


@dataclass(frozen=True, slots=True)
class ProcessLogChunk:
    text: str
    cursor: int
    next_cursor: int
    total_bytes: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ProcessLogs:
    stdout: ProcessLogChunk
    stderr: ProcessLogChunk


class ProcessExecutorPort(RuntimeAdapter, Protocol):
    def prepare_environment(
        self,
        environment: Mapping[str, str],
        policy: ProcessEnvironmentPolicy | None = None,
    ) -> Mapping[str, str]: ...

    async def start_process(self, request: ProcessStartRequest) -> ProcessHandle: ...

    async def wait_process(self, handle: ProcessHandle) -> ProcessResult: ...

    async def poll_process(self, handle: ProcessHandle) -> ProcessResult | None: ...

    async def read_process_logs(
        self, handle: ProcessHandle, stdout_cursor: int, stderr_cursor: int
    ) -> ProcessLogs: ...

    def owns_process(self, handle: ProcessHandle) -> bool: ...

    async def stop_process(
        self, handle: ProcessHandle, grace_seconds: float, status: ProcessExitStatus
    ) -> ProcessResult: ...
