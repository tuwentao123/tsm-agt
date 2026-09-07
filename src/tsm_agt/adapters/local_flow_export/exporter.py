"""Cross-platform, no-overwrite writer for redacted Flow JSON artifacts."""

from __future__ import annotations

import hashlib
import html
import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, FlowArtifactExportResult, HealthState,
    HealthStatus, WorkspaceFilesystemPort, WorkspacePathPort,
)


class LocalFlowArtifactExporter:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.local-flow-artifact-exporter",
        adapter_version="0.1.0",
        port_name="FlowArtifactExportPort",
        port_version="1.0",
        capabilities=frozenset({
            "json", "jsonl", "html", "workspace-contained", "no-overwrite",
            "private-file",
        }),
    )

    _SENSITIVE_NAMES = frozenset({
        ".agent", ".git", ".ssh", ".npmrc", ".pypirc",
        "credentials", "credentials.json", "id_rsa", "id_ed25519",
        "secrets",
    })
    _SENSITIVE_SUFFIXES = frozenset({
        ".jks", ".key", ".keystore", ".pem", ".p12", ".pfx",
    })

    def __init__(
        self, path_service: WorkspacePathPort,
        filesystem: WorkspaceFilesystemPort,
    ) -> None:
        self._path_service = path_service
        self._filesystem = filesystem
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "Flow artifact exporter ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    def export_json(
        self, workspace: Path, relative_path: str, document: Mapping[str, Any]
    ) -> FlowArtifactExportResult:
        encoded = (
            json.dumps(
                dict(document), ensure_ascii=False, indent=2, sort_keys=True
            ) + "\n"
        ).encode("utf-8")
        return self._write(workspace, relative_path, ".json", encoded)

    def export_jsonl(
        self, workspace: Path, relative_path: str, document: Mapping[str, Any]
    ) -> FlowArtifactExportResult:
        records: list[dict[str, Any]] = [{
            "record_type": "metadata",
            "artifact_type": document.get("artifact_type"),
            "schema_version": document.get("schema_version"),
            "task_id": document.get("task_id"),
            "cursor": document.get("cursor"),
            "redaction": document.get("redaction"),
        }]
        flow = document.get("flow")
        if isinstance(flow, Mapping):
            records.extend(
                {"record_type": "node", "data": node}
                for node in flow.get("nodes", [])
            )
            records.extend(
                {"record_type": "edge", "data": edge}
                for edge in flow.get("edges", [])
            )
        timeline = document.get("timeline")
        if isinstance(timeline, Mapping):
            records.extend(
                {"record_type": "timeline", "data": item}
                for item in timeline.get("items", [])
            )
        encoded = ("\n".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True)
            for record in records
        ) + "\n").encode("utf-8")
        return self._write(workspace, relative_path, ".jsonl", encoded)

    def export_html(
        self, workspace: Path, relative_path: str, document: Mapping[str, Any]
    ) -> FlowArtifactExportResult:
        serialized = json.dumps(dict(document), ensure_ascii=False, sort_keys=True)
        safe_json = (
            serialized.replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )
        timeline = document.get("timeline")
        items = timeline.get("items", []) if isinstance(timeline, Mapping) else []
        rows = "".join(
            "<tr>" + "".join(
                f"<td>{html.escape(str(value))}</td>"
                for value in (
                    item.get("event_seq_start", ""), item.get("lane", ""),
                    item.get("kind", ""), item.get("label", ""),
                    item.get("status", ""), item.get("duration_ms", ""),
                )
            ) + "</tr>"
            for item in items if isinstance(item, Mapping)
        )
        title = html.escape(f"tsm-agt flow {document.get('task_id', '')}")
        body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><style>
body{{font:14px system-ui,sans-serif;margin:2rem;color:#202124}}table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ddd;padding:.5rem;text-align:left}}th{{background:#f4f6f8}}tr:hover{{background:#fafafa}}
.note{{color:#5f6368}}
</style></head><body><h1>{title}</h1>
<p class="note">Offline, payload-free diagnostic report. Event payloads, prompts, tool arguments, environment variables, logs, and hidden reasoning are excluded.</p>
<table><thead><tr><th>Event</th><th>Lane</th><th>Kind</th><th>Label</th><th>Status</th><th>Duration ms</th></tr></thead><tbody>{rows}</tbody></table>
<script type="application/json" id="flow-data">{safe_json}</script>
</body></html>"""
        return self._write(workspace, relative_path, ".html", body.encode("utf-8"))

    def _write(
        self, workspace: Path, relative_path: str, suffix: str, encoded: bytes,
    ) -> FlowArtifactExportResult:
        if not self._started:
            raise RuntimeError("Flow artifact exporter is not started")
        if not relative_path.strip():
            raise ValueError("Flow export path must not be empty")
        resolved = self._path_service.resolve_mutation_path(
            workspace, relative_path
        )
        relative = Path(resolved.relative_path)
        if relative.suffix.lower() != suffix:
            raise ValueError(f"Flow export path must use the {suffix} suffix")
        if self._is_sensitive(relative):
            raise PermissionError("Flow artifacts cannot target sensitive paths")
        target = resolved.path
        try:
            descriptor = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self._filesystem.protect_private_path(target)
            self._filesystem.sync_directory(target.parent)
        except FileExistsError as error:
            raise FileExistsError(
                f"Flow export target already exists: {resolved.relative_path}"
            ) from error
        except Exception:
            if target.exists() and target.is_file():
                try:
                    self._filesystem.unlink(target)
                    self._filesystem.sync_directory(target.parent)
                except OSError:
                    pass
            raise
        return FlowArtifactExportResult(
            relative_path=resolved.relative_path,
            size_bytes=len(encoded),
            sha256=hashlib.sha256(encoded).hexdigest(),
        )

    @classmethod
    def _is_sensitive(cls, relative: Path) -> bool:
        for part in relative.parts:
            lowered = part.lower()
            if lowered in cls._SENSITIVE_NAMES:
                return True
            if lowered == ".env" or lowered.startswith(".env."):
                return True
            if Path(lowered).suffix in cls._SENSITIVE_SUFFIXES:
                return True
        return False
