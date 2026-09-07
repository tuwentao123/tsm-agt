"""Port for explicitly requested, redacted Flow diagnostic artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .adapter import RuntimeAdapter


@dataclass(frozen=True, slots=True)
class FlowArtifactExportResult:
    relative_path: str
    size_bytes: int
    sha256: str


class FlowArtifactExportPort(RuntimeAdapter, Protocol):
    def export_json(
        self, workspace: Path, relative_path: str, document: Mapping[str, Any]
    ) -> FlowArtifactExportResult: ...

    def export_jsonl(
        self, workspace: Path, relative_path: str, document: Mapping[str, Any]
    ) -> FlowArtifactExportResult: ...

    def export_html(
        self, workspace: Path, relative_path: str, document: Mapping[str, Any]
    ) -> FlowArtifactExportResult: ...
