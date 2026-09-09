"""Task-scoped grants for reading explicitly approved external directories."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class WorkspaceAccessCapability(StrEnum):
    """Capabilities deliberately kept narrower than filesystem permissions."""

    READ = "READ"


@dataclass(frozen=True, slots=True)
class WorkspaceAccessGrant:
    """One canonical directory that read-only tools may use for this Task."""

    grant_id: str
    canonical_root: str
    capability: WorkspaceAccessCapability
    approval_request_id: str
    granted_at: datetime
    scope: str = "TASK"

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> WorkspaceAccessGrant:
        return cls(
            grant_id=str(data["grant_id"]),
            canonical_root=str(data["canonical_root"]),
            capability=WorkspaceAccessCapability(str(data["capability"])),
            approval_request_id=str(data["approval_request_id"]),
            granted_at=datetime.fromisoformat(str(data["granted_at"])),
            scope=str(data.get("scope", "TASK")),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "canonical_root": self.canonical_root,
            "capability": self.capability.value,
            "scope": self.scope,
            "approval_request_id": self.approval_request_id,
            "granted_at": self.granted_at.isoformat(),
        }
