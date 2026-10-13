"""Workspace writes, deletions and mutation rollback for ``Kernel``.

A mixin module in the facade-plus-siblings shape (``run_agent.py`` +
``agent/session_persistence.py``): the facade ``core/kernel.py`` imports
this at module level and adds ``WorkspaceMixin`` to ``Kernel``'s bases, so
**every method resolves through ``Kernel``'s MRO unchanged** --
``self._dependencies``, ``self._append_events`` and ``Kernel``'s own class
attributes all still resolve. Moved bodies are verbatim: nothing is renamed
and no signature changes.

This module must never bind the facade at module level; late-import it
inside a method instead (the project's module-boundary rule).

Moved from ``core/kernel.py``:

* ``_compensate_prepared_mutation``
* ``_preflight_cascade_rollback``
* ``_require_rollback_candidate``
* ``_rollback_workspace_mutation_locked``
* ``_validate_batch_rollback``
* ``_validate_cascade_rollback``
* ``_validate_grouped_rollback``
* ``delete_workspace_file``
* ``get_workspace_changes``
* ``recover_workspace_transactions``
* ``rollback_workspace_mutation``
* ``rollback_workspace_mutation_batch``
* ``rollback_workspace_mutation_groups``
* ``rollback_workspace_mutations``
* ``write_workspace_text``
* ``write_workspace_text_batch``
"""

from __future__ import annotations


from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from tsm_agt.ports import RuntimeEvent, RuntimeUnitOfWork
from typing import Any
from uuid import uuid4
from .task import TaskSnapshot, TaskState
from .turn import InvalidTurnState
from .workspace import (
    MutationOperation, MutationRecord, WorkspaceChangeSet,
    WorkspaceMutationConflict, WorkspaceTransactionEntry,
    WorkspaceTransactionManifest, commit_prepared_workspace_deletion,
    commit_prepared_workspace_mutation, compare_workspace_baseline,
    file_sha256, prepare_workspace_bytes_write,
    prepare_workspace_file_delete, prepare_workspace_text_write,
    read_mutation_backup, read_workspace_transaction_manifests,
    remove_workspace_transaction_manifest,
    restore_prepared_workspace_deletion,
    restore_prepared_workspace_mutation, write_mutation_backup,
    write_workspace_transaction_manifest
)


class WorkspaceMixin:
    async def get_workspace_changes(self, task_id: str) -> WorkspaceChangeSet:
        task = await self.get_task(task_id)
        if task.workspace_baseline is None:
            raise RuntimeError("task does not contain a workspace baseline")
        changes = compare_workspace_baseline(
            task.workspace_baseline, Path(task.workspace),
            self._dependencies.workspace_path,
        )
        await self._append_events(task_id, (("workspace.compared", {
            "added": list(changes.added),
            "modified": list(changes.modified),
            "deleted": list(changes.deleted),
            "git_head_changed": changes.git_head_changed,
            "current_git_head": changes.current_git_head,
            "scan_truncated": changes.scan_truncated,
            "skipped_files": changes.skipped_files,
        }),))
        return changes

    async def write_workspace_text(
        self, task_id: str, step_id: str, relative_path: str, content: str,
        expected_hash: str | None,
    ) -> MutationRecord:
        await self.recover_workspace_transactions(task_id)
        if not step_id.strip():
            raise ValueError("step_id must not be empty")
        task = await self.get_task(task_id)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        workspace = Path(task.workspace)
        async with self._workspace_path_locks.hold(workspace, relative_path):
            stored = await self._require_stored_task(task_id)
            task = TaskSnapshot.from_data(stored.data)
            if task.state is not TaskState.EXECUTING:
                raise InvalidTurnState(
                    f"task {task_id} must be EXECUTING, got {task.state.value}"
                )
            prepared = prepare_workspace_text_write(
                workspace, relative_path, content, expected_hash,
                self._dependencies.workspace_path,
            )
            mutation_id = f"mutation-{uuid4().hex}"
            backup_ref = None
            if prepared.before_bytes is not None:
                backup_ref = write_mutation_backup(
                    workspace, self._safe_storage_key(task_id),
                    mutation_id, prepared.before_bytes,
                    self._dependencies.workspace_filesystem,
                    self._dependencies.workspace_path,
                )
            mutation = MutationRecord(
                mutation_id=mutation_id, path=prepared.relative_path,
                operation=prepared.operation, before_hash=prepared.before_hash,
                after_hash=prepared.after_hash, step_id=step_id.strip(),
                backup_ref=backup_ref, created_at=datetime.now(timezone.utc),
                before_mode=prepared.previous_mode,
            )
            created_directories = commit_prepared_workspace_mutation(
                prepared, self._dependencies.workspace_filesystem
            )
            updated = task.with_mutation(mutation)
            event = RuntimeEvent(
                event_id=f"evt-{uuid4().hex}", task_id=task_id,
                sequence=stored.last_event_sequence + 1,
                event_type="workspace.mutation_committed",
                payload=mutation.to_data(),
            )
            try:
                await self._dependencies.store.commit(RuntimeUnitOfWork(
                    task_id, stored.version, updated.to_data(), (event,)
                ))
            except Exception:
                try:
                    restore_prepared_workspace_mutation(
                        prepared, self._dependencies.workspace_filesystem,
                        created_directories,
                    )
                except Exception as restore_error:
                    raise RuntimeError(
                        "workspace mutation was written but journal commit failed and "
                        "automatic restoration could not be proven safe"
                    ) from restore_error
                raise
            return mutation

    async def write_workspace_text_batch(
        self, task_id: str, step_id: str,
        writes: tuple[tuple[str, str, str | None], ...],
    ) -> tuple[MutationRecord, ...]:
        """Apply 2..50 distinct text writes with preflight and compensation."""
        await self.recover_workspace_transactions(task_id)
        if not step_id.strip():
            raise ValueError("step_id must not be empty")
        if len(writes) < 2 or len(writes) > 50:
            raise ValueError("batch patch requires 2..50 files")
        task = await self.get_task(task_id)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        workspace = Path(task.workspace)
        paths = tuple(path for path, _content, _hash in writes)
        async with self._workspace_path_locks.hold_many(workspace, paths):
            stored = await self._require_stored_task(task_id)
            task = TaskSnapshot.from_data(stored.data)
            if task.state is not TaskState.EXECUTING:
                raise InvalidTurnState(
                    f"task {task_id} must be EXECUTING, got {task.state.value}"
                )

            prepared = tuple(
                prepare_workspace_text_write(
                    workspace, path, content, expected_hash,
                    self._dependencies.workspace_path,
                )
                for path, content, expected_hash in writes
            )
            storage_key = self._safe_storage_key(task_id)
            records: list[MutationRecord] = []
            for item in prepared:
                mutation_id = f"mutation-{uuid4().hex}"
                backup_ref = None
                if item.before_bytes is not None:
                    backup_ref = write_mutation_backup(
                        workspace, storage_key, mutation_id, item.before_bytes,
                        self._dependencies.workspace_filesystem,
                        self._dependencies.workspace_path,
                    )
                records.append(MutationRecord(
                    mutation_id=mutation_id, path=item.relative_path,
                    operation=item.operation, before_hash=item.before_hash,
                    after_hash=item.after_hash, step_id=step_id.strip(),
                    backup_ref=backup_ref, created_at=datetime.now(timezone.utc),
                    before_mode=item.previous_mode,
                ))
            transaction = WorkspaceTransactionManifest(
                transaction_id=f"workspace-tx-{uuid4().hex}",
                task_id=task_id, step_id=step_id.strip(), kind="apply_patches",
                created_at=datetime.now(timezone.utc),
                entries=tuple(
                    WorkspaceTransactionEntry(
                        mutation_id=record.mutation_id, path=record.path,
                        before_hash=record.before_hash, after_hash=record.after_hash,
                        backup_ref=record.backup_ref, before_mode=record.before_mode,
                    )
                    for record in records
                ),
            )
            manifest_path = write_workspace_transaction_manifest(
                workspace, storage_key, transaction,
                self._dependencies.workspace_filesystem,
                self._dependencies.workspace_path,
            )

            attempted: list[tuple[Any, tuple[Path, ...]]] = []
            try:
                for item in prepared:
                    # Register the action before calling it: a fault may be raised
                    # after the atomic replace has already happened. The candidate
                    # parents are narrowed to the commit's authoritative return on
                    # the ordinary success path.
                    attempted.append((
                        item,
                        tuple(
                            directory for directory in item.parent_directories
                            if not directory.exists()
                        ),
                    ))
                    created_directories = commit_prepared_workspace_mutation(
                        item, self._dependencies.workspace_filesystem
                    )
                    attempted[-1] = (item, created_directories or ())
                updated = task
                for record in records:
                    updated = updated.with_mutation(record)
                events = tuple(
                    RuntimeEvent(
                        event_id=f"evt-{uuid4().hex}", task_id=task_id,
                        sequence=stored.last_event_sequence + index,
                        event_type="workspace.mutation_committed",
                        payload=record.to_data(),
                    )
                    for index, record in enumerate(records, start=1)
                )
                await self._dependencies.store.commit(RuntimeUnitOfWork(
                    task_id, stored.version, updated.to_data(), events
                ))
            except Exception:
                restoration_errors: list[Exception] = []
                for item, created_directories in reversed(attempted):
                    try:
                        self._compensate_prepared_mutation(
                            item, created_directories
                        )
                    except Exception as restore_error:
                        restoration_errors.append(restore_error)
                if restoration_errors:
                    raise RuntimeError(
                        "batch patch failed and restoration of every written file "
                        "could not be proven safe"
                    ) from restoration_errors[0]
                remove_workspace_transaction_manifest(
                    manifest_path, self._dependencies.workspace_filesystem
                )
                raise
            try:
                remove_workspace_transaction_manifest(
                    manifest_path, self._dependencies.workspace_filesystem
                )
            except OSError:
                # SQLite and all files are already committed. A durable residue is
                # safer than reporting failure and inviting a duplicate retry; the
                # recovery path recognizes the committed mutation IDs and cleans it.
                pass
            return tuple(records)

    async def delete_workspace_file(
        self, task_id: str, step_id: str, relative_path: str, expected_hash: str,
    ) -> MutationRecord:
        await self.recover_workspace_transactions(task_id)
        if not step_id.strip():
            raise ValueError("step_id must not be empty")
        task = await self.get_task(task_id)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        workspace = Path(task.workspace)
        async with self._workspace_path_locks.hold(workspace, relative_path):
            stored = await self._require_stored_task(task_id)
            task = TaskSnapshot.from_data(stored.data)
            if task.state is not TaskState.EXECUTING:
                raise InvalidTurnState(
                    f"task {task_id} must be EXECUTING, got {task.state.value}"
                )
            prepared = prepare_workspace_file_delete(
                workspace, relative_path, expected_hash,
                self._dependencies.workspace_path,
            )
            mutation_id = f"mutation-{uuid4().hex}"
            backup_ref = write_mutation_backup(
                workspace, self._safe_storage_key(task_id),
                mutation_id, prepared.before_bytes,
                self._dependencies.workspace_filesystem,
                self._dependencies.workspace_path,
            )
            mutation = MutationRecord(
                mutation_id=mutation_id, path=prepared.relative_path,
                operation=MutationOperation.DELETE, before_hash=prepared.before_hash,
                after_hash=None, step_id=step_id.strip(), backup_ref=backup_ref,
                created_at=datetime.now(timezone.utc),
                before_mode=prepared.previous_mode,
            )
            commit_prepared_workspace_deletion(
                prepared, self._dependencies.workspace_filesystem
            )
            updated = task.with_mutation(mutation)
            event = RuntimeEvent(
                event_id=f"evt-{uuid4().hex}", task_id=task_id,
                sequence=stored.last_event_sequence + 1,
                event_type="workspace.mutation_committed",
                payload=mutation.to_data(),
            )
            try:
                await self._dependencies.store.commit(RuntimeUnitOfWork(
                    task_id, stored.version, updated.to_data(), (event,)
                ))
            except Exception:
                try:
                    restore_prepared_workspace_deletion(
                        prepared, self._dependencies.workspace_filesystem
                    )
                except Exception as restore_error:
                    raise RuntimeError(
                        "workspace deletion completed but journal commit failed and "
                        "automatic restoration could not be proven safe"
                    ) from restore_error
                raise
            return mutation

    async def rollback_workspace_mutation(
        self, task_id: str, step_id: str, mutation_id: str,
    ) -> MutationRecord:
        await self.recover_workspace_transactions(task_id)
        if not step_id.strip() or not mutation_id.strip():
            raise ValueError("step_id and mutation_id must not be empty")
        task = await self.get_task(task_id)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        original = self._require_rollback_candidate(task, mutation_id)
        workspace = Path(task.workspace)
        async with self._workspace_path_locks.hold(workspace, original.path):
            return await self._rollback_workspace_mutation_locked(
                task_id, step_id.strip(), mutation_id, workspace
            )

    async def rollback_workspace_mutations(
        self, task_id: str, step_id: str, mutation_ids: tuple[str, ...],
    ) -> tuple[MutationRecord, ...]:
        await self.recover_workspace_transactions(task_id)
        if not step_id.strip():
            raise ValueError("step_id must not be empty")
        if len(mutation_ids) < 2 or len(mutation_ids) > 100:
            raise ValueError("cascade rollback requires 2..100 mutation IDs")
        if any(not item.strip() for item in mutation_ids):
            raise ValueError("mutation IDs must not be empty")
        if len(set(mutation_ids)) != len(mutation_ids):
            raise ValueError("cascade rollback mutation IDs must be unique")
        task = await self.get_task(task_id)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        path = self._validate_cascade_rollback(task, mutation_ids)
        workspace = Path(task.workspace)
        async with self._workspace_path_locks.hold(workspace, path):
            current = await self.get_task(task_id)
            self._validate_cascade_rollback(current, mutation_ids)
            self._preflight_cascade_rollback(
                current, mutation_ids, workspace,
                self._safe_storage_key(task_id),
            )
            results: list[MutationRecord] = []
            for mutation_id in mutation_ids:
                results.append(await self._rollback_workspace_mutation_locked(
                    task_id, step_id.strip(), mutation_id, workspace
                ))
            return tuple(results)

    async def rollback_workspace_mutation_batch(
        self, task_id: str, step_id: str, mutation_ids: tuple[str, ...],
    ) -> tuple[MutationRecord, ...]:
        """Rollback one latest active mutation on each of 2..50 files."""
        await self.recover_workspace_transactions(task_id)
        if not step_id.strip():
            raise ValueError("step_id must not be empty")
        if len(mutation_ids) < 2 or len(mutation_ids) > 50:
            raise ValueError("batch rollback requires 2..50 mutation IDs")
        if any(not item.strip() for item in mutation_ids):
            raise ValueError("mutation IDs must not be empty")
        if len(set(mutation_ids)) != len(mutation_ids):
            raise ValueError("batch rollback mutation IDs must be unique")
        task = await self.get_task(task_id)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        selected = self._validate_batch_rollback(task, mutation_ids)
        workspace = Path(task.workspace)
        paths = tuple(item.path for item in selected)
        async with self._workspace_path_locks.hold_many(workspace, paths):
            stored = await self._require_stored_task(task_id)
            task = TaskSnapshot.from_data(stored.data)
            selected = self._validate_batch_rollback(task, mutation_ids)
            storage_key = self._safe_storage_key(task_id)
            self._preflight_cascade_rollback(
                task, mutation_ids, workspace, storage_key
            )

            prepared_items: list[tuple[str, Any, MutationRecord]] = []
            for original in selected:
                rollback_id = f"mutation-{uuid4().hex}"
                if original.operation is MutationOperation.CREATE:
                    if original.after_hash is None:
                        raise ValueError("created mutation is missing after_hash")
                    prepared = prepare_workspace_file_delete(
                        workspace, original.path, original.after_hash,
                        self._dependencies.workspace_path,
                    )
                    backup_ref = write_mutation_backup(
                        workspace, storage_key, rollback_id, prepared.before_bytes,
                        self._dependencies.workspace_filesystem,
                        self._dependencies.workspace_path,
                    )
                    record = MutationRecord(
                        mutation_id=rollback_id, path=original.path,
                        operation=MutationOperation.DELETE,
                        before_hash=prepared.before_hash, after_hash=None,
                        step_id=step_id.strip(), backup_ref=backup_ref,
                        created_at=datetime.now(timezone.utc),
                        before_mode=prepared.previous_mode,
                        reverts_mutation_id=original.mutation_id,
                    )
                    prepared_items.append(("delete", prepared, record))
                    continue
                if original.backup_ref is None or original.before_hash is None:
                    raise ValueError("mutation does not contain a restorable backup")
                old_content = read_mutation_backup(
                    workspace, storage_key, original.backup_ref, original.before_hash,
                    self._dependencies.workspace_path,
                )
                prepared = prepare_workspace_bytes_write(
                    workspace, original.path, old_content, original.after_hash,
                    self._dependencies.workspace_path,
                    result_mode=original.before_mode,
                )
                backup_ref = None
                if prepared.before_bytes is not None:
                    backup_ref = write_mutation_backup(
                        workspace, storage_key, rollback_id, prepared.before_bytes,
                        self._dependencies.workspace_filesystem,
                        self._dependencies.workspace_path,
                    )
                record = MutationRecord(
                    mutation_id=rollback_id, path=original.path,
                    operation=prepared.operation, before_hash=prepared.before_hash,
                    after_hash=prepared.after_hash, step_id=step_id.strip(),
                    backup_ref=backup_ref, created_at=datetime.now(timezone.utc),
                    before_mode=prepared.previous_mode,
                    reverts_mutation_id=original.mutation_id,
                )
                prepared_items.append(("write", prepared, record))

            transaction = WorkspaceTransactionManifest(
                transaction_id=f"workspace-tx-{uuid4().hex}",
                task_id=task_id, step_id=step_id.strip(),
                kind="rollback_mutation_batch",
                created_at=datetime.now(timezone.utc),
                entries=tuple(
                    WorkspaceTransactionEntry(
                        mutation_id=record.mutation_id, path=record.path,
                        before_hash=record.before_hash, after_hash=record.after_hash,
                        backup_ref=record.backup_ref, before_mode=record.before_mode,
                    )
                    for _kind, _prepared, record in prepared_items
                ),
            )
            manifest_path = write_workspace_transaction_manifest(
                workspace, storage_key, transaction,
                self._dependencies.workspace_filesystem,
                self._dependencies.workspace_path,
            )

            attempted: list[tuple[str, Any]] = []
            try:
                for kind, prepared, _record in prepared_items:
                    attempted.append((kind, prepared))
                    if kind == "delete":
                        commit_prepared_workspace_deletion(
                            prepared, self._dependencies.workspace_filesystem
                        )
                    else:
                        commit_prepared_workspace_mutation(
                            prepared, self._dependencies.workspace_filesystem
                        )
                updated = task
                for _kind, _prepared, record in prepared_items:
                    updated = updated.with_mutation(record)
                events = tuple(
                    RuntimeEvent(
                        event_id=f"evt-{uuid4().hex}", task_id=task_id,
                        sequence=stored.last_event_sequence + index,
                        event_type="workspace.rollback_committed",
                        payload=record.to_data(),
                    )
                    for index, (_kind, _prepared, record)
                    in enumerate(prepared_items, start=1)
                )
                await self._dependencies.store.commit(RuntimeUnitOfWork(
                    task_id, stored.version, updated.to_data(), events
                ))
            except Exception:
                restoration_errors: list[Exception] = []
                for kind, prepared in reversed(attempted):
                    try:
                        self._compensate_prepared(kind, prepared)
                    except Exception as restore_error:
                        restoration_errors.append(restore_error)
                if restoration_errors:
                    raise RuntimeError(
                        "batch rollback failed and restoration of every changed file "
                        "could not be proven safe"
                    ) from restoration_errors[0]
                remove_workspace_transaction_manifest(
                    manifest_path, self._dependencies.workspace_filesystem
                )
                raise
            try:
                remove_workspace_transaction_manifest(
                    manifest_path, self._dependencies.workspace_filesystem
                )
            except OSError:
                pass
            return tuple(record for _kind, _prepared, record in prepared_items)

    async def rollback_workspace_mutation_groups(
        self, task_id: str, step_id: str,
        mutation_groups: tuple[tuple[str, ...], ...],
    ) -> tuple[MutationRecord, ...]:
        """Rollback newest contiguous chains on several distinct files."""
        await self.recover_workspace_transactions(task_id)
        if not step_id.strip():
            raise ValueError("step_id must not be empty")
        if len(mutation_groups) < 2 or len(mutation_groups) > 50:
            raise ValueError("grouped rollback requires 2..50 file groups")
        if any(not group for group in mutation_groups):
            raise ValueError("rollback mutation groups must not be empty")
        flattened = tuple(
            mutation_id for group in mutation_groups for mutation_id in group
        )
        if len(flattened) > 100:
            raise ValueError("grouped rollback supports at most 100 mutation IDs")
        if any(not mutation_id.strip() for mutation_id in flattened):
            raise ValueError("mutation IDs must not be empty")
        if len(set(flattened)) != len(flattened):
            raise ValueError("grouped rollback mutation IDs must be unique")
        task = await self.get_task(task_id)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        paths = self._validate_grouped_rollback(task, mutation_groups)
        workspace = Path(task.workspace)
        async with self._workspace_path_locks.hold_many(workspace, paths):
            stored = await self._require_stored_task(task_id)
            task = TaskSnapshot.from_data(stored.data)
            self._validate_grouped_rollback(task, mutation_groups)
            storage_key = self._safe_storage_key(task_id)
            self._preflight_cascade_rollback(
                task, flattened, workspace, storage_key
            )

            prepared_groups: list[tuple[str, Any | None, tuple[MutationRecord, ...]]] = []
            for group in mutation_groups:
                originals = tuple(
                    self._require_rollback_candidate(task, mutation_id)
                    for mutation_id in group
                )
                newest, oldest = originals[0], originals[-1]
                prepared_kind: str
                prepared: Any | None
                if oldest.before_hash is None:
                    if newest.after_hash is None:
                        current_path = workspace / newest.path
                        if current_path.exists() or current_path.is_symlink():
                            raise WorkspaceMutationConflict(
                                newest.path, None, file_sha256(current_path)
                            )
                        prepared_kind, prepared = "noop", None
                    else:
                        prepared_kind = "delete"
                        prepared = prepare_workspace_file_delete(
                            workspace, newest.path, newest.after_hash,
                            self._dependencies.workspace_path,
                        )
                else:
                    if oldest.backup_ref is None:
                        raise ValueError("oldest mutation is missing its backup")
                    final_content = read_mutation_backup(
                        workspace, storage_key, oldest.backup_ref, oldest.before_hash,
                        self._dependencies.workspace_path,
                    )
                    prepared_kind = "write"
                    prepared = prepare_workspace_bytes_write(
                        workspace, newest.path, final_content, newest.after_hash,
                        self._dependencies.workspace_path,
                        result_mode=oldest.before_mode,
                    )

                rollback_records: list[MutationRecord] = []
                for index, original in enumerate(originals):
                    rollback_id = f"mutation-{uuid4().hex}"
                    backup_ref = None
                    before_mode = None
                    if original.after_hash is not None:
                        if index == 0:
                            if prepared is None:
                                raise WorkspaceMutationConflict(
                                    original.path, original.after_hash, None
                                )
                            before_content = prepared.before_bytes
                            before_mode = prepared.previous_mode
                            if before_content is None:
                                raise WorkspaceMutationConflict(
                                    original.path, original.after_hash, None
                                )
                        else:
                            newer = originals[index - 1]
                            if newer.backup_ref is None or newer.before_hash is None:
                                raise ValueError(
                                    "mutation chain is missing an intermediate backup"
                                )
                            before_content = read_mutation_backup(
                                workspace, storage_key, newer.backup_ref,
                                newer.before_hash,
                                self._dependencies.workspace_path,
                            )
                            before_mode = newer.before_mode
                        backup_ref = write_mutation_backup(
                            workspace, storage_key, rollback_id, before_content,
                            self._dependencies.workspace_filesystem,
                            self._dependencies.workspace_path,
                        )
                    operation = (
                        MutationOperation.DELETE
                        if original.before_hash is None
                        else MutationOperation.CREATE
                        if original.after_hash is None
                        else MutationOperation.MODIFY
                    )
                    rollback_records.append(MutationRecord(
                        mutation_id=rollback_id, path=original.path,
                        operation=operation, before_hash=original.after_hash,
                        after_hash=original.before_hash, step_id=step_id.strip(),
                        backup_ref=backup_ref, created_at=datetime.now(timezone.utc),
                        before_mode=before_mode,
                        reverts_mutation_id=original.mutation_id,
                    ))

                prepared_groups.append((
                    prepared_kind, prepared, tuple(rollback_records)
                ))

            attempted: list[tuple[str, Any]] = []
            all_records = tuple(
                record for _kind, _prepared, records in prepared_groups
                for record in records
            )
            transaction_entries: list[WorkspaceTransactionEntry] = []
            for kind, prepared, records in prepared_groups:
                if not records:
                    raise ValueError("grouped rollback produced no journal records")
                if kind == "noop":
                    before_hash = after_hash = backup_ref = before_mode = None
                elif kind == "delete":
                    before_hash, after_hash = prepared.before_hash, None
                    backup_ref, before_mode = (
                        records[0].backup_ref, prepared.previous_mode
                    )
                else:
                    before_hash, after_hash = (
                        prepared.before_hash, prepared.after_hash
                    )
                    backup_ref, before_mode = (
                        records[0].backup_ref, prepared.previous_mode
                    )
                transaction_entries.append(WorkspaceTransactionEntry(
                    mutation_id=records[0].mutation_id,
                    path=records[0].path, before_hash=before_hash,
                    after_hash=after_hash, backup_ref=backup_ref,
                    before_mode=before_mode,
                    mutation_ids=tuple(record.mutation_id for record in records),
                ))
            transaction = WorkspaceTransactionManifest(
                transaction_id=f"workspace-tx-{uuid4().hex}",
                task_id=task_id, step_id=step_id.strip(),
                kind="rollback_mutation_groups",
                created_at=datetime.now(timezone.utc),
                entries=tuple(transaction_entries),
            )
            manifest_path = write_workspace_transaction_manifest(
                workspace, storage_key, transaction,
                self._dependencies.workspace_filesystem,
                self._dependencies.workspace_path,
            )
            try:
                for kind, prepared, _records in prepared_groups:
                    if kind == "noop":
                        continue
                    attempted.append((kind, prepared))
                    if kind == "delete":
                        commit_prepared_workspace_deletion(
                            prepared, self._dependencies.workspace_filesystem
                        )
                    else:
                        commit_prepared_workspace_mutation(
                            prepared, self._dependencies.workspace_filesystem
                        )
                updated = task
                for record in all_records:
                    updated = updated.with_mutation(record)
                events = tuple(
                    RuntimeEvent(
                        event_id=f"evt-{uuid4().hex}", task_id=task_id,
                        sequence=stored.last_event_sequence + index,
                        event_type="workspace.rollback_committed",
                        payload=record.to_data(),
                    )
                    for index, record in enumerate(all_records, start=1)
                )
                await self._dependencies.store.commit(RuntimeUnitOfWork(
                    task_id, stored.version, updated.to_data(), events
                ))
            except Exception:
                restoration_errors: list[Exception] = []
                for kind, prepared in reversed(attempted):
                    try:
                        self._compensate_prepared(kind, prepared)
                    except Exception as restore_error:
                        restoration_errors.append(restore_error)
                if restoration_errors:
                    raise RuntimeError(
                        "grouped rollback failed and restoration of every changed "
                        "file could not be proven safe"
                    ) from restoration_errors[0]
                remove_workspace_transaction_manifest(
                    manifest_path, self._dependencies.workspace_filesystem
                )
                raise
            try:
                remove_workspace_transaction_manifest(
                    manifest_path, self._dependencies.workspace_filesystem
                )
            except OSError:
                pass
            return all_records

    def _compensate_prepared_mutation(
        self, prepared: Any, created_directories: tuple[Path, ...] = (),
    ) -> None:
        """Restore an applied write, accept an untouched target, reject ambiguity."""
        current_hash = file_sha256(prepared.path)
        if current_hash == prepared.after_hash:
            restore_prepared_workspace_mutation(
                prepared, self._dependencies.workspace_filesystem,
                created_directories,
            )
            return
        untouched = (
            current_hash == prepared.before_hash
            and (
                prepared.before_hash is not None
                or (not prepared.path.exists() and not prepared.path.is_symlink())
            )
        )
        if untouched:
            return
        raise WorkspaceMutationConflict(
            prepared.relative_path, prepared.after_hash, current_hash
        )

    @staticmethod
    def _require_rollback_candidate(
        task: TaskSnapshot, mutation_id: str,
    ) -> MutationRecord:
        original = next(
            (item for item in task.mutation_journal
             if item.mutation_id == mutation_id),
            None,
        )
        if original is None:
            raise ValueError(f"mutation not found in task journal: {mutation_id}")
        if original.reverts_mutation_id is not None:
            raise ValueError("rollback records cannot be used as cascade originals")
        if any(
            item.reverts_mutation_id == mutation_id
            for item in task.mutation_journal
        ):
            raise ValueError(f"mutation was already rolled back: {mutation_id}")
        return original

    @classmethod
    def _validate_batch_rollback(
        cls, task: TaskSnapshot, mutation_ids: tuple[str, ...],
    ) -> tuple[MutationRecord, ...]:
        selected = tuple(
            cls._require_rollback_candidate(task, mutation_id)
            for mutation_id in mutation_ids
        )
        paths = tuple(item.path for item in selected)
        if len(set(paths)) != len(paths):
            raise ValueError(
                "batch rollback requires exactly one mutation per file path"
            )
        reverted = {
            item.reverts_mutation_id for item in task.mutation_journal
            if item.reverts_mutation_id is not None
        }
        for original in selected:
            active = [
                item for item in task.mutation_journal
                if item.path == original.path
                and item.reverts_mutation_id is None
                and item.mutation_id not in reverted
            ]
            if not active or active[-1].mutation_id != original.mutation_id:
                raise ValueError(
                    "batch rollback requires the newest active mutation for each path"
                )
        return selected

    @classmethod
    def _validate_grouped_rollback(
        cls, task: TaskSnapshot,
        mutation_groups: tuple[tuple[str, ...], ...],
    ) -> tuple[str, ...]:
        paths = tuple(
            cls._validate_cascade_rollback(task, group)
            for group in mutation_groups
        )
        if len(set(paths)) != len(paths):
            raise ValueError(
                "grouped rollback requires exactly one group per file path"
            )
        return paths

    @classmethod
    def _validate_cascade_rollback(
        cls, task: TaskSnapshot, mutation_ids: tuple[str, ...],
    ) -> str:
        selected = [
            cls._require_rollback_candidate(task, item)
            for item in mutation_ids
        ]
        paths = {item.path for item in selected}
        if len(paths) != 1:
            raise ValueError("cascade rollback supports exactly one file path")
        path = selected[0].path
        reverted = {
            item.reverts_mutation_id for item in task.mutation_journal
            if item.reverts_mutation_id is not None
        }
        active = [
            item for item in task.mutation_journal
            if item.path == path
            and item.reverts_mutation_id is None
            and item.mutation_id not in reverted
        ]
        expected = tuple(
            item.mutation_id for item in reversed(active[-len(mutation_ids):])
        )
        if mutation_ids != expected:
            raise ValueError(
                "cascade rollback IDs must be the newest active mutations for one "
                "path in reverse journal order"
            )
        for newer, older in zip(selected, selected[1:]):
            if newer.before_hash != older.after_hash:
                raise ValueError(
                    "cascade rollback mutations do not form a contiguous hash chain"
                )
        return path

    def _preflight_cascade_rollback(
        self, task: TaskSnapshot, mutation_ids: tuple[str, ...],
        workspace: Path, storage_key: str,
    ) -> None:
        """Verify every required backup before the first file change."""
        for mutation_id in mutation_ids:
            original = self._require_rollback_candidate(task, mutation_id)
            if original.operation is MutationOperation.CREATE:
                continue
            if original.backup_ref is None or original.before_hash is None:
                raise ValueError("mutation does not contain a restorable backup")
            read_mutation_backup(
                workspace, storage_key, original.backup_ref, original.before_hash,
                self._dependencies.workspace_path,
            )

    async def _rollback_workspace_mutation_locked(
        self, task_id: str, step_id: str, mutation_id: str, workspace: Path,
    ) -> MutationRecord:
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        original = self._require_rollback_candidate(task, mutation_id)
        rollback_id = f"mutation-{uuid4().hex}"
        storage_key = self._safe_storage_key(task_id)

        if original.operation is MutationOperation.CREATE:
            if original.after_hash is None:
                raise ValueError("created mutation is missing after_hash")
            prepared_delete = prepare_workspace_file_delete(
                workspace, original.path, original.after_hash,
                self._dependencies.workspace_path,
            )
            backup_ref = write_mutation_backup(
                workspace, storage_key, rollback_id,
                prepared_delete.before_bytes,
                self._dependencies.workspace_filesystem,
                self._dependencies.workspace_path,
            )
            rollback = MutationRecord(
                mutation_id=rollback_id, path=original.path,
                operation=MutationOperation.DELETE,
                before_hash=prepared_delete.before_hash, after_hash=None,
                step_id=step_id, backup_ref=backup_ref,
                created_at=datetime.now(timezone.utc),
                before_mode=prepared_delete.previous_mode,
                reverts_mutation_id=original.mutation_id,
            )
            commit = lambda: commit_prepared_workspace_deletion(
                prepared_delete, self._dependencies.workspace_filesystem
            )
            restore = lambda: restore_prepared_workspace_deletion(
                prepared_delete, self._dependencies.workspace_filesystem
            )
        else:
            if original.backup_ref is None or original.before_hash is None:
                raise ValueError("mutation does not contain a restorable backup")
            old_content = read_mutation_backup(
                workspace, storage_key, original.backup_ref, original.before_hash,
                self._dependencies.workspace_path,
            )
            prepared_write = prepare_workspace_bytes_write(
                workspace, original.path, old_content, original.after_hash,
                self._dependencies.workspace_path,
                result_mode=original.before_mode,
            )
            backup_ref = None
            if prepared_write.before_bytes is not None:
                backup_ref = write_mutation_backup(
                    workspace, storage_key, rollback_id,
                    prepared_write.before_bytes,
                    self._dependencies.workspace_filesystem,
                    self._dependencies.workspace_path,
                )
            rollback = MutationRecord(
                mutation_id=rollback_id, path=original.path,
                operation=prepared_write.operation,
                before_hash=prepared_write.before_hash,
                after_hash=prepared_write.after_hash, step_id=step_id,
                backup_ref=backup_ref, created_at=datetime.now(timezone.utc),
                before_mode=prepared_write.previous_mode,
                reverts_mutation_id=original.mutation_id,
            )
            commit = lambda: commit_prepared_workspace_mutation(
                prepared_write, self._dependencies.workspace_filesystem
            )
            restore = lambda: restore_prepared_workspace_mutation(
                prepared_write, self._dependencies.workspace_filesystem
            )

        commit()
        updated = task.with_mutation(rollback)
        event = RuntimeEvent(
            event_id=f"evt-{uuid4().hex}", task_id=task_id,
            sequence=stored.last_event_sequence + 1,
            event_type="workspace.rollback_committed",
            payload=rollback.to_data(),
        )
        try:
            await self._dependencies.store.commit(RuntimeUnitOfWork(
                task_id, stored.version, updated.to_data(), (event,)
            ))
        except Exception:
            try:
                restore()
            except Exception as restore_error:
                raise RuntimeError(
                    "workspace rollback changed the file but journal commit failed "
                    "and restoration could not be proven safe"
                ) from restore_error
            raise
        return rollback

    async def recover_workspace_transactions(
        self, task_id: str,
    ) -> tuple[Mapping[str, Any], ...]:
        """Reconcile durable apply-patches manifests after interruption."""
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        workspace = Path(task.workspace)
        storage_key = self._safe_storage_key(task_id)
        manifests = read_workspace_transaction_manifests(
            workspace, storage_key, self._dependencies.workspace_path
        )
        results: list[Mapping[str, Any]] = []
        for manifest_path, manifest in manifests:
            if manifest.task_id != task_id:
                raise PermissionError(
                    "workspace transaction manifest belongs to another Task"
                )
            mutation_ids = {
                mutation_id for entry in manifest.entries
                for mutation_id in entry.effective_mutation_ids
            }
            committed_ids = {
                mutation.mutation_id for mutation in task.mutation_journal
                if mutation.mutation_id in mutation_ids
            }
            if committed_ids and committed_ids != mutation_ids:
                raise RuntimeError(
                    "workspace transaction has a partially committed journal"
                )
            paths = tuple(entry.path for entry in manifest.entries)
            async with self._workspace_path_locks.hold_many(workspace, paths):
                current = await self.get_task(task_id)
                committed_ids = {
                    mutation.mutation_id for mutation in current.mutation_journal
                    if mutation.mutation_id in mutation_ids
                }
                if committed_ids == mutation_ids:
                    status = "committed"
                    restored = 0
                elif committed_ids:
                    raise RuntimeError(
                        "workspace transaction has a partially committed journal"
                    )
                else:
                    prepared: list[tuple[str, Any] | None] = []
                    for entry in manifest.entries:
                        target = workspace / entry.path
                        actual_hash = file_sha256(target)
                        actually_missing = (
                            actual_hash is None
                            and not target.exists()
                            and not target.is_symlink()
                        )
                        if entry.before_hash is None:
                            if actually_missing:
                                prepared.append(None)
                            elif (
                                entry.after_hash is not None
                                and actual_hash == entry.after_hash
                            ):
                                prepared.append((
                                    "delete", prepare_workspace_file_delete(
                                        workspace, entry.path, entry.after_hash,
                                        self._dependencies.workspace_path,
                                    )
                                ))
                            else:
                                raise WorkspaceMutationConflict(
                                    entry.path, entry.after_hash, actual_hash
                                )
                            continue
                        if entry.backup_ref is None:
                            raise ValueError(
                                "workspace transaction entry is missing its backup"
                            )
                        before_content = read_mutation_backup(
                            workspace, storage_key, entry.backup_ref, entry.before_hash,
                            self._dependencies.workspace_path,
                        )
                        if actual_hash == entry.before_hash:
                            prepared.append(None)
                        elif entry.after_hash is None and actually_missing:
                            prepared.append((
                                "write", prepare_workspace_bytes_write(
                                    workspace, entry.path, before_content, None,
                                    self._dependencies.workspace_path,
                                    result_mode=entry.before_mode,
                                )
                            ))
                        elif (
                            entry.after_hash is not None
                            and actual_hash == entry.after_hash
                        ):
                            prepared.append((
                                "write", prepare_workspace_bytes_write(
                                    workspace, entry.path, before_content,
                                    entry.after_hash,
                                    self._dependencies.workspace_path,
                                    result_mode=entry.before_mode,
                                )
                            ))
                        else:
                            raise WorkspaceMutationConflict(
                                entry.path, entry.after_hash, actual_hash
                            )
                    restored = 0
                    for item in reversed(prepared):
                        if item is None:
                            continue
                        kind, operation = item
                        if kind == "delete":
                            commit_prepared_workspace_deletion(
                                operation, self._dependencies.workspace_filesystem
                            )
                        else:
                            commit_prepared_workspace_mutation(
                                operation, self._dependencies.workspace_filesystem
                            )
                        restored += 1
                    status = "restored"

                await self._append_events(task_id, ((
                    "workspace.transaction_recovered",
                    {
                        "transaction_id": manifest.transaction_id,
                        "kind": manifest.kind, "status": status,
                        "entry_count": len(manifest.entries),
                        "restored_files": restored,
                    },
                ),))
                remove_workspace_transaction_manifest(
                    manifest_path, self._dependencies.workspace_filesystem
                )
                results.append({
                    "transaction_id": manifest.transaction_id,
                    "status": status, "entry_count": len(manifest.entries),
                    "restored_files": restored,
                })
            stored = await self._require_stored_task(task_id)
            task = TaskSnapshot.from_data(stored.data)
        return tuple(results)
