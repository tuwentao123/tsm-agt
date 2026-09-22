"""Microkernel-owned process lifecycle records and errors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from tsm_agt.ports import ProcessCommandResult, ProcessHandle, ProcessResult


class ProcessSandboxDenied(PermissionError):
    pass


class BackgroundProcessState(StrEnum):
    RUNNING = "RUNNING"
    EXITED = "EXITED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    ORPHANED = "ORPHANED"

    @property
    def is_terminal(self) -> bool:
        return self is not self.RUNNING


@dataclass(frozen=True, slots=True)
class BackgroundProcessRecord:
    process_id: str
    turn_id: str
    handle: ProcessHandle
    state: BackgroundProcessState
    max_lifetime_seconds: float
    deadline_at: datetime
    stop_on_task_end: bool
    exit_code: int | None = None
    termination_signal: str | None = None
    finished_at: datetime | None = None

    def finish(self, result: ProcessResult) -> BackgroundProcessRecord:
        states = {
            "exited": BackgroundProcessState.EXITED,
            "cancelled": BackgroundProcessState.CANCELLED,
            "timed_out": BackgroundProcessState.TIMED_OUT,
        }
        return BackgroundProcessRecord(
            self.process_id, self.turn_id, self.handle, states[result.status.value],
            self.max_lifetime_seconds, self.deadline_at, self.stop_on_task_end,
            result.exit_code, result.termination_signal, result.finished_at,
        )

    def orphan(self, when: datetime) -> BackgroundProcessRecord:
        return BackgroundProcessRecord(
            self.process_id, self.turn_id, self.handle,
            BackgroundProcessState.ORPHANED, self.max_lifetime_seconds,
            self.deadline_at, self.stop_on_task_end, finished_at=when,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "process_id": self.process_id, "turn_id": self.turn_id,
            "state": self.state.value,
            "max_lifetime_seconds": self.max_lifetime_seconds,
            "deadline_at": self.deadline_at.isoformat(),
            "stop_on_task_end": self.stop_on_task_end,
            "exit_code": self.exit_code,
            "termination_signal": self.termination_signal,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "handle": {
                "process_id": self.handle.process_id, "pid": self.handle.pid,
                "pgid": self.handle.pgid, "birth_marker": self.handle.birth_marker,
                "argv_hash": self.handle.argv_hash, "cwd": self.handle.cwd,
                "started_at": self.handle.started_at.isoformat(),
            },
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> BackgroundProcessRecord:
        raw_handle = data["handle"]
        if not isinstance(raw_handle, Mapping):
            raise ValueError("background process handle must be an object")
        handle = ProcessHandle(
            process_id=str(raw_handle["process_id"]), pid=int(raw_handle["pid"]),
            pgid=int(raw_handle["pgid"]), birth_marker=str(raw_handle["birth_marker"]),
            argv_hash=str(raw_handle["argv_hash"]), cwd=str(raw_handle["cwd"]),
            started_at=datetime.fromisoformat(str(raw_handle["started_at"])),
        )
        finished = data.get("finished_at")
        return cls(
            process_id=str(data["process_id"]), turn_id=str(data["turn_id"]),
            handle=handle, state=BackgroundProcessState(str(data["state"])),
            max_lifetime_seconds=float(data["max_lifetime_seconds"]),
            deadline_at=datetime.fromisoformat(str(data["deadline_at"])),
            stop_on_task_end=bool(data.get("stop_on_task_end", True)),
            exit_code=int(data["exit_code"]) if data.get("exit_code") is not None else None,
            termination_signal=(str(data["termination_signal"]) if data.get("termination_signal") else None),
            finished_at=datetime.fromisoformat(str(finished)) if finished else None,
        )


# Backward-compatible public name; new code uses the provider-neutral port type.
SupervisedProcessResult = ProcessCommandResult
