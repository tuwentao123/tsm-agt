"""Chat-first dispatcher for deciding when runtime routing is necessary."""

from __future__ import annotations

from dataclasses import dataclass

from .kernel import Kernel
from .runtime_input import RuntimeInputIntent, RuntimeInputRoute


@dataclass(frozen=True)
class ChatDispatchResult:
    """Represents the top-level handling decision for user input."""

    handled_by_runtime: bool
    route: RuntimeInputRoute | None = None


class ChatDispatcher:
    """Keep runtime routing behind a narrow explicit entry point.

    Ordinary chat should remain the default interaction mode. Runtime routing
    is only entered for explicit steering, approval continuation, or resumable
    task control flows.
    """

    def __init__(
        self,
        kernel: Kernel,
        *,
        allow_implicit_task_resume: bool = False,
    ) -> None:
        self._kernel = kernel
        self._allow_implicit_task_resume = allow_implicit_task_resume

    async def dispatch_runtime_input(
        self,
        task_id: str,
        text: str,
        input_id: str,
        *,
        explicit_intent: RuntimeInputIntent | None = None,
        fallback_intent: RuntimeInputIntent | None = None,
    ) -> ChatDispatchResult:
        if not self._allow_implicit_task_resume:
            fallback_intent = None

        route = await self._kernel.route_runtime_input(
            task_id,
            text,
            input_id,
            explicit_intent=explicit_intent,
            fallback_intent=fallback_intent,
        )
        return ChatDispatchResult(True, route)
