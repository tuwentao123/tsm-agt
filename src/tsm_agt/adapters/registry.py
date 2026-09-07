"""Registry for Port implementations and their lifecycle."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, TypeVar

from tsm_agt.ports import AdapterContext, RuntimeAdapter

PortT = TypeVar("PortT")


class DuplicateAdapterError(ValueError):
    pass


class MissingAdapterError(LookupError):
    pass


class AdapterRegistry:
    """Registers implementations by Port and coordinates start/stop."""

    def __init__(self) -> None:
        self._adapters: dict[type[Any], list[RuntimeAdapter]] = defaultdict(list)
        self._ids: set[str] = set()

    def register(self, port: type[PortT], adapter: PortT) -> None:
        descriptor = getattr(adapter, "descriptor", None)
        if descriptor is None:
            raise TypeError("adapter must expose an AdapterDescriptor")
        if descriptor.adapter_id in self._ids:
            raise DuplicateAdapterError(f"duplicate adapter id: {descriptor.adapter_id}")
        if descriptor.port_name != port.__name__:
            raise TypeError(
                f"adapter {descriptor.adapter_id} declares {descriptor.port_name}, "
                f"registered as {port.__name__}"
            )
        self._ids.add(descriptor.adapter_id)
        self._adapters[port].append(adapter)  # type: ignore[arg-type]

    def require(self, port: type[PortT]) -> PortT:
        candidates = self._adapters.get(port, [])
        if len(candidates) != 1:
            raise MissingAdapterError(
                f"required singleton {port.__name__} has {len(candidates)} implementations"
            )
        return candidates[0]  # type: ignore[return-value]

    def all(self, port: type[PortT]) -> tuple[PortT, ...]:
        return tuple(self._adapters.get(port, ()))  # type: ignore[return-value]

    def descriptors(self) -> tuple[Any, ...]:
        return tuple(
            adapter.descriptor
            for adapters in self._adapters.values()
            for adapter in adapters
        )

    def runtime_adapters(self) -> tuple[RuntimeAdapter, ...]:
        """Return registered instances for composition-time dependency wiring."""
        return tuple(
            adapter
            for adapters in self._adapters.values()
            for adapter in adapters
        )

    async def start_all(
        self, configs: Mapping[str, Mapping[str, Any]] | None = None
    ) -> None:
        adapter_configs = configs or {}
        for adapters in self._adapters.values():
            for adapter in adapters:
                context = AdapterContext(
                    config=adapter_configs.get(adapter.descriptor.adapter_id, {}),
                    emit_event=lambda _event_type, _payload: None,
                )
                await adapter.start(context)

    async def stop_all(self) -> None:
        deadline = datetime.now() + timedelta(seconds=5)
        for adapters in reversed(tuple(self._adapters.values())):
            for adapter in reversed(adapters):
                await adapter.stop(deadline)
