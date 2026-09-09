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

    def is_same_or_descendant(self, path: Path, root: Path) -> bool:
        """Use canonical POSIX identity; this comparison grants no access."""
        self._require_started()
        candidate = path.expanduser().resolve(strict=False)
        ancestor = root.expanduser().resolve(strict=False)
        try:
            candidate.relative_to(ancestor)
        except ValueError:
            return False
        return True

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

    def resolve_read_path(
        self, workspace: Path, requested_path: str,
        additional_roots: tuple[Path, ...] = (),
    ) -> ResolvedWorkspacePath:
        self._require_started()
        workspace_root = self.normalize_workspace(workspace)
        candidate = Path(requested_path).expanduser()
        resolved = (
            candidate.resolve(strict=False)
            if candidate.is_absolute()
            else (workspace_root / candidate).resolve(strict=False)
        )
        roots = (workspace_root,) + tuple(
            self.normalize_workspace(root) for root in additional_roots
        )
        for root in roots:
            try:
                relative = resolved.relative_to(root)
            except ValueError:
                continue
            return ResolvedWorkspacePath(
                root, resolved, relative.as_posix(), str(resolved)
            )
        raise ValueError("path escapes the approved read roots")

    def external_read_approval_root(
        self, workspace: Path, requested_path: str,
    ) -> Path | None:
        self._require_started()
        workspace_root = self.normalize_workspace(workspace)
        candidate = Path(requested_path).expanduser()
        candidate = (
            candidate.resolve(strict=False)
            if candidate.is_absolute()
            else (workspace_root / candidate).resolve(strict=False)
        )
        if not candidate.exists():
            return None
        root = candidate if candidate.is_dir() else candidate.parent
        root = root.resolve(strict=True)
        home = Path.home().resolve(strict=True)
        if root == home or root == Path(root.anchor):
            return None
        return root

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("POSIX workspace path is not started")
