"""Optional trusted project instructions with a single explicit source."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tsm_agt.ports import WorkspacePathPort

from .configuration import canonical_hash
from .trust import ProjectTrustLevel


PROJECT_INSTRUCTIONS_PATH = ".agent/projectInstructions.md"
MAX_PROJECT_INSTRUCTIONS_BYTES = 32 * 1024


@dataclass(frozen=True, slots=True)
class ProjectInstructionsSnapshot:
    path: str
    discovered: bool
    activated: bool
    content: str | None = None
    content_hash: str | None = None
    reason: str = ""

    def event_data(self) -> dict[str, object]:
        return {
            "path": self.path, "discovered": self.discovered,
            "activated": self.activated, "content_hash": self.content_hash,
            "byte_count": len(self.content.encode("utf-8")) if self.content else 0,
            "reason": self.reason, "body_persisted": False,
        }


def load_project_instructions(
    workspace: Path, trust: ProjectTrustLevel, paths: WorkspacePathPort,
) -> ProjectInstructionsSnapshot:
    root = paths.normalize_workspace(workspace)
    try:
        resolved = paths.resolve_access_path(root, PROJECT_INSTRUCTIONS_PATH).path
    except (PermissionError, ValueError):
        return ProjectInstructionsSnapshot(
            PROJECT_INSTRUCTIONS_PATH, False, False, reason="invalid_path"
        )
    if not resolved.exists() or not resolved.is_file():
        return ProjectInstructionsSnapshot(
            PROJECT_INSTRUCTIONS_PATH, False, False, reason="not_found"
        )
    if paths.is_link_like(resolved):
        return ProjectInstructionsSnapshot(
            PROJECT_INSTRUCTIONS_PATH, True, False, reason="link_not_allowed"
        )
    if trust is ProjectTrustLevel.UNTRUSTED:
        return ProjectInstructionsSnapshot(
            PROJECT_INSTRUCTIONS_PATH, True, False, reason="project_untrusted"
        )
    try:
        raw = resolved.read_bytes()
    except OSError:
        return ProjectInstructionsSnapshot(
            PROJECT_INSTRUCTIONS_PATH, True, False, reason="read_failed"
        )
    if len(raw) > MAX_PROJECT_INSTRUCTIONS_BYTES:
        return ProjectInstructionsSnapshot(
            PROJECT_INSTRUCTIONS_PATH, True, False, reason="file_too_large"
        )
    try:
        content = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ProjectInstructionsSnapshot(
            PROJECT_INSTRUCTIONS_PATH, True, False, reason="invalid_utf8"
        )
    if not content:
        return ProjectInstructionsSnapshot(
            PROJECT_INSTRUCTIONS_PATH, True, False, reason="empty"
        )
    return ProjectInstructionsSnapshot(
        PROJECT_INSTRUCTIONS_PATH, True, True, content,
        canonical_hash(content), "trusted_and_valid",
    )
