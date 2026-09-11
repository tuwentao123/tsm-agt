"""POSIX durable workspace filesystem operations."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
)


class PosixWorkspaceFilesystem:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.posix-workspace-filesystem",
        adapter_version="0.1.0", port_name="WorkspaceFilesystemPort",
        port_version="1.0",
        capabilities=frozenset({"atomic-replace", "durable-delete", "directory-fsync"}),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        if os.name == "nt":
            raise RuntimeError("POSIX workspace filesystem cannot start on Windows")
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "POSIX workspace filesystem ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    def replace(self, source: Path, target: Path) -> None:
        self._require_started()
        os.replace(source, target)

    def unlink(self, path: Path) -> None:
        self._require_started()
        path.unlink()

    def make_directory(self, path: Path) -> None:
        """Create exactly one directory; the core validates its location."""
        self._require_started()
        path.mkdir()

    def remove_directory(self, path: Path) -> None:
        """Remove exactly one empty directory."""
        self._require_started()
        path.rmdir()

    def sync_directory(self, directory: Path) -> None:
        self._require_started()
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def protect_private_path(self, path: Path) -> None:
        self._require_started()
        os.chmod(path, 0o700 if path.is_dir() else 0o600)

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("POSIX workspace filesystem is not started")
