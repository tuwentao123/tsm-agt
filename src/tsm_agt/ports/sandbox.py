"""Sandbox policy boundary used before process execution exists."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
from dataclasses import field
from typing import Protocol

from .adapter import RuntimeAdapter


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SandboxDecision:
    allowed: bool
    reason: str


class SandboxPort(RuntimeAdapter, Protocol):
    async def authorize(self, request: SandboxRequest) -> SandboxDecision: ...
