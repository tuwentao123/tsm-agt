"""Provider-neutral semantic code navigation boundary."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .adapter import RuntimeAdapter


@runtime_checkable
class CodeIntelligencePort(RuntimeAdapter, Protocol):
    """Read-only code queries; concrete indexing technology stays replaceable."""

    async def symbol_overview(
        self, workspace: Path, path: str, *, limit: int = 200,
    ) -> Mapping[str, Any]: ...

    async def definition(
        self, workspace: Path, symbol: str, *, path: str | None = None,
        limit: int = 50,
    ) -> Mapping[str, Any]: ...

    async def references(
        self, workspace: Path, symbol: str, *, path: str | None = None,
        include_declaration: bool = True, limit: int = 200,
    ) -> Mapping[str, Any]: ...

    async def implementations(
        self, workspace: Path, symbol: str, *, limit: int = 100,
    ) -> Mapping[str, Any]: ...

    async def workspace_symbols(
        self, workspace: Path, query: str, *, limit: int = 200,
    ) -> Mapping[str, Any]: ...

    async def diagnostics(
        self, workspace: Path, *, path: str | None = None, limit: int = 200,
    ) -> Mapping[str, Any]: ...

    async def rename_preview(
        self, workspace: Path, symbol: str, new_name: str, *,
        path: str | None = None, limit: int = 200,
    ) -> Mapping[str, Any]: ...
