from __future__ import annotations

import os
from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
)


class PosixLocalIdentity:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.posix-local-identity", adapter_version="0.1.0",
        port_name="LocalIdentityPort", port_version="1.0",
        capabilities=frozenset({"uid-subject"}),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        if os.name == "nt":
            raise RuntimeError("POSIX local identity cannot start on Windows")
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "POSIX UID identity ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    def current_subject(self) -> str:
        if not self._started:
            raise RuntimeError("POSIX local identity is not started")
        return f"uid:{os.getuid()}"
