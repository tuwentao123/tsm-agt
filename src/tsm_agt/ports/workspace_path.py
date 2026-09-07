"""Platform boundary for workspace path identity and containment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .adapter import RuntimeAdapter


@dataclass(frozen=True, slots=True)
class ResolvedWorkspacePath:
    workspace: Path
    path: Path
    relative_path: str
    canonical_key: str


class WorkspacePathPort(RuntimeAdapter, Protocol):
    """Resolve workspace-relative read/mutation paths without escaping root."""

    def normalize_workspace(self, workspace: Path) -> Path: ...

    def workspace_key(self, workspace: Path) -> str: ...

    def is_link_like(self, path: Path) -> bool: ...

    def resolve_mutation_path(
        self, workspace: Path, relative_path: str,
    ) -> ResolvedWorkspacePath: ...

    def resolve_access_path(
        self, workspace: Path, relative_path: str,
    ) -> ResolvedWorkspacePath: ...
