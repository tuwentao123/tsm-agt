"""Structural contracts for Skill instruction files and preparation receipts."""
from __future__ import annotations

from typing import Any, Protocol


class SkillInstallationReceiptsPort(Protocol):
    def list(self) -> list[dict]: ...
    def get(self, identity: str) -> dict: ...
    def update(self, identity: str, *, expected_status: str | None = None, **changes) -> dict: ...


class SkillFilesPort(Protocol):
    issues: list[str]
    installations: SkillInstallationReceiptsPort

    def refresh(self, registry: Any) -> Any: ...
    def read(self, skill: Any, resource: str = "SKILL.md") -> dict: ...
