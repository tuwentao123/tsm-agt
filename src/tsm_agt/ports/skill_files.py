"""Structural contracts for Skill instruction files and preparation receipts."""
from __future__ import annotations

from typing import Any, Protocol
from pathlib import PurePosixPath


class SkillResourceError(ValueError):
    """A file provider's explicit failure contract; never parse error prose."""

    def __init__(self, code: str, message: str, hint: str) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


def validate_skill_resource(resource: str) -> None:
    """Shared lexical boundary for shipped and user-owned instruction files."""
    if not isinstance(resource, str) or not resource.strip():
        raise SkillResourceError(
            "INVALID_PARAM", "resource must be a non-empty string.",
            'Omit resource to read the main instructions, or pass "SKILL.md". '
            'For a linked file, use its relative path, e.g. "references/workflow.md".',
        )
    relative = PurePosixPath(resource)
    if (relative.is_absolute() or ".." in relative.parts or "\\" in resource
            or any(part.startswith(".") for part in relative.parts)
            or any(ord(char) < 32 or ord(char) == 127 for char in resource)):
        raise SkillResourceError(
            "SKILL_RESOURCE_FORBIDDEN",
            "resource must stay inside the Skill directory and contain no hidden, parent, or control-character components.",
            'Use "SKILL.md" or an allowed relative file path within this Skill; do not bypass the directory boundary.',
        )


class SkillInstallationReceiptsPort(Protocol):
    def list(self) -> list[dict]: ...
    def get(self, identity: str) -> dict: ...
    def update(self, identity: str, *, expected_status: str | None = None, **changes) -> dict: ...


class SkillFilesPort(Protocol):
    issues: list[str]
    installations: SkillInstallationReceiptsPort

    def refresh(self, registry: Any) -> Any: ...
    def read(self, skill: Any, resource: str = "SKILL.md") -> dict: ...
    def discover(self) -> tuple: ...
    def settings(self) -> dict: ...


class SkillCatalogPort(Protocol):
    """Merged instruction discovery supplied by the composition root."""

    issues: list[str]

    def discover(self) -> tuple: ...
    def refresh(self, registry: Any) -> tuple: ...
    def read(self, skill: Any, resource: str = "SKILL.md") -> dict: ...
    def model_data(self, skills: tuple) -> dict: ...
