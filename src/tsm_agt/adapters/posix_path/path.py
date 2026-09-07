"""POSIX workspace path identity and containment."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
    ResolvedWorkspacePath,
)


class PosixWorkspacePath:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.posix-workspace-path",
        adapter_version="0.1.0", port_name="WorkspacePathPort",
        port_version="1.0",
        capabilities=frozenset({
            "canonical-path-key", "workspace-containment",
            "symlink-aware-parent",
        }),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        if os.name == "nt":
            raise RuntimeError("POSIX workspace path cannot start on Windows")
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "POSIX workspace path ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    def normalize_workspace(self, workspace: Path) -> Path:
        self._require_started()
        root = workspace.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"workspace is not a directory: {root}")
        return root

    def workspace_key(self, workspace: Path) -> str:
        return str(self.normalize_workspace(workspace))

    def is_link_like(self, path: Path) -> bool:
        self._require_started()
        return path.is_symlink()

    def resolve_mutation_path(
        self, workspace: Path, relative_path: str,
    ) -> ResolvedWorkspacePath:
        self._require_started()
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise ValueError("absolute workspace mutation paths are not allowed")
        root = self.normalize_workspace(workspace)
        path = root / candidate
        try:
            parent = path.parent.resolve(strict=True)
        except FileNotFoundError as error:
            raise ValueError(
                "workspace mutation parent directory does not exist"
            ) from error
        try:
            parent.relative_to(root)
        except ValueError as error:
            raise ValueError("workspace mutation path escapes the workspace") from error
        if not parent.is_dir():
            raise ValueError("workspace mutation parent is not a directory")
        canonical = parent / path.name
        relative = canonical.relative_to(root).as_posix()
        return ResolvedWorkspacePath(root, canonical, relative, str(canonical))

    def resolve_access_path(
        self, workspace: Path, relative_path: str,
    ) -> ResolvedWorkspacePath:
        self._require_started()
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise ValueError("absolute workspace paths are not allowed")
        root = self.normalize_workspace(workspace)
        resolved = (root / candidate).resolve(strict=False)
        try:
            relative = resolved.relative_to(root)
        except ValueError as error:
            raise ValueError("path escapes the workspace") from error
        return ResolvedWorkspacePath(
            root, resolved, relative.as_posix(), str(resolved)
        )

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("POSIX workspace path is not started")
