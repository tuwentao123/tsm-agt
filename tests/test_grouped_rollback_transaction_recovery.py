from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import MutationOperation, MutationRecord, TaskState
from tsm_agt.core.workspace import (
    WorkspaceTransactionEntry, WorkspaceTransactionManifest,
    commit_prepared_workspace_mutation, prepare_workspace_bytes_write,
    write_mutation_backup, write_workspace_transaction_manifest,
)
from tsm_agt.ports import (
    RuntimeEvent, RuntimeUnitOfWork, WorkspaceFilesystemPort, WorkspacePathPort,
)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class GroupedRollbackTransactionRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.database = self.workspace / "runtime.db"
        self.first = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(self.database)
        )
        await self.first.registry.start_all()
        self.filesystem = self.first.registry.require(WorkspaceFilesystemPort)
        self.path_service = self.first.registry.require(WorkspacePathPort)
        task = await self.first.kernel.create_task(
            "recover grouped rollback", self.workspace,
            "task-grouped-recovery",
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await self.first.kernel.transition_task(
                task.task_id, state, state.value
            )
        self.task = task
        self.storage_key = self.first.kernel._safe_storage_key(task.task_id)

    async def asyncTearDown(self) -> None:
        await self.first.registry.stop_all()
        self.temporary.cleanup()

    async def reopened(self):
        await self.first.registry.stop_all()
        application = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(self.database)
        )
        await application.registry.start_all()
        return application

    def grouped_crash(self, *, apply: bool = True):
        target = self.workspace / "history.txt"
        peer = self.workspace / "peer.txt"
        target.write_text("C\n", encoding="utf-8")
        peer.write_text("Y\n", encoding="utf-8")
        prepared_target = prepare_workspace_bytes_write(
            self.workspace, target.name, b"A\n", sha256("C\n"),
            self.path_service,
        )
        prepared_peer = prepare_workspace_bytes_write(
            self.workspace, peer.name, b"X\n", sha256("Y\n"),
            self.path_service,
        )
        target_ids = ("mutation-group-new", "mutation-group-old")
        peer_ids = ("mutation-group-peer",)
        target_backup = write_mutation_backup(
            self.workspace, self.storage_key, target_ids[0],
            prepared_target.before_bytes, self.filesystem, self.path_service,
        )
        peer_backup = write_mutation_backup(
            self.workspace, self.storage_key, peer_ids[0], prepared_peer.before_bytes,
            self.filesystem, self.path_service,
        )
        manifest = WorkspaceTransactionManifest(
            transaction_id="workspace-tx-grouped-crash",
            task_id=self.task.task_id, step_id="inv-grouped-crash",
            kind="rollback_mutation_groups",
            created_at=datetime.now(timezone.utc),
            entries=(
                WorkspaceTransactionEntry(
                    target_ids[0], target.name, prepared_target.before_hash,
                    prepared_target.after_hash, target_backup,
                    prepared_target.previous_mode, target_ids,
                ),
                WorkspaceTransactionEntry(
                    peer_ids[0], peer.name, prepared_peer.before_hash,
                    prepared_peer.after_hash, peer_backup,
                    prepared_peer.previous_mode, peer_ids,
                ),
            ),
        )
        manifest_path = write_workspace_transaction_manifest(
            self.workspace, self.storage_key, manifest, self.filesystem,
            self.path_service,
        )
        if apply:
            commit_prepared_workspace_mutation(prepared_target, self.filesystem)
            commit_prepared_workspace_mutation(prepared_peer, self.filesystem)
        return (
            target, peer, manifest, manifest_path, target_ids, peer_ids,
            target_backup, peer_backup,
        )

    async def commit_records(self, manifest, ids, count=None):
        stored = await self.first.kernel._require_stored_task(self.task.task_id)
        task = await self.first.kernel.get_task(self.task.task_id)
        records = []
        for index, mutation_id in enumerate(ids):
            entry = manifest.entries[0] if index < 2 else manifest.entries[1]
            records.append(MutationRecord(
                mutation_id, entry.path, MutationOperation.MODIFY,
                entry.before_hash, entry.after_hash, manifest.step_id,
                entry.backup_ref if index in {0, 2} else entry.backup_ref,
                datetime.now(timezone.utc), entry.before_mode, f"original-{index}",
            ))
        if count is not None:
            records = records[:count]
        updated = task
        for record in records:
            updated = updated.with_mutation(record)
        events = tuple(
            RuntimeEvent(
                f"evt-group-{index}", self.task.task_id,
                stored.last_event_sequence + index, "workspace.rollback_committed",
                record.to_data(),
            )
            for index, record in enumerate(records, start=1)
        )
        await self.first.kernel.dependencies.store.commit(RuntimeUnitOfWork(
            self.task.task_id, stored.version, updated.to_data(), events
        ))

    async def test_restart_restores_each_file_to_pre_group_state(self) -> None:
        target, peer, _manifest, path, *_rest = self.grouped_crash()
        application = await self.reopened()
        try:
            result = await application.kernel.recover_workspace_transactions(
                self.task.task_id
            )
            self.assertEqual(result[0]["status"], "restored")
            self.assertEqual(result[0]["entry_count"], 2)
            self.assertEqual(result[0]["restored_files"], 2)
            self.assertEqual(target.read_text(), "C\n")
            self.assertEqual(peer.read_text(), "Y\n")
            self.assertFalse(path.exists())
        finally:
            await application.registry.stop_all()

    async def test_all_group_journal_ids_committed_keeps_restored_files(self) -> None:
        target, peer, manifest, path, target_ids, peer_ids, *_ = self.grouped_crash()
        await self.commit_records(manifest, target_ids + peer_ids)
        application = await self.reopened()
        try:
            result = await application.kernel.recover_workspace_transactions(
                self.task.task_id
            )
            self.assertEqual(result[0]["status"], "committed")
            self.assertEqual(target.read_text(), "A\n")
            self.assertEqual(peer.read_text(), "X\n")
            self.assertFalse(path.exists())
        finally:
            await application.registry.stop_all()

    async def test_partial_group_journal_is_rejected_and_manifest_remains(self) -> None:
        target, peer, manifest, path, target_ids, peer_ids, *_ = self.grouped_crash()
        await self.commit_records(manifest, target_ids + peer_ids, count=1)
        application = await self.reopened()
        try:
            with self.assertRaisesRegex(RuntimeError, "partially committed"):
                await application.kernel.recover_workspace_transactions(
                    self.task.task_id
                )
            self.assertEqual(target.read_text(), "A\n")
            self.assertEqual(peer.read_text(), "X\n")
            self.assertTrue(path.exists())
        finally:
            await application.registry.stop_all()

    async def test_noop_group_manifest_is_cleaned_without_file_operation(self) -> None:
        peer = self.workspace / "peer.txt"
        peer.write_text("Y\n", encoding="utf-8")
        peer_prepared = prepare_workspace_bytes_write(
            self.workspace, peer.name, b"X\n", sha256("Y\n"),
            self.path_service,
        )
        peer_id = "mutation-noop-peer"
        peer_backup = write_mutation_backup(
            self.workspace, self.storage_key, peer_id, peer_prepared.before_bytes,
            self.filesystem, self.path_service,
        )
        manifest = WorkspaceTransactionManifest(
            transaction_id="workspace-tx-grouped-noop",
            task_id=self.task.task_id, step_id="inv-grouped-noop",
            kind="rollback_mutation_groups",
            created_at=datetime.now(timezone.utc),
            entries=(
                WorkspaceTransactionEntry(
                    "mutation-noop-new", "absent.txt", None, None, None, None,
                    ("mutation-noop-new", "mutation-noop-old"),
                ),
                WorkspaceTransactionEntry(
                    peer_id, peer.name, peer_prepared.before_hash,
                    peer_prepared.after_hash, peer_backup,
                    peer_prepared.previous_mode, (peer_id,),
                ),
            ),
        )
        path = write_workspace_transaction_manifest(
            self.workspace, self.storage_key, manifest, self.filesystem,
            self.path_service,
        )
        application = await self.reopened()
        try:
            result = await application.kernel.recover_workspace_transactions(
                self.task.task_id
            )
            self.assertEqual(result[0]["restored_files"], 0)
            self.assertFalse((self.workspace / "absent.txt").exists())
            self.assertEqual(peer.read_text(), "Y\n")
            self.assertFalse(path.exists())
        finally:
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
