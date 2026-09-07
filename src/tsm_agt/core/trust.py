"""Project trust levels and workspace identity binding."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from tsm_agt.ports import WorkspacePathPort


class ProjectTrustLevel(StrEnum):
    UNTRUSTED = "UNTRUSTED"
    TRUSTED_READ = "TRUSTED_READ"
    TRUSTED_BUILD = "TRUSTED_BUILD"
    TRUSTED_FULL = "TRUSTED_FULL"

    @property
    def allows_project_execution(self) -> bool:
        return self in {self.TRUSTED_BUILD, self.TRUSTED_FULL}


@dataclass(frozen=True, slots=True)
class ProjectTrustBinding:
    workspace: str
    subject: str
    fingerprint: str
    level: ProjectTrustLevel
    updated_at: datetime

    def to_data(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace, "subject": self.subject,
            "fingerprint": self.fingerprint, "level": self.level.value,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ProjectTrustBinding:
        return cls(
            workspace=str(data["workspace"]), subject=str(data["subject"]),
            fingerprint=str(data["fingerprint"]),
            level=ProjectTrustLevel(str(data["level"])),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
        )


_IDENTITY_PATHS = (
    ".git/config", ".agent/config.yaml", "AGENTS.md", "pyproject.toml",
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "settings.gradle", "settings.gradle.kts", "build.gradle",
    "build.gradle.kts", "gradlew", "Cargo.toml", "go.mod",
)
_MAX_IDENTITY_FILE_BYTES = 2_000_000


def workspace_fingerprint(
    workspace: Path, path_service: WorkspacePathPort,
) -> str:
    root = path_service.normalize_workspace(workspace)
    digest = hashlib.sha256()
    digest.update(path_service.workspace_key(root).encode("utf-8"))
    for relative_name in _IDENTITY_PATHS:
        try:
            resolved = path_service.resolve_access_path(root, relative_name)
        except (PermissionError, ValueError):
            # Linked/reparse identity inputs outside the workspace are untrusted
            # and must neither be read nor prevent opening the project.
            continue
        path = resolved.path
        if not path.is_file() or path_service.is_link_like(path):
            continue
        size = path.stat().st_size
        digest.update(relative_name.encode("utf-8"))
        digest.update(str(size).encode("ascii"))
        if size <= _MAX_IDENTITY_FILE_BYTES:
            digest.update(path.read_bytes())
        else:
            digest.update(b"oversized-identity-file")
    return digest.hexdigest()


def new_trust_binding(
    workspace: Path, subject: str, level: ProjectTrustLevel,
    path_service: WorkspacePathPort,
) -> ProjectTrustBinding:
    root = path_service.normalize_workspace(workspace)
    return ProjectTrustBinding(
        path_service.workspace_key(root), subject,
        workspace_fingerprint(root, path_service), level,
        datetime.now(timezone.utc),
    )
