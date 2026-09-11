"""Platform boundary for workspace path identity and containment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .adapter import RuntimeAdapter


_SENSITIVE_READ_NAMES = frozenset({
    ".git", ".ssh", ".npmrc", ".pypirc", "credentials",
    "credentials.json", "id_rsa", "id_ed25519",
})
_SENSITIVE_READ_SUFFIXES = frozenset({
    ".jks", ".key", ".keystore", ".p12", ".pem", ".pfx",
})


def is_sensitive_read_path(path: Path) -> bool:
    """Apply one secret-path rule before approval and again during reads."""
    for part in path.parts:
        lowered = part.casefold()
        if lowered in _SENSITIVE_READ_NAMES:
            return True
        if lowered == ".env" or lowered.startswith(".env."):
            return True
        if Path(lowered).suffix in _SENSITIVE_READ_SUFFIXES:
            return True
    return False


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

    def is_same_or_descendant(self, path: Path, root: Path) -> bool:
        """Compare canonical path containment without granting access."""
        ...

    def resolve_mutation_path(
        self, workspace: Path, relative_path: str,
    ) -> ResolvedWorkspacePath: ...

    def resolve_create_path(
        self, workspace: Path, relative_path: str,
    ) -> ResolvedWorkspacePath:
        """Resolve a file-creation path whose ordinary parents may be missing."""
        ...

    def resolve_access_path(
        self, workspace: Path, relative_path: str,
    ) -> ResolvedWorkspacePath: ...

    def resolve_read_path(
        self, workspace: Path, requested_path: str,
        additional_roots: tuple[Path, ...] = (),
    ) -> ResolvedWorkspacePath:
        """Resolve a read path against the workspace or explicit extra roots."""
        ...

    def external_read_approval_root(
        self, workspace: Path, requested_path: str,
    ) -> Path | None:
        """Return a narrow canonical external directory, or None if unsafe."""
        ...
