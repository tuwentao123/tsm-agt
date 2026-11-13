"""Langfuse export seam.

The Web layer renders and tags turns, but it must not import the Langfuse
adapter: only the composition root may construct concrete adapters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


class LangfuseExportError(RuntimeError):
    """Langfuse configuration or export could not proceed."""


class LangfuseEndpoint(Protocol):
    scheme: str
    hostname: str | None


class LangfuseClientPort(Protocol):
    endpoint: LangfuseEndpoint

    def send_trace(self, payload: Mapping[str, Any], *, description: str = "trace") -> None: ...


class LangfuseExporter(Protocol):
    """Everything the Web layer needs to build and ship one trace."""

    def config_path(self) -> Path: ...

    def client_from_config(self, path: Path | None = None) -> LangfuseClientPort: ...

    def attributes(self, values: Mapping[str, Any]) -> list[dict[str, Any]]: ...

    def trace_payload(
        self, service_name: str, spans: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]: ...
