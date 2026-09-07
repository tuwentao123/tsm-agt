"""Provider-neutral contracts for supervised operating-system processes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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


@dataclass(frozen=True, slots=True)
class ProcessStartRequest:
    process_id: str
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str] = field(default_factory=dict)
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
        self, environment: Mapping[str, str]
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
