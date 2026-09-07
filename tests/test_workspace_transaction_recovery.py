from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskState, WorkspaceMutationConflict
from tsm_agt.core.workspace import (
    MutationOperation, MutationRecord, WorkspaceTransactionEntry,
    WorkspaceTransactionManifest, prepare_workspace_text_write,
    commit_prepared_workspace_mutation, write_mutation_backup,
    write_workspace_transaction_manifest,
    read_workspace_transaction_manifests,
)
from tsm_agt.ports import WorkspaceFilesystemPort, WorkspacePathPort


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class WorkspaceTransactionRecoveryTest(unittest.IsolatedAsyncioTestCase):
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
            "recover crashed file transaction", self.workspace,
            "task-workspace-recovery",
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

    def crash_manifest(self, *, apply_second: bool = False):
        first_path = self.workspace / "first.txt"
        second_path = self.workspace / "second.txt"
        first_path.write_text("first-before\n", encoding="utf-8")
        second_path.write_text("second-before\n", encoding="utf-8")
        prepared = (
            prepare_workspace_text_write(
                self.workspace, first_path.name, "first-after\n",
                sha256("first-before\n"), self.path_service,
            ),
            prepare_workspace_text_write(
                self.workspace, second_path.name, "second-after\n",
                sha256("second-before\n"), self.path_service,
            ),
        )
        entries = []
        for index, item in enumerate(prepared, start=1):
            mutation_id = f"mutation-crash-{index}"
            backup_ref = write_mutation_backup(
                self.workspace, self.storage_key, mutation_id, item.before_bytes,
                self.filesystem, self.path_service,
            )
            entries.append(WorkspaceTransactionEntry(
                mutation_id=mutation_id, path=item.relative_path,
                before_hash=item.before_hash, after_hash=item.after_hash,
                backup_ref=backup_ref, before_mode=item.previous_mode,
            ))
        manifest = WorkspaceTransactionManifest(
            transaction_id="workspace-tx-crash", task_id=self.task.task_id,
            step_id="inv-crash", kind="apply_patches",
            created_at=datetime.now(timezone.utc), entries=tuple(entries),
        )
        manifest_path = write_workspace_transaction_manifest(
            self.workspace, self.storage_key, manifest, self.filesystem,
            self.path_service,
        )
        commit_prepared_workspace_mutation(prepared[0], self.filesystem)
        if apply_second:
            commit_prepared_workspace_mutation(prepared[1], self.filesystem)
        return first_path, second_path, prepared, manifest, manifest_path

    async def reopened(self):
        await self.first.registry.stop_all()
        second = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(self.database)
        )
        await second.registry.start_all()
        return second

    async def test_restart_restores_partially_applied_batch(self) -> None:
        first, second_path, _prepared, manifest, manifest_path = (
            self.crash_manifest()
        )
        application = await self.reopened()
        try:
            result = await application.kernel.recover_workspace_transactions(
                self.task.task_id
            )
            self.assertEqual(result, ({
                "transaction_id": manifest.transaction_id, "status": "restored",
                "entry_count": 2, "restored_files": 1,
            },))
            self.assertEqual(first.read_text(), "first-before\n")
            self.assertEqual(second_path.read_text(), "second-before\n")
            self.assertFalse(manifest_path.exists())
            events = await application.kernel.dependencies.store.read_events(
                self.task.task_id
            )
            self.assertEqual(events[-1].event_type, "workspace.transaction_recovered")
            self.assertEqual(events[-1].payload["status"], "restored")
        finally:
            await application.registry.stop_all()

    async def test_restart_restores_fully_applied_but_uncommitted_batch(self) -> None:
        first, second_path, _prepared, _manifest, manifest_path = (
            self.crash_manifest(apply_second=True)
        )
        application = await self.reopened()
        try:
            result = await application.kernel.recover_workspace_transactions(
                self.task.task_id
            )
            self.assertEqual(result[0]["restored_files"], 2)
            self.assertEqual(first.read_text(), "first-before\n")
            self.assertEqual(second_path.read_text(), "second-before\n")
            self.assertFalse(manifest_path.exists())
        finally:
            await application.registry.stop_all()

    async def test_committed_journal_only_cleans_manifest(self) -> None:
        first, second_path, prepared, manifest, manifest_path = self.crash_manifest()
        commit_prepared_workspace_mutation(prepared[1], self.filesystem)
        stored = await self.first.kernel._require_stored_task(self.task.task_id)
        task = await self.first.kernel.get_task(self.task.task_id)
        records = []
        for entry in manifest.entries:
            records.append(MutationRecord(
                mutation_id=entry.mutation_id, path=entry.path,
                operation=MutationOperation.MODIFY,
                before_hash=entry.before_hash, after_hash=entry.after_hash,
                step_id=manifest.step_id, backup_ref=entry.backup_ref,
                created_at=datetime.now(timezone.utc), before_mode=entry.before_mode,
            ))
        updated = task
        for record in records:
            updated = updated.with_mutation(record)
        from tsm_agt.ports import RuntimeEvent, RuntimeUnitOfWork
        events = tuple(
            RuntimeEvent(
                f"evt-committed-{index}", self.task.task_id,
                stored.last_event_sequence + index, "workspace.mutation_committed",
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
            self.assertEqual(result[0]["restored_files"], 0)
            self.assertEqual(first.read_text(), "first-after\n")
            self.assertEqual(second_path.read_text(), "second-after\n")
            self.assertFalse(manifest_path.exists())
        finally:
            await application.registry.stop_all()

    async def test_user_change_after_crash_blocks_recovery_and_keeps_manifest(self) -> None:
        first, second_path, _prepared, _manifest, manifest_path = (
            self.crash_manifest()
        )
        first.write_text("user-after-crash\n", encoding="utf-8")
        application = await self.reopened()
        try:
            with self.assertRaises(WorkspaceMutationConflict):
                await application.kernel.recover_workspace_transactions(
                    self.task.task_id
                )
            self.assertEqual(first.read_text(), "user-after-crash\n")
            self.assertEqual(second_path.read_text(), "second-before\n")
            self.assertTrue(manifest_path.exists())
        finally:
            await application.registry.stop_all()

    async def test_recovery_is_idempotent_after_success(self) -> None:
        first, _second, _prepared, _manifest, _manifest_path = self.crash_manifest()
        application = await self.reopened()
        try:
            first_result = await application.kernel.recover_workspace_transactions(
                self.task.task_id
            )
            second_result = await application.kernel.recover_workspace_transactions(
                self.task.task_id
            )
            self.assertEqual(first_result[0]["status"], "restored")
            self.assertEqual(second_result, ())
            self.assertEqual(first.read_text(), "first-before\n")
        finally:
            await application.registry.stop_all()

    async def test_next_workspace_mutation_recovers_old_transaction_first(self) -> None:
        first, second_path, _prepared, _manifest, manifest_path = (
            self.crash_manifest()
        )
        application = await self.reopened()
        try:
            record = await application.kernel.write_workspace_text(
                self.task.task_id, "step-after-recovery", "third.txt",
                "third\n", None,
            )
            self.assertEqual(record.path, "third.txt")
            self.assertEqual(first.read_text(), "first-before\n")
            self.assertEqual(second_path.read_text(), "second-before\n")
            self.assertEqual((self.workspace / "third.txt").read_text(), "third\n")
            self.assertFalse(manifest_path.exists())
            events = await application.kernel.dependencies.store.read_events(
                self.task.task_id
            )
            event_types = [event.event_type for event in events]
            self.assertLess(
                event_types.index("workspace.transaction_recovered"),
                event_types.index("workspace.mutation_committed"),
            )
        finally:
            await application.registry.stop_all()

    async def test_schema_v1_manifest_remains_readable_after_v2_upgrade(self) -> None:
        _first, _second, _prepared, manifest, manifest_path = self.crash_manifest()
        payload = manifest.to_data()
        payload["schema_version"] = 1
        for entry in payload["entries"]:
            entry.pop("mutation_ids", None)
        manifest_path.write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
        loaded = read_workspace_transaction_manifests(
            self.workspace, self.storage_key, self.path_service
        )
        self.assertEqual(len(loaded), 1)
        self.assertEqual(
            loaded[0][1].entries[0].effective_mutation_ids,
            (manifest.entries[0].mutation_id,),
        )


if __name__ == "__main__":
    unittest.main()
