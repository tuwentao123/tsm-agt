"""Cross-process leases implemented with POSIX flock."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
)


class PosixCrossProcessLock:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.posix-cross-process-lock",
        adapter_version="0.1.0",
        port_name="CrossProcessLockPort",
        port_version="1.0",
        capabilities=frozenset({"local-process", "flock", "cancellation-safe"}),
    )

    def __init__(self, poll_interval_seconds: float = 0.01) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("lock poll interval must be positive")
        self._poll_interval_seconds = poll_interval_seconds
        self._started = False
        self._leases: dict[str, int] = {}

    async def start(self, context: AdapterContext) -> None:
        try:
            import fcntl  # noqa: F401
        except ImportError as error:
            raise RuntimeError(
                "POSIX cross-process lock requires fcntl.flock"
            ) from error
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "POSIX flock adapter ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        for lease_id in tuple(self._leases):
            await self.release(lease_id)
        self._started = False

    async def acquire(self, lock_file: Path) -> str:
        if not self._started:
            raise RuntimeError("POSIX cross-process lock adapter is not started")
        import fcntl

        descriptor = self._open_lock_file(lock_file)
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(self._poll_interval_seconds)
        except BaseException:
            os.close(descriptor)
            raise
        lease_id = f"lock-lease-{uuid4().hex}"
        self._leases[lease_id] = descriptor
        return lease_id

    async def release(self, lease_id: str) -> None:
        import fcntl

        descriptor = self._leases.pop(lease_id, None)
        if descriptor is None:
            raise LookupError(f"cross-process lock lease not found: {lease_id}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @staticmethod
    def _open_lock_file(path: Path) -> int:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        return descriptor
