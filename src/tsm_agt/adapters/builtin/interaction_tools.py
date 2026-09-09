"""Model-visible interaction schema; execution remains in the Kernel."""

from __future__ import annotations

from datetime import datetime

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus, ToolCall,
    ToolIdempotency, ToolInvocationContext, ToolResult, ToolRisk, ToolSpec,
    ToolEffect, ToolProtocol, ToolResultAuthority,
)


class CoreInteractionToolProvider:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.core-interaction", adapter_version="0.1.0",
        port_name="ToolProviderPort", port_version="1.0",
        capabilities=frozenset({"core.request_input"}),
    )
    _spec = ToolSpec(
        name="core.request_input",
        description=(
            "Pause the current task and ask the user one short question only when "
            "missing information would materially change the result, permission, "
            "or an irreversible action. Prefer a visible assumption otherwise."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "choices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string"},
                            "label": {"type": "string"},
                        },
                        "required": ["value", "label"],
                        "additionalProperties": False,
                    },
                    "maxItems": 3,
                },
                "reason": {"type": "string"},
                "required": {"type": "boolean"},
            },
            "required": ["question", "reason"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R0, is_read_only=True, is_concurrency_safe=False,
        idempotency=ToolIdempotency.KEYED, max_result_tokens=1000,
        data_transmission="the answer is returned only to the active model turn",
        rollback="no external side effect; the pending question can be left unanswered",
        effect=ToolEffect.INTERACT,
        result_authority=ToolResultAuthority.USER_INTENT,
        protocol=ToolProtocol.WAIT_USER,
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "interaction schema ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return (self._spec,)

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext,
    ) -> ToolResult:
        raise RuntimeError(
            "core.request_input must be intercepted by the Kernel interaction protocol"
        )
