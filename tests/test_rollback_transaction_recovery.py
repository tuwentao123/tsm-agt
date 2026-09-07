from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    MutationOperation, MutationRecord, TaskState, WorkspaceMutationConflict,
)
from tsm_agt.core.workspace import (
    WorkspaceTransactionEntry, WorkspaceTransactionManifest,
    commit_prepared_workspace_deletion, commit_prepared_workspace_mutation,
    prepare_workspace_bytes_write, prepare_workspace_file_delete,
    write_mutation_backup, write_workspace_transaction_manifest,
)
from tsm_agt.ports import (
    RuntimeEvent, RuntimeUnitOfWork, WorkspaceFilesystemPort, WorkspacePathPort,
)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RollbackTransactionRecoveryTest(unittest.IsolatedAsyncioTestCase):
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
            "recover crashed rollback transaction", self.workspace,
            "task-rollback-recovery",
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

    def write_manifest(self, entries):
        manifest = WorkspaceTransactionManifest(
            transaction_id="workspace-tx-rollback-crash",
            task_id=self.task.task_id, step_id="inv-rollback-crash",
            kind="rollback_mutation_batch",
            created_at=datetime.now(timezone.utc), entries=tuple(entries),
        )
        path = write_workspace_transaction_manifest(
            self.workspace, self.storage_key, manifest, self.filesystem,
            self.path_service,
        )
        return manifest, path

    async def test_restart_recreates_file_deleted_by_uncommitted_rollback(self) -> None:
        target = self.workspace / "created-by-agent.txt"
        target.write_text("agent-created\n", encoding="utf-8")
        prepared = prepare_workspace_file_delete(
            self.workspace, target.name, sha256("agent-created\n"),
            self.path_service,
        )
        rollback_id = "mutation-rollback-delete"
        backup_ref = write_mutation_backup(
            self.workspace, self.storage_key, rollback_id, prepared.before_bytes,
            self.filesystem, self.path_service,
        )
        manifest, manifest_path = self.write_manifest((WorkspaceTransactionEntry(
            rollback_id, target.name, prepared.before_hash, None, backup_ref,
            prepared.previous_mode,
        ), WorkspaceTransactionEntry(
            "mutation-rollback-peer", "peer.txt", None, sha256("peer\n"),
            None, None,
        )))
        commit_prepared_workspace_deletion(prepared, self.filesystem)
        application = await self.reopened()
        try:
            result = await application.kernel.recover_workspace_transactions(
                self.task.task_id
            )
            self.assertEqual(result[0]["status"], "restored")
            self.assertEqual(result[0]["restored_files"], 1)
            self.assertEqual(target.read_text(), "agent-created\n")
            self.assertFalse(manifest_path.exists())
            self.assertEqual(result[0]["transaction_id"], manifest.transaction_id)
        finally:
            await application.registry.stop_all()

    async def test_restart_deletes_file_recreated_by_uncommitted_rollback(self) -> None:
        target = self.workspace / "deleted-by-agent.txt"
        restored = b"restored-by-rollback\n"
        prepared = prepare_workspace_bytes_write(
            self.workspace, target.name, restored, None, self.path_service
        )
        manifest, manifest_path = self.write_manifest((WorkspaceTransactionEntry(
            "mutation-rollback-create", target.name, None,
            hashlib.sha256(restored).hexdigest(), None, None,
        ), WorkspaceTransactionEntry(
            "mutation-rollback-peer", "peer.txt", None, sha256("peer\n"),
            None, None,
        )))
        commit_prepared_workspace_mutation(prepared, self.filesystem)
        application = await self.reopened()
        try:
            result = await application.kernel.recover_workspace_transactions(
                self.task.task_id
            )
            self.assertEqual(result[0]["restored_files"], 1)
            self.assertFalse(target.exists())
            self.assertFalse(manifest_path.exists())
            self.assertEqual(result[0]["transaction_id"], manifest.transaction_id)
        finally:
            await application.registry.stop_all()

    async def test_restart_restores_modified_file_to_pre_rollback_content(self) -> None:
        target = self.workspace / "modified.txt"
        target.write_text("after-agent-change\n", encoding="utf-8")
        prepared = prepare_workspace_bytes_write(
            self.workspace, target.name, b"before-agent-change\n",
            sha256("after-agent-change\n"), self.path_service,
        )
        rollback_id = "mutation-rollback-modify"
        backup_ref = write_mutation_backup(
            self.workspace, self.storage_key, rollback_id, prepared.before_bytes,
            self.filesystem, self.path_service,
        )
        _manifest, manifest_path = self.write_manifest((WorkspaceTransactionEntry(
            rollback_id, target.name, prepared.before_hash, prepared.after_hash,
            backup_ref, prepared.previous_mode,
        ), WorkspaceTransactionEntry(
            "mutation-rollback-peer", "peer.txt", None, sha256("peer\n"),
            None, None,
        )))
        commit_prepared_workspace_mutation(prepared, self.filesystem)
        application = await self.reopened()
        try:
            result = await application.kernel.recover_workspace_transactions(
                self.task.task_id
            )
            self.assertEqual(result[0]["restored_files"], 1)
            self.assertEqual(target.read_text(), "after-agent-change\n")
            self.assertFalse(manifest_path.exists())
        finally:
            await application.registry.stop_all()

    async def test_committed_rollback_journal_keeps_rollback_result(self) -> None:
        target = self.workspace / "committed-delete.txt"
        target.write_text("agent-created\n", encoding="utf-8")
        prepared = prepare_workspace_file_delete(
            self.workspace, target.name, sha256("agent-created\n"),
            self.path_service,
        )
        rollback_id = "mutation-rollback-committed-delete"
        backup_ref = write_mutation_backup(
            self.workspace, self.storage_key, rollback_id, prepared.before_bytes,
            self.filesystem, self.path_service,
        )
        peer_id = "mutation-rollback-committed-peer"
        _manifest, manifest_path = self.write_manifest((WorkspaceTransactionEntry(
            rollback_id, target.name, prepared.before_hash, None, backup_ref,
            prepared.previous_mode,
        ), WorkspaceTransactionEntry(
            peer_id, "peer.txt", None, sha256("peer\n"), None, None,
        )))
        commit_prepared_workspace_deletion(prepared, self.filesystem)
        stored = await self.first.kernel._require_stored_task(self.task.task_id)
        task = await self.first.kernel.get_task(self.task.task_id)
        records = (
            MutationRecord(
                rollback_id, target.name, MutationOperation.DELETE,
                prepared.before_hash, None, "inv-rollback-crash", backup_ref,
                datetime.now(timezone.utc), prepared.previous_mode, "original-one",
            ),
            MutationRecord(
                peer_id, "peer.txt", MutationOperation.CREATE, None,
                sha256("peer\n"), "inv-rollback-crash", None,
                datetime.now(timezone.utc), None, "original-two",
            ),
        )
        updated = task
        for record in records:
            updated = updated.with_mutation(record)
        events = tuple(
            RuntimeEvent(
                f"evt-rollback-committed-{index}", self.task.task_id,
                stored.last_event_sequence + index, "workspace.rollback_committed",
                record.to_data(),
            )
            for index, record in enumerate(records, start=1)
        )
        await self.first.kernel.dependencies.store.commit(RuntimeUnitOfWork(
            self.task.task_id, stored.version, updated.to_data(), events
        ))
        application = await self.reopened()
        try:
            result = await application.kernel.recover_workspace_transactions(
                self.task.task_id
            )
            self.assertEqual(result[0]["status"], "committed")
            self.assertFalse(target.exists())
            self.assertFalse(manifest_path.exists())
        finally:
            await application.registry.stop_all()

    async def test_user_recreates_deleted_result_with_other_content_blocks_recovery(self) -> None:
        target = self.workspace / "conflicted-delete.txt"
        target.write_text("agent-created\n", encoding="utf-8")
        prepared = prepare_workspace_file_delete(
            self.workspace, target.name, sha256("agent-created\n"),
            self.path_service,
        )
        rollback_id = "mutation-rollback-conflicted-delete"
        backup_ref = write_mutation_backup(
            self.workspace, self.storage_key, rollback_id, prepared.before_bytes,
            self.filesystem, self.path_service,
        )
        _manifest, manifest_path = self.write_manifest((WorkspaceTransactionEntry(
            rollback_id, target.name, prepared.before_hash, None, backup_ref,
            prepared.previous_mode,
        ), WorkspaceTransactionEntry(
            "mutation-rollback-peer", "peer.txt", None, sha256("peer\n"),
            None, None,
        )))
        commit_prepared_workspace_deletion(prepared, self.filesystem)
        target.write_text("user-recreated\n", encoding="utf-8")
        application = await self.reopened()
        try:
            with self.assertRaises(WorkspaceMutationConflict):
                await application.kernel.recover_workspace_transactions(
                    self.task.task_id
                )
            self.assertEqual(target.read_text(), "user-recreated\n")
            self.assertTrue(manifest_path.exists())
        finally:
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
