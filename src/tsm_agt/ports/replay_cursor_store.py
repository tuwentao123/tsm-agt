"""Port for a resumable, payload-free Flow Replay cursor."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .adapter import RuntimeAdapter


@dataclass(frozen=True, slots=True)
class ReplayCursor:
    task_id: str
    sequence: int


class ReplayCursorStorePort(RuntimeAdapter, Protocol):
    async def save(
        self, workspace: Path, relative_path: str, cursor: ReplayCursor
    ) -> None: ...

    def load(self, workspace: Path, relative_path: str) -> ReplayCursor: ...
