"""In-process cancellation resources for one Task execution generation.

Spec: ``task-cancellation-spec.md`` §5.1. This structure exists **only in
memory**: it is never written to a checkpoint or an event payload, and process
exit discards it. It is therefore orthogonal to the durable cancellation intent
(which lives in ``task.state_changed``).

Deviation from the spec signature: ``register_process`` also takes a ``stop``
callable, because a bare ``ProcessHandle`` cannot stop itself — the executor
owns that operation (``executor.stop_process``).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

#: Per-resource grace, per spec D2. The whole close is bounded by the caller's
#: ``timeout_seconds`` (default 10s).
_PER_RESOURCE_TIMEOUT_SECONDS = 3.0

logger = logging.getLogger("tsm_agt.cancellation")


@dataclass(slots=True)
class CancellationMetrics:
    """Minimal process-local counters for Phase 3 observability.

    Not persisted: cancel *intent* and outcomes live in events; these counters
    are only a cheap operational view for logs/dashboards.
    """

    cancel_requested: int = 0
    interrupting_settled: int = 0
    stream_released: int = 0
    active_scopes: int = 0
    cleanup_completed: int = 0
    cleanup_timed_out: int = 0
    cleanup_failed: int = 0
    cleanup_duration_seconds_total: float = 0.0


#: Process-local singleton. Tests must assert deltas, not absolutes.
CANCELLATION_METRICS = CancellationMetrics()


def note_cancel_requested(task_id: str) -> None:
    CANCELLATION_METRICS.cancel_requested += 1
    logger.info("task_cancel_requested task_id=%s", task_id)


def note_interrupting_settled(task_id: str, intent: str) -> None:
    CANCELLATION_METRICS.interrupting_settled += 1
    logger.info("task_cancel_propagated task_id=%s intent=%s", task_id, intent)


def note_stream_released(task_id: str) -> None:
    CANCELLATION_METRICS.stream_released += 1
    logger.info("stream_released task_id=%s", task_id)


def note_scope_opened() -> None:
    CANCELLATION_METRICS.active_scopes += 1


def note_scope_closed(report: CleanupReport) -> None:
    """Record one scope close, including the leak/timeout signals (Phase 3)."""
    CANCELLATION_METRICS.active_scopes = max(
        0, CANCELLATION_METRICS.active_scopes - 1
    )
    CANCELLATION_METRICS.cleanup_duration_seconds_total += report.duration_seconds
    if report.timed_out:
        logger.warning("cleanup_timeout resources=%s", report.timed_out)
    if report.failed:
        logger.warning("cleanup_failed resources=%s", report.failed)


class CancellationSignal(Protocol):
    """Spec §5 facade over the existing cancellation signals.

    A facade, not a new primitive: implementations delegate to
    ``CancelTaskInput`` (task), ``process.cancel_requested`` (process) or the
    model stream's ``stopped`` Event.
    """

    def request(self, reason: str) -> None: ...
    def requested(self) -> bool: ...
    async def wait(self) -> None: ...


@dataclass(slots=True)
class TaskCancellationSignal:
    """Task-level signal backed by the Kernel's in-memory cancel set."""

    task_id: str
    _is_requested: Callable[[str], bool]
    _request: Callable[[str, str], None]

    def request(self, reason: str) -> None:
        self._request(self.task_id, reason)

    def requested(self) -> bool:
        return self._is_requested(self.task_id)

    async def wait(self) -> None:
        while not self.requested():
            await asyncio.sleep(0.05)


@dataclass(slots=True)
class CleanupReport:
    closed: list[str] = field(default_factory=list)
    timed_out: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    duration_seconds: float = 0.0


@dataclass(slots=True)
class _Entry:
    name: str
    cleanup: Callable[[], Awaitable[None]]


class CancellationScope:
    """One Task execution generation's in-process resources. Not persisted."""

    def __init__(self) -> None:
        self._entries: list[_Entry] = []
        self._closed = False
        self._report: CleanupReport | None = None

    # ---- registration -------------------------------------------------

    def register_cleanup(
        self, name: str, fn: Callable[[], Awaitable[None]],
    ) -> bool:
        return self._register(name, fn)

    def register_task(self, task: asyncio.Task, *, name: str) -> bool:
        async def cleanup() -> None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return self._register(name, cleanup)

    def register_thread(self, stopped: threading.Event, *, name: str) -> bool:
        async def cleanup() -> None:
            # Only signal; never join, which would block the event loop.
            stopped.set()
        return self._register(name, cleanup)

    def register_process(
        self, handle: Any, *, name: str,
        stop: Callable[[Any], Any],
    ) -> bool:
        async def cleanup() -> None:
            result = stop(handle)
            if asyncio.iscoroutine(result):
                await result
        return self._register(name, cleanup)

    def register_generator(self, agen: Any, *, name: str) -> bool:
        async def cleanup() -> None:
            await agen.aclose()
        return self._register(name, cleanup)

    def register_stream(self, closable: Any, *, name: str) -> bool:
        async def cleanup() -> None:
            aclose = getattr(closable, "aclose", None)
            if aclose is not None:
                await aclose()
                return
            close = getattr(closable, "close", None)
            if close is not None:
                close()
        return self._register(name, cleanup)

    def _register(
        self, name: str, cleanup: Callable[[], Awaitable[None]],
    ) -> bool:
        if self._closed:
            # A closed scope must not retain new resources: clean up now,
            # best-effort, without blocking the caller.
            _schedule_quiet(cleanup)
            return False
        self._entries.append(_Entry(name, cleanup))
        return True

    # ---- close --------------------------------------------------------

    async def close_all(self, *, timeout_seconds: float = 10.0) -> CleanupReport:
        """Release every registered resource. Idempotent by construction."""
        if self._report is not None:
            return self._report
        self._closed = True
        report = CleanupReport()
        started = time.monotonic()
        deadline = started + max(0.0, timeout_seconds)
        for entry in self._entries:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                report.timed_out.append(entry.name)
                continue
            budget = min(_PER_RESOURCE_TIMEOUT_SECONDS, remaining)
            try:
                await asyncio.wait_for(entry.cleanup(), timeout=budget)
                report.closed.append(entry.name)
            except asyncio.TimeoutError:
                report.timed_out.append(entry.name)
            except Exception as error:  # recorded, never raised
                report.failed.append((entry.name, type(error).__name__))
        report.duration_seconds = time.monotonic() - started
        self._entries.clear()
        self._report = report
        CANCELLATION_METRICS.cleanup_completed += len(report.closed)
        CANCELLATION_METRICS.cleanup_timed_out += len(report.timed_out)
        CANCELLATION_METRICS.cleanup_failed += len(report.failed)
        logger.info(
            "cleanup_completed closed=%d timed_out=%d failed=%d duration=%.3f",
            len(report.closed), len(report.timed_out), len(report.failed),
            report.duration_seconds,
        )
        return report


def _schedule_quiet(cleanup: Callable[[], Awaitable[None]]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def run() -> None:
        try:
            await cleanup()
        except Exception:
            return

    loop.create_task(run())


__all__ = [
    "CANCELLATION_METRICS",
    "CancellationMetrics",
    "CancellationScope",
    "CancellationSignal",
    "CleanupReport",
    "TaskCancellationSignal",
    "note_cancel_requested",
    "note_interrupting_settled",
    "note_scope_closed",
    "note_scope_opened",
    "note_stream_released",
]
