"""Workspace baseline manifests and read-only change detection."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from enum import StrEnum

from tsm_agt.ports import WorkspaceFilesystemPort, WorkspacePathPort


_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git", ".agent", ".gradle", ".idea", ".venv", ".vscode",
        "__pycache__", "node_modules", "target",
    }
)
_SENSITIVE_NAMES = frozenset(
    {
        ".npmrc", ".pypirc", "credentials", "credentials.json",
        "id_rsa", "id_ed25519",
    }
)
_SENSITIVE_SUFFIXES = frozenset(
    {".jks", ".key", ".keystore", ".p12", ".pem", ".pfx"}
)
_MAX_FILE_BYTES = 10_000_000
_MAX_FILES = 20_000


@dataclass(frozen=True, slots=True)
class WorkspaceFileSnapshot:
    path: str
    sha256: str
    size: int
    mtime_ns: int

    def to_data(self) -> dict[str, Any]:
        return {
            "path": self.path, "sha256": self.sha256, "size": self.size,
            "mtime_ns": self.mtime_ns,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> WorkspaceFileSnapshot:
        return cls(
            path=str(data["path"]), sha256=str(data["sha256"]),
            size=int(data["size"]), mtime_ns=int(data["mtime_ns"]),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceBaseline:
    workspace: str
    captured_at: datetime
    files: Mapping[str, WorkspaceFileSnapshot] = field(default_factory=dict)
    git_head: str | None = None
    skipped_files: int = 0
    truncated: bool = False

    def to_data(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "captured_at": self.captured_at.isoformat(),
            "files": {key: value.to_data() for key, value in self.files.items()},
            "git_head": self.git_head, "skipped_files": self.skipped_files,
            "truncated": self.truncated,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> WorkspaceBaseline:
        raw_files = data.get("files", {})
        if not isinstance(raw_files, Mapping):
            raise ValueError("workspace baseline files must be an object")
        return cls(
            workspace=str(data["workspace"]),
            captured_at=datetime.fromisoformat(str(data["captured_at"])),
            files={
                str(key): WorkspaceFileSnapshot.from_data(value)
                for key, value in raw_files.items() if isinstance(value, Mapping)
            },
            git_head=(str(data["git_head"]) if data.get("git_head") else None),
            skipped_files=int(data.get("skipped_files", 0)),
            truncated=bool(data.get("truncated", False)),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceChangeSet:
    added: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]
    git_head_changed: bool
    current_git_head: str | None
    scan_truncated: bool
    skipped_files: int

    @property
    def changed(self) -> bool:
        return bool(self.added or self.modified or self.deleted or self.git_head_changed)


class MutationOperation(StrEnum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class WorkspaceMutationConflict(RuntimeError):
    def __init__(
        self, path: str, expected_hash: str | None, actual_hash: str | None
    ) -> None:
        super().__init__(
            f"workspace file changed before write: {path}; expected "
            f"{expected_hash or '<missing>'}, got {actual_hash or '<missing>'}"
        )
        self.path = path
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash


@dataclass(frozen=True, slots=True)
class MutationRecord:
    mutation_id: str
    path: str
    operation: MutationOperation
    before_hash: str | None
    after_hash: str | None
    step_id: str
    backup_ref: str | None
    created_at: datetime
    before_mode: int | None = None
    reverts_mutation_id: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation_id, "path": self.path,
            "operation": self.operation.value,
            "before_hash": self.before_hash, "after_hash": self.after_hash,
            "step_id": self.step_id, "backup_ref": self.backup_ref,
            "created_at": self.created_at.isoformat(),
            "before_mode": self.before_mode,
            "reverts_mutation_id": self.reverts_mutation_id,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> MutationRecord:
        return cls(
            mutation_id=str(data["mutation_id"]), path=str(data["path"]),
            operation=MutationOperation(str(data["operation"])),
            before_hash=(str(data["before_hash"]) if data.get("before_hash") else None),
            after_hash=(str(data["after_hash"]) if data.get("after_hash") else None),
            step_id=str(data["step_id"]),
            backup_ref=(str(data["backup_ref"]) if data.get("backup_ref") else None),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            before_mode=(
                int(data["before_mode"])
                if data.get("before_mode") is not None else None
            ),
            reverts_mutation_id=(
                str(data["reverts_mutation_id"])
                if data.get("reverts_mutation_id") else None
            ),
        )


@dataclass(frozen=True, slots=True)
class PreparedWorkspaceMutation:
    path: Path
    relative_path: str
    operation: MutationOperation
    before_hash: str | None
    before_bytes: bytes | None
    after_hash: str
    after_bytes: bytes
    previous_mode: int | None
    result_mode: int | None


@dataclass(frozen=True, slots=True)
class PreparedWorkspaceDeletion:
    path: Path
    relative_path: str
    before_hash: str
    before_bytes: bytes
    previous_mode: int


@dataclass(frozen=True, slots=True)
class WorkspaceTransactionEntry:
    mutation_id: str
    path: str
    before_hash: str | None
    after_hash: str | None
    backup_ref: str | None
    before_mode: int | None
    mutation_ids: tuple[str, ...] = ()

    @property
    def effective_mutation_ids(self) -> tuple[str, ...]:
        return self.mutation_ids or (self.mutation_id,)

    def to_data(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation_id, "path": self.path,
            "before_hash": self.before_hash, "after_hash": self.after_hash,
            "backup_ref": self.backup_ref, "before_mode": self.before_mode,
            "mutation_ids": list(self.effective_mutation_ids),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> WorkspaceTransactionEntry:
        legacy = {
            "mutation_id", "path", "before_hash", "after_hash",
            "backup_ref", "before_mode",
        }
        current = legacy | {"mutation_ids"}
        if set(data) not in {frozenset(legacy), frozenset(current)}:
            raise ValueError("workspace transaction entry has unexpected fields")
        before_hash = data["before_hash"]
        after_hash = data["after_hash"]
        backup_ref = data["backup_ref"]
        before_mode = data["before_mode"]
        raw_mutation_ids = data.get("mutation_ids")
        if raw_mutation_ids is not None and not isinstance(raw_mutation_ids, list):
            raise ValueError("workspace transaction mutation_ids must be an array")
        return cls(
            mutation_id=str(data["mutation_id"]), path=str(data["path"]),
            before_hash=(str(before_hash) if before_hash is not None else None),
            after_hash=(str(after_hash) if after_hash is not None else None),
            backup_ref=(str(backup_ref) if backup_ref is not None else None),
            before_mode=(int(before_mode) if before_mode is not None else None),
            mutation_ids=(
                tuple(str(item) for item in raw_mutation_ids)
                if isinstance(raw_mutation_ids, list) else ()
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceTransactionManifest:
    transaction_id: str
    task_id: str
    step_id: str
    kind: str
    created_at: datetime
    entries: tuple[WorkspaceTransactionEntry, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": 2, "transaction_id": self.transaction_id,
            "task_id": self.task_id, "step_id": self.step_id,
            "kind": self.kind, "created_at": self.created_at.isoformat(),
            "entries": [entry.to_data() for entry in self.entries],
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> WorkspaceTransactionManifest:
        expected = {
            "schema_version", "transaction_id", "task_id", "step_id",
            "kind", "created_at", "entries",
        }
        if set(data) != expected or data.get("schema_version") not in {1, 2}:
            raise ValueError("unsupported workspace transaction manifest")
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValueError("workspace transaction manifest entries are invalid")
        manifest = cls(
            transaction_id=str(data["transaction_id"]),
            task_id=str(data["task_id"]), step_id=str(data["step_id"]),
            kind=str(data["kind"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            entries=tuple(
                WorkspaceTransactionEntry.from_data(item)
                for item in raw_entries if isinstance(item, Mapping)
            ),
        )
        if (
            len(manifest.entries) != len(raw_entries)
            or len(manifest.entries) < 2
            or len(manifest.entries) > 50
            or manifest.kind not in {
                "apply_patches", "rollback_mutation_batch",
                "rollback_mutation_groups",
            }
            or not manifest.transaction_id.startswith("workspace-tx-")
            or not manifest.task_id.strip()
            or not manifest.step_id.strip()
        ):
            raise ValueError("workspace transaction manifest identity is invalid")
        mutation_ids = tuple(
            mutation_id for entry in manifest.entries
            for mutation_id in entry.effective_mutation_ids
        )
        paths = tuple(entry.path for entry in manifest.entries)
        if (
            not mutation_ids
            or len(mutation_ids) > 100
            or len(set(mutation_ids)) != len(mutation_ids)
            or len(set(paths)) != len(paths)
        ):
            raise ValueError("workspace transaction entries must be unique")
        for entry in manifest.entries:
            if (
                not entry.mutation_id.startswith("mutation-")
                or not entry.effective_mutation_ids
                or entry.mutation_id != entry.effective_mutation_ids[0]
                or any(
                    not item.startswith("mutation-")
                    for item in entry.effective_mutation_ids
                )
                or not entry.path
                or (
                    entry.after_hash is not None
                    and not _is_sha256(entry.after_hash)
                )
                or (entry.before_hash is not None and not _is_sha256(entry.before_hash))
                or (
                    entry.before_hash is None
                    and entry.after_hash is None
                    and manifest.kind != "rollback_mutation_groups"
                )
                or (entry.before_hash is None and entry.backup_ref is not None)
                or (entry.before_hash is not None and entry.backup_ref is None)
                or (
                    entry.before_mode is not None
                    and not 0 <= entry.before_mode <= 0o777
                )
            ):
                raise ValueError("workspace transaction entry values are invalid")
        return manifest


def prepare_workspace_text_write(
    workspace: Path, relative_path: str, content: str, expected_hash: str | None,
    path_service: WorkspacePathPort,
) -> PreparedWorkspaceMutation:
    if not isinstance(content, str):
        raise TypeError("workspace text content must be a string")
    return prepare_workspace_bytes_write(
        workspace, relative_path, content.encode("utf-8"), expected_hash,
        path_service,
    )


def prepare_workspace_bytes_write(
    workspace: Path, relative_path: str, content: bytes,
    expected_hash: str | None, path_service: WorkspacePathPort, *,
    result_mode: int | None = None,
) -> PreparedWorkspaceMutation:
    if not isinstance(content, bytes):
        raise TypeError("workspace content must be bytes")
    _root, path, normalized = _resolve_mutation_path(
        workspace, relative_path, path_service
    )
    if path.is_symlink():
        raise PermissionError("workspace mutation cannot replace a symlink")
    before_bytes: bytes | None
    previous_mode: int | None
    if path.exists():
        if not path.is_file():
            raise ValueError("workspace mutation target is not a regular file")
        before_bytes = path.read_bytes()
        actual_hash = _hash_bytes(before_bytes)
        previous_mode = path.stat().st_mode & 0o777
        operation = MutationOperation.MODIFY
    else:
        before_bytes = None
        actual_hash = None
        previous_mode = None
        operation = MutationOperation.CREATE
    if actual_hash != expected_hash:
        raise WorkspaceMutationConflict(normalized, expected_hash, actual_hash)
    after_bytes = content
    if len(after_bytes) > _MAX_FILE_BYTES:
        raise ValueError(f"workspace mutation exceeds {_MAX_FILE_BYTES} byte limit")
    return PreparedWorkspaceMutation(
        path, normalized, operation, actual_hash, before_bytes,
        _hash_bytes(after_bytes), after_bytes, previous_mode,
        previous_mode if result_mode is None else result_mode,
    )


def prepare_workspace_file_delete(
    workspace: Path, relative_path: str, expected_hash: str,
    path_service: WorkspacePathPort,
) -> PreparedWorkspaceDeletion:
    if not isinstance(expected_hash, str) or not expected_hash:
        raise ValueError("workspace deletion requires a non-empty expected hash")
    _root, path, normalized = _resolve_mutation_path(
        workspace, relative_path, path_service
    )
    if path.is_symlink():
        raise PermissionError("workspace mutation cannot delete a symlink")
    if not path.exists():
        raise FileNotFoundError(f"workspace file does not exist: {normalized}")
    if not path.is_file():
        raise ValueError("workspace deletion target is not a regular file")
    before_bytes = path.read_bytes()
    actual_hash = _hash_bytes(before_bytes)
    if actual_hash != expected_hash:
        raise WorkspaceMutationConflict(normalized, expected_hash, actual_hash)
    return PreparedWorkspaceDeletion(
        path, normalized, actual_hash, before_bytes, path.stat().st_mode & 0o777
    )


def commit_prepared_workspace_mutation(
    mutation: PreparedWorkspaceMutation, filesystem: WorkspaceFilesystemPort,
) -> None:
    _assert_precondition(mutation)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{mutation.path.name}.tsm-agt-", dir=mutation.path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(mutation.after_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        if mutation.result_mode is not None:
            os.chmod(temporary, mutation.result_mode)
        _assert_precondition(mutation)
        filesystem.replace(temporary, mutation.path)
        filesystem.sync_directory(mutation.path.parent)
    finally:
        _remove_temporary_file(temporary, filesystem)
    if file_sha256(mutation.path) != mutation.after_hash:
        raise OSError("workspace mutation verification failed after atomic replace")


def restore_prepared_workspace_mutation(
    mutation: PreparedWorkspaceMutation, filesystem: WorkspaceFilesystemPort,
) -> None:
    current_hash = file_sha256(mutation.path)
    if current_hash != mutation.after_hash:
        raise WorkspaceMutationConflict(
            mutation.relative_path, mutation.after_hash, current_hash
        )
    if mutation.before_bytes is None:
        filesystem.unlink(mutation.path)
        filesystem.sync_directory(mutation.path.parent)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{mutation.path.name}.tsm-agt-restore-", dir=mutation.path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(mutation.before_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        if mutation.previous_mode is not None:
            os.chmod(temporary, mutation.previous_mode)
        filesystem.replace(temporary, mutation.path)
        filesystem.sync_directory(mutation.path.parent)
    finally:
        _remove_temporary_file(temporary, filesystem)


def commit_prepared_workspace_deletion(
    deletion: PreparedWorkspaceDeletion, filesystem: WorkspaceFilesystemPort,
) -> None:
    actual_hash = file_sha256(deletion.path)
    if actual_hash != deletion.before_hash:
        raise WorkspaceMutationConflict(
            deletion.relative_path, deletion.before_hash, actual_hash
        )
    filesystem.unlink(deletion.path)
    filesystem.sync_directory(deletion.path.parent)
    if deletion.path.exists() or deletion.path.is_symlink():
        raise OSError("workspace deletion verification failed")


def restore_prepared_workspace_deletion(
    deletion: PreparedWorkspaceDeletion, filesystem: WorkspaceFilesystemPort,
) -> None:
    actual_hash = file_sha256(deletion.path)
    if actual_hash is not None or deletion.path.exists() or deletion.path.is_symlink():
        raise WorkspaceMutationConflict(
            deletion.relative_path, None, actual_hash
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{deletion.path.name}.tsm-agt-restore-",
        dir=deletion.path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(deletion.before_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, deletion.previous_mode)
        if deletion.path.exists() or deletion.path.is_symlink():
            raise WorkspaceMutationConflict(
                deletion.relative_path, None, file_sha256(deletion.path)
            )
        filesystem.replace(temporary, deletion.path)
        filesystem.sync_directory(deletion.path.parent)
    finally:
        _remove_temporary_file(temporary, filesystem)
    if file_sha256(deletion.path) != deletion.before_hash:
        raise OSError("workspace deletion restoration verification failed")


def write_mutation_backup(
    workspace: Path, task_storage_key: str, mutation_id: str, content: bytes,
    filesystem: WorkspaceFilesystemPort, path_service: WorkspacePathPort,
) -> str:
    root = path_service.normalize_workspace(workspace)
    backup_directory = _private_runtime_directory(
        root, (task_storage_key, "backups"), path_service, filesystem
    )
    backup = backup_directory / f"{mutation_id}.bak"
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    filesystem.protect_private_path(backup)
    filesystem.sync_directory(backup_directory)
    return backup.relative_to(root).as_posix()


def read_mutation_backup(
    workspace: Path, task_storage_key: str, backup_ref: str,
    expected_hash: str, path_service: WorkspacePathPort,
) -> bytes:
    root = path_service.normalize_workspace(workspace)
    expected_directory = _private_runtime_directory(
        root, (task_storage_key, "backups"), path_service, None, create=False
    )
    reference = Path(backup_ref)
    if reference.is_absolute() or ".." in reference.parts:
        raise PermissionError("mutation backup reference is not workspace-relative")
    backup = root / reference
    if path_service.is_link_like(backup) or not backup.is_file():
        raise FileNotFoundError("mutation backup is missing or not a regular file")
    if backup.parent != expected_directory:
        raise PermissionError("mutation backup reference is outside the task backup directory")
    content = backup.read_bytes()
    actual_hash = _hash_bytes(content)
    if actual_hash != expected_hash:
        raise WorkspaceMutationConflict(backup_ref, expected_hash, actual_hash)
    return content


def write_workspace_transaction_manifest(
    workspace: Path, task_storage_key: str, manifest: WorkspaceTransactionManifest,
    filesystem: WorkspaceFilesystemPort, path_service: WorkspacePathPort,
) -> Path:
    directory = _workspace_transaction_directory(
        workspace, task_storage_key, path_service, filesystem
    )
    path = directory / f"{manifest.transaction_id}.json"
    payload = json.dumps(
        manifest.to_data(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{manifest.transaction_id}.", suffix=".tmp", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"workspace transaction already exists: {path.name}")
        filesystem.replace(temporary, path)
        filesystem.protect_private_path(path)
        filesystem.sync_directory(directory)
    finally:
        _remove_temporary_file(temporary, filesystem)
    return path


def read_workspace_transaction_manifests(
    workspace: Path, task_storage_key: str, path_service: WorkspacePathPort,
) -> tuple[tuple[Path, WorkspaceTransactionManifest], ...]:
    directory = _workspace_transaction_directory(
        workspace, task_storage_key, path_service, None, create=False
    )
    if not directory.exists():
        return ()
    results: list[tuple[Path, WorkspaceTransactionManifest]] = []
    for path in sorted(directory.glob("workspace-tx-*.json")):
        if path_service.is_link_like(path) or not path.is_file():
            raise PermissionError(
                "workspace transaction manifest must be a regular file"
            )
        if path.stat().st_size > 1_000_000:
            raise ValueError("workspace transaction manifest is too large")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("workspace transaction manifest is invalid JSON") from error
        if not isinstance(raw, Mapping):
            raise ValueError("workspace transaction manifest must be an object")
        manifest = WorkspaceTransactionManifest.from_data(raw)
        if path.name != f"{manifest.transaction_id}.json":
            raise ValueError("workspace transaction manifest filename mismatch")
        results.append((path, manifest))
    return tuple(results)


def remove_workspace_transaction_manifest(
    path: Path, filesystem: WorkspaceFilesystemPort,
) -> None:
    if path.is_symlink() or not path.is_file():
        raise PermissionError("workspace transaction manifest is not a regular file")
    parent = path.parent
    filesystem.unlink(path)
    filesystem.sync_directory(parent)


def _remove_temporary_file(
    path: Path, filesystem: WorkspaceFilesystemPort,
) -> None:
    """Remove a leftover transaction temp through the platform boundary."""
    try:
        filesystem.unlink(path)
    except FileNotFoundError:
        # A successful atomic replace consumes the source path.
        return


def _workspace_transaction_directory(
    workspace: Path, task_storage_key: str, path_service: WorkspacePathPort,
    filesystem: WorkspaceFilesystemPort | None, *,
    create: bool = True,
) -> Path:
    if not task_storage_key or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
        for character in task_storage_key
    ):
        raise ValueError("invalid task storage key")
    return _private_runtime_directory(
        workspace, (task_storage_key, "transactions"), path_service, filesystem,
        create=create
    )


def _private_runtime_directory(
    workspace: Path, suffix: tuple[str, ...], path_service: WorkspacePathPort,
    filesystem: WorkspaceFilesystemPort | None, *,
    create: bool = True,
) -> Path:
    root = path_service.normalize_workspace(workspace)
    directory = root
    for name in (".agent", "runtime", *suffix):
        candidate = directory / name
        existed = candidate.exists() or path_service.is_link_like(candidate)
        if not create and not existed:
            return candidate
        if create:
            # Never apply permissions through an existing symlink/Junction:
            # doing so could change an object outside the workspace.
            if existed and (
                path_service.is_link_like(candidate) or not candidate.is_dir()
            ):
                raise PermissionError(
                    "workspace runtime path must contain only real directories"
                )
            try:
                candidate.mkdir(mode=0o700)
            except FileExistsError:
                pass
            # Re-check after mkdir to fail closed if another process raced us.
            if path_service.is_link_like(candidate) or not candidate.is_dir():
                raise PermissionError(
                    "workspace runtime path must contain only real directories"
                )
            if filesystem is None:
                raise RuntimeError(
                    "creating a private runtime directory requires filesystem protection"
                )
            filesystem.protect_private_path(candidate)
        if path_service.is_link_like(candidate) or not candidate.is_dir():
            raise PermissionError(
                "workspace runtime path must contain only real directories"
            )
        try:
            resolved = path_service.resolve_access_path(
                root, candidate.relative_to(root).as_posix()
            ).path
        except ValueError as error:
            raise PermissionError(
                "workspace runtime path escapes the workspace"
            ) from error
        # Windows paths are case-insensitive and may be returned with a
        # different drive-letter/component casing.  Compare through the
        # platform identity service instead of Path's textual spelling.
        if path_service.workspace_key(resolved) != path_service.workspace_key(candidate):
            raise PermissionError(
                "workspace runtime path must not contain aliases or links"
            )
        directory = candidate
    return directory


def file_sha256(path: Path) -> str | None:
    if not path.exists() or path.is_symlink() or not path.is_file():
        return None
    return _hash_bytes(path.read_bytes())


def workspace_mutation_lock_key(
    workspace: Path, relative_path: str, path_service: WorkspacePathPort,
) -> str:
    """Return one canonical lock key for a workspace mutation target."""

    _resolve_mutation_path(workspace, relative_path, path_service)
    resolved = path_service.resolve_mutation_path(workspace, relative_path)
    return resolved.canonical_key


def workspace_mutation_lock_file(
    workspace: Path, lock_key: str, path_service: WorkspacePathPort,
    filesystem: WorkspaceFilesystemPort,
) -> Path:
    """Create/validate the private runtime lock directory and return a lock file."""

    directory = _private_runtime_directory(
        workspace, ("locks",), path_service, filesystem
    )
    digest = hashlib.sha256(lock_key.encode("utf-8")).hexdigest()
    return directory / f"{digest}.lock"


def _resolve_mutation_path(
    workspace: Path, relative_path: str, path_service: WorkspacePathPort,
) -> tuple[Path, Path, str]:
    if not relative_path or "\x00" in relative_path:
        raise ValueError("workspace path must not be empty or contain NUL")
    candidate = Path(relative_path)
    lexical = Path(os.path.normpath(relative_path))
    if (
        _is_sensitive(candidate) or _is_sensitive(lexical)
        or (lexical.parts and lexical.parts[0] in {".git", ".agent"})
    ):
        raise PermissionError("sensitive or runtime paths cannot be mutated")
    resolved = path_service.resolve_mutation_path(workspace, relative_path)
    normalized = Path(resolved.relative_path)
    if (
        _is_sensitive(normalized)
        or (normalized.parts and normalized.parts[0] in {".git", ".agent"})
    ):
        raise PermissionError("sensitive or runtime paths cannot be mutated")
    return resolved.workspace, resolved.path, resolved.relative_path


def _assert_precondition(mutation: PreparedWorkspaceMutation) -> None:
    actual_hash = file_sha256(mutation.path)
    if actual_hash != mutation.before_hash:
        raise WorkspaceMutationConflict(
            mutation.relative_path, mutation.before_hash, actual_hash
        )


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_sha256(value: str) -> bool:
    return (
        len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def capture_workspace_baseline(
    workspace: Path, path_service: WorkspacePathPort,
) -> WorkspaceBaseline:
    root = path_service.normalize_workspace(workspace)
    files: dict[str, WorkspaceFileSnapshot] = {}
    skipped = 0
    truncated = False
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = sorted(
            name for name in directory_names
            if name not in _EXCLUDED_DIRECTORIES
            and not path_service.is_link_like(Path(directory) / name)
        )
        for name in sorted(file_names):
            path = Path(directory) / name
            if len(files) >= _MAX_FILES:
                truncated = True
                break
            try:
                relative = path.relative_to(root)
                if path_service.is_link_like(path) or _is_sensitive(relative):
                    skipped += 1
                    continue
                stat = path.stat()
                if not path.is_file() or stat.st_size > _MAX_FILE_BYTES:
                    skipped += 1
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except (FileNotFoundError, PermissionError, OSError):
                skipped += 1
                continue
            key = relative.as_posix()
            files[key] = WorkspaceFileSnapshot(
                key, digest, stat.st_size, stat.st_mtime_ns
            )
        if truncated:
            break
    return WorkspaceBaseline(
        workspace=path_service.workspace_key(root),
        captured_at=datetime.now(timezone.utc),
        files=dict(sorted(files.items())),
        git_head=_read_git_head(root, path_service),
        skipped_files=skipped, truncated=truncated,
    )


def compare_workspace_baseline(
    baseline: WorkspaceBaseline, workspace: Path, path_service: WorkspacePathPort,
) -> WorkspaceChangeSet:
    current = capture_workspace_baseline(workspace, path_service)
    if current.workspace != baseline.workspace:
        raise ValueError("workspace does not match the baseline canonical path")
    before = baseline.files
    after = current.files
    added = tuple(sorted(set(after) - set(before)))
    deleted = tuple(sorted(set(before) - set(after)))
    modified = tuple(
        path for path in sorted(set(before) & set(after))
        if before[path].sha256 != after[path].sha256
    )
    return WorkspaceChangeSet(
        added, modified, deleted, baseline.git_head != current.git_head,
        current.git_head, current.truncated, current.skipped_files,
    )


def _is_sensitive(relative: Path) -> bool:
    for part in relative.parts:
        lowered = part.lower()
        if lowered in _SENSITIVE_NAMES:
            return True
        if lowered == ".env" or lowered.startswith(".env."):
            return True
        if Path(lowered).suffix in _SENSITIVE_SUFFIXES:
            return True
    return False


def _read_git_head(root: Path, path_service: WorkspacePathPort) -> str | None:
    git_directory = root / ".git"
    if not git_directory.is_dir() or path_service.is_link_like(git_directory):
        return None
    try:
        resolved_head = path_service.resolve_access_path(root, ".git/HEAD")
        if (
            not resolved_head.relative_path.startswith(".git/")
            or path_service.is_link_like(git_directory / "HEAD")
        ):
            return None
        head_path = resolved_head.path
        head = head_path.read_text(encoding="utf-8").strip()
    except (ValueError, FileNotFoundError, PermissionError, OSError, UnicodeDecodeError):
        return None
    if not head.startswith("ref: " ):
        return head or None
    reference = head[5:].strip()
    if not reference or reference.startswith("/") or ".." in Path(reference).parts:
        return None
    try:
        resolved = path_service.resolve_access_path(root, f".git/{reference}")
        if not resolved.relative_path.startswith(".git/"):
            return None
        reference_path = resolved.path
        return reference_path.read_text(encoding="utf-8").strip() or None
    except (ValueError, FileNotFoundError, PermissionError, OSError, UnicodeDecodeError):
        return None
