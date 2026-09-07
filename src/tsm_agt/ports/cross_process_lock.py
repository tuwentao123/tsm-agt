"""Platform-neutral cross-process lock contract."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .adapter import RuntimeAdapter


class CrossProcessLockPort(RuntimeAdapter, Protocol):
    """Acquire opaque leases for lock files shared by local processes."""

    async def acquire(self, lock_file: Path) -> str: ...

    async def release(self, lease_id: str) -> None: ...
