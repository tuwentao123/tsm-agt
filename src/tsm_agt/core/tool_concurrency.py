"""A turn-local execution gate: serialize bookkeeping, overlap provider I/O.

Workers hold ``lock`` while running the existing authorization and checkpoint
path. Only the provider invocation releases it. The ContextVar keeps this
coordination out of the public ToolProviderPort and direct Kernel API.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from tsm_agt.ports import (
    ToolCall, ToolEffect, ToolIdempotency, ToolInvocationContext,
    ToolProtocol, ToolProviderPort, ToolResult, ToolResultAuthority, ToolSpec,
)


def supports_parallel_execution(spec: ToolSpec) -> bool:
    """Only explicitly safe, repeatable observations enter the first phase."""
    return (
        spec.is_read_only and spec.is_concurrency_safe
        and not spec.is_internal_state
        and spec.protocol is ToolProtocol.IMMEDIATE
        and spec.idempotency is not ToolIdempotency.NON_IDEMPOTENT
        and spec.effect in {ToolEffect.OBSERVE, ToolEffect.UNSPECIFIED}
        and spec.result_authority not in {
            ToolResultAuthority.PROCESS_FACT, ToolResultAuthority.RUNTIME_FACT,
            ToolResultAuthority.USER_INTENT,
        }
    )


_execution_gate: ContextVar[ToolExecutionGate | None] = ContextVar(
    "tool_execution_gate", default=None,
)


@dataclass
class ToolExecutionGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    running: set[str] = field(default_factory=set)
    stopped: bool = False
    settled: asyncio.Event = field(default_factory=asyncio.Event)

    @contextmanager
    def activate(self):
        token = _execution_gate.set(self)
        try:
            yield
        finally:
            _execution_gate.reset(token)

    async def _reacquire(self):
        acquisition = asyncio.create_task(self.lock.acquire())
        cancelled = None
        while not acquisition.done():
            try:
                await asyncio.shield(acquisition)
            except asyncio.CancelledError as error:
                cancelled = error
        acquisition.result()
        if cancelled is not None:
            raise cancelled

    async def invoke(self, provider, spec, call, context, timeout_seconds):
        # The authoritative spec may differ from the turn's advertised spec
        # (or an exploration policy may have rewritten the call). Recheck it
        # here; never overlap a newly unsafe invocation with running siblings.
        if not supports_parallel_execution(spec):
            self.stopped = True
            while self.running:
                self.settled.clear()
                self.lock.release()
                try:
                    await self.settled.wait()
                finally:
                    await self._reacquire()
            async with asyncio.timeout(timeout_seconds):
                return await provider.invoke(call, context)
        self.running.add(call.call_id)
        self.lock.release()
        try:
            async with asyncio.timeout(timeout_seconds):
                return await provider.invoke(call, context)
        finally:
            # Always reacquire before the worker's async-with releases its
            # lease, even if cancellation is requested again during cleanup.
            try:
                await self._reacquire()
            finally:
                self.running.discard(call.call_id)
                self.settled.set()


async def invoke_tool_provider(
    provider: ToolProviderPort, spec: ToolSpec, call: ToolCall,
    context: ToolInvocationContext, timeout_seconds: float,
) -> ToolResult:
    gate = _execution_gate.get()
    if gate is not None:
        return await gate.invoke(provider, spec, call, context, timeout_seconds)
    async with asyncio.timeout(timeout_seconds):
        return await provider.invoke(call, context)
