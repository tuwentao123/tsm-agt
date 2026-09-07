"""Cross-process leases implemented with the Windows CRT file lock."""

from __future__ import annotations

import asyncio
import errno
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
)


class WindowsCrossProcessLock:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.windows-cross-process-lock",
        adapter_version="0.1.0",
        port_name="CrossProcessLockPort",
        port_version="1.0",
        capabilities=frozenset(
            {"local-process", "windows-byte-range-lock", "cancellation-safe"}
        ),
    )

    def __init__(self, poll_interval_seconds: float = 0.01) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("lock poll interval must be positive")
        self._poll_interval_seconds = poll_interval_seconds
        self._started = False
        self._leases: dict[str, int] = {}

    async def start(self, context: AdapterContext) -> None:
        if os.name != "nt":
            raise RuntimeError(
                "Windows cross-process lock can start only on Windows"
            )
        import msvcrt  # noqa: F401

        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "Windows file-lock adapter ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        for lease_id in tuple(self._leases):
            await self.release(lease_id)
        self._started = False

    async def acquire(self, lock_file: Path) -> str:
        if not self._started:
            raise RuntimeError("Windows cross-process lock adapter is not started")
        import msvcrt

        descriptor = self._open_lock_file(lock_file)
        try:
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    await asyncio.sleep(self._poll_interval_seconds)
        except BaseException:
            os.close(descriptor)
            raise
        lease_id = f"lock-lease-{uuid4().hex}"
        self._leases[lease_id] = descriptor
        return lease_id

    async def release(self, lease_id: str) -> None:
        import msvcrt

        descriptor = self._leases.pop(lease_id, None)
        if descriptor is None:
            raise LookupError(f"cross-process lock lease not found: {lease_id}")
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(descriptor)

    @staticmethod
    def _open_lock_file(path: Path) -> int:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        return descriptor
