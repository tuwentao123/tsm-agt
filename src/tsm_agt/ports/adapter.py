"""Lifecycle shared by concrete runtime adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class HealthStatus:
    state: HealthState
    message: str = ""
    checked_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True, slots=True)
class AdapterDescriptor:
    """Identity and compatibility metadata for one Port implementation."""

    adapter_id: str
    adapter_version: str
    port_name: str
    port_version: str
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class AdapterContext:
    """Least-authority services granted while starting an adapter."""

    config: Mapping[str, Any]
    emit_event: Callable[[str, Mapping[str, Any]], None]


class RuntimeAdapter(Protocol):
    """Lifecycle required from every concrete Port implementation."""

    descriptor: AdapterDescriptor

    async def start(self, context: AdapterContext) -> None: ...

    async def health(self) -> HealthStatus: ...

    async def stop(self, deadline: datetime) -> None: ...

