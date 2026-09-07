from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    HealthState,
    HealthStatus,
    AdapterContext,
    AdapterDescriptor,
    SandboxDecision,
    SandboxRequest,
)


class DenyAllSandbox:
    descriptor = AdapterDescriptor(
        adapter_id="fixture.deny-all-sandbox",
        adapter_version="0.1.0",
        port_name="SandboxPort",
        port_version="1.0",
        capabilities=frozenset({"deny-all"}),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        state = HealthState.HEALTHY if self._started else HealthState.UNHEALTHY
        return HealthStatus(state, "deny-all sandbox ready" if self._started else "not started")

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def authorize(self, request: SandboxRequest) -> SandboxDecision:
        return SandboxDecision(False, "fixture sandbox denies process execution")
