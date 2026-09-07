"""Workspace-contained atomic storage for payload-free replay cursors."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, CrossProcessLockPort, HealthState,
    HealthStatus, ReplayCursor, WorkspaceFilesystemPort, WorkspacePathPort,
)

class LocalReplayCursorStore:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.local-replay-cursor-store", adapter_version="0.1.0",
        port_name="ReplayCursorStorePort", port_version="1.0",
        capabilities=frozenset({
            "atomic-replace", "workspace-contained", "private-file",
        }),
    )
    _SENSITIVE_NAMES = frozenset({
        ".agent", ".git", ".ssh", ".npmrc", ".pypirc", "credentials",
        "credentials.json", "id_rsa", "id_ed25519", "secrets",
    })
    _SENSITIVE_SUFFIXES = frozenset({
        ".jks", ".key", ".keystore", ".pem", ".p12", ".pfx",
    })

    def __init__(
        self, path_service: WorkspacePathPort,
        filesystem: WorkspaceFilesystemPort,
        cross_process_lock: CrossProcessLockPort,
    ) -> None:
        self._path_service = path_service
        self._filesystem = filesystem
        self._cross_process_lock = cross_process_lock
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "Replay cursor store ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def save(
        self, workspace: Path, relative_path: str, cursor: ReplayCursor,
    ) -> None:
        resolved = self._resolve(workspace, relative_path, mutation=True)
        lock_file = resolved.path.with_name(f".{resolved.path.name}.lock")
        lease_id = await self._cross_process_lock.acquire(lock_file)
        try:
            self._filesystem.protect_private_path(lock_file)
            if resolved.path.exists():
                existing = self._read(resolved.path)
                if existing.task_id != cursor.task_id:
                    raise ValueError(
                        "Replay cursor target belongs to a different task"
                    )
                if cursor.sequence < existing.sequence:
                    raise ValueError("Replay cursor sequence cannot move backwards")
            encoded = (json.dumps({
                "artifact_type": "tsm-agt.replay-cursor", "schema_version": 1,
                "task_id": cursor.task_id, "sequence": cursor.sequence,
            }, sort_keys=True) + "\n").encode("utf-8")
            temporary = resolved.path.with_name(
                f".{resolved.path.name}.{uuid4().hex}.tmp"
            )
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._filesystem.protect_private_path(temporary)
                self._filesystem.replace(temporary, resolved.path)
                self._filesystem.protect_private_path(resolved.path)
                self._filesystem.sync_directory(resolved.path.parent)
            except Exception:
                if temporary.exists():
                    try:
                        self._filesystem.unlink(temporary)
                    except OSError:
                        pass
                raise
        finally:
            await self._cross_process_lock.release(lease_id)

    def load(self, workspace: Path, relative_path: str) -> ReplayCursor:
        resolved = self._resolve(workspace, relative_path, mutation=False)
        return self._read(resolved.path)

    @staticmethod
    def _read(path: Path) -> ReplayCursor:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Replay cursor cannot be read") from error
        if (
            data.get("artifact_type") != "tsm-agt.replay-cursor"
            or data.get("schema_version") != 1
        ):
            raise ValueError("Replay cursor has an unsupported format")
        task_id, sequence = data.get("task_id"), data.get("sequence")
        if (
            not isinstance(task_id, str) or not task_id
            or not isinstance(sequence, int) or sequence < 0
        ):
            raise ValueError("Replay cursor contains invalid values")
        return ReplayCursor(task_id, sequence)

    def _resolve(self, workspace: Path, relative_path: str, *, mutation: bool):
        if not self._started:
            raise RuntimeError("Replay cursor store is not started")
        resolver = (
            self._path_service.resolve_mutation_path
            if mutation else self._path_service.resolve_access_path
        )
        resolved = resolver(workspace, relative_path)
        relative = Path(resolved.relative_path)
        if relative.suffix.lower() != ".json":
            raise ValueError("Replay cursor path must use the .json suffix")
        if self._is_sensitive(relative):
            raise PermissionError("Replay cursor cannot target sensitive paths")
        if not mutation and (
            not resolved.path.exists() or not resolved.path.is_file()
        ):
            raise FileNotFoundError(
                f"Replay cursor does not exist: {resolved.relative_path}"
            )
        return resolved

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
