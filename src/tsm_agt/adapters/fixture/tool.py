from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    ToolCall,
    ToolIdempotency,
    ToolInvocationContext,
    ToolResult,
    ToolRisk,
    ToolSpec,
)


class EchoToolProvider:
    """Deterministic, side-effect-free tool provider for tests and demos."""

    descriptor = AdapterDescriptor(
        adapter_id="fixture.echo-tool-provider",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({"fixture.echo"}),
    )
    _spec = ToolSpec(
        name="fixture.echo",
        description="Return the supplied text unchanged for deterministic tests.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R0,
        is_read_only=True,
        is_concurrency_safe=True,
        idempotency=ToolIdempotency.IDEMPOTENT,
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        state = HealthState.HEALTHY if self._started else HealthState.UNHEALTHY
        message = "echo tool ready" if self._started else "not started"
        return HealthStatus(state, message)

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return (self._spec,)

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        if not self._started:
            raise RuntimeError("adapter is not started")
        if call.name != self._spec.name:
            return ToolResult(
                call_id=call.call_id,
                ok=False,
                error_code="NOT_FOUND",
                message=f"unknown fixture tool: {call.name}",
            )
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={"text": call.arguments["text"]},
            meta={"invocation_id": context.invocation_id},
        )
