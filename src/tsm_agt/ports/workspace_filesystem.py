"""Platform durability boundary for workspace file transactions."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .adapter import RuntimeAdapter


class WorkspaceFilesystemPort(RuntimeAdapter, Protocol):
    """Perform already-authorized local file operations durably."""

    def replace(self, source: Path, target: Path) -> None: ...

    def unlink(self, path: Path) -> None: ...

    def sync_directory(self, directory: Path) -> None: ...

    def protect_private_path(self, path: Path) -> None: ...
