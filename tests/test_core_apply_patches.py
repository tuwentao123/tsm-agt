from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.builtin import CoreWorkspaceMutationToolProvider
from tsm_agt.adapters.fixture import InMemoryRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application, compose_readonly_application
from tsm_agt.core import ApprovalDecision, ApprovalRequired, TaskState
from tsm_agt.ports import ToolCall
from tsm_agt.core import kernel as kernel_module


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _FailBatchJournalStore(InMemoryRuntimeStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_batch = False

    async def commit(self, unit):
        if self.fail_batch and len(unit.events) > 1 and all(
            event.event_type == "workspace.mutation_committed"
            for event in unit.events
        ):
            self.fail_batch = False
            raise RuntimeError("simulated batch journal failure")
        return await super().commit(unit)


class CoreApplyPatchesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.store = _FailBatchJournalStore()
        self.application = compose_fixture_application(
            store_adapter=self.store,
            tool_adapters=(CoreWorkspaceMutationToolProvider(),),
        )
        await self.application.registry.start_all()
        task = await self.application.kernel.create_task(
            "patch several files", self.workspace, "task-apply-patches"
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await self.application.kernel.transition_task(
                task.task_id, state, state.value
            )
        self.task = task

    async def asyncTearDown(self) -> None:
        await self.application.registry.stop_all()
        self.temporary.cleanup()

    @staticmethod
    def call(call_id: str, patches: list[dict]) -> ToolCall:
        return ToolCall(call_id, "core.apply_patches", {"patches": patches})

    async def approve(self, call: ToolCall, turn_id: str):
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, turn_id, call
            )
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve exact multi-file patch",
        )
        return request, result

    def two_modifications(self) -> tuple[Path, Path, list[dict]]:
        first = self.workspace / "first.txt"
        second = self.workspace / "second.txt"
        first.write_text("first-old\n", encoding="utf-8")
        second.write_text("second-old\n", encoding="utf-8")
        return first, second, [
            {
                "path": first.name, "expected_hash": sha256("first-old\n"),
                "edits": [{"old_text": "old", "new_text": "new"}],
            },
            {
                "path": second.name, "expected_hash": sha256("second-old\n"),
                "edits": [{"old_text": "old", "new_text": "new"}],
            },
        ]

    async def test_approval_precedes_batch_and_journal_commits_together(self) -> None:
        first, second, patches = self.two_modifications()
        call = self.call("call-batch", patches)
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-batch", call
            )
        self.assertEqual(first.read_text(), "first-old\n")
        self.assertEqual(second.read_text(), "second-old\n")
        request = caught.exception.request
        self.assertEqual(request.target, "paths=['first.txt', 'second.txt']")
        self.assertIn("完整补丁", request.preview)
        self.assertIn("first.txt", request.preview)
        self.assertIn("old", request.preview)

        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "reviewed both exact patches",
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["patched"], 2)
        self.assertEqual(first.read_text(), "first-new\n")
        self.assertEqual(second.read_text(), "second-new\n")
        journal = (await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal
        self.assertEqual(len(journal), 2)
        self.assertTrue(all(item.step_id == request.invocation_id for item in journal))
        events = await self.store.read_events(self.task.task_id)
        mutation_events = [
            event for event in events
            if event.event_type == "workspace.mutation_committed"
        ]
        self.assertEqual(len(mutation_events), 2)
        self.assertEqual(
            [event.sequence for event in mutation_events],
            list(range(mutation_events[0].sequence, mutation_events[0].sequence + 2)),
        )

    async def test_large_preview_is_compact_and_denial_writes_nothing(self) -> None:
        paths = []
        patches = []
        for index in range(5):
            path = self.workspace / f"file-{index}.txt"
            old = f"OLD_UNIQUE_{index}\n" * 10
            path.write_text(old, encoding="utf-8")
            paths.append(path)
            patches.append({
                "path": path.name, "expected_hash": sha256(old),
                "edits": [{
                    "old_text": old, "new_text": f"NEW_UNIQUE_{index}\n" * 10,
                }],
            })
        call = self.call("call-large-preview", patches)
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-large-preview", call
            )
        request = caught.exception.request
        self.assertIn("改动清单", request.preview)
        self.assertIn("代表性改动示例（3/5）", request.preview)
        self.assertNotIn("OLD_UNIQUE_4", request.preview)
        self.assertEqual(request.call.arguments, call.arguments)

        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.DENY, "do not apply the full patch",
        )
        self.assertFalse(result.ok)
        for index, path in enumerate(paths):
            self.assertEqual(path.read_text(encoding="utf-8"), f"OLD_UNIQUE_{index}\n" * 10)
        self.assertEqual(
            (await self.application.kernel.get_task(self.task.task_id)).mutation_journal,
            (),
        )

    async def test_stale_second_file_rejects_batch_before_first_write(self) -> None:
        first, second, patches = self.two_modifications()
        call = self.call("call-stale-batch", patches)
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-stale-batch", call
            )
        second.write_text("user-change\n", encoding="utf-8")
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve stale batch",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CONFLICT")
        self.assertEqual(first.read_text(), "first-old\n")
        self.assertEqual(second.read_text(), "user-change\n")
        self.assertEqual((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal, ())

    async def test_duplicate_canonical_path_is_rejected_without_write(self) -> None:
        target = self.workspace / "same.txt"
        target.write_text("old\n", encoding="utf-8")
        patches = [
            {
                "path": "same.txt", "expected_hash": sha256("old\n"),
                "edits": [{"old_text": "old", "new_text": "one"}],
            },
            {
                "path": "./same.txt", "expected_hash": sha256("old\n"),
                "edits": [{"old_text": "old", "new_text": "two"}],
            },
        ]
        _request, result = await self.approve(
            self.call("call-duplicate-path", patches), "turn-duplicate-path"
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")
        self.assertEqual(target.read_text(), "old\n")

    async def test_modify_and_create_can_commit_in_one_batch(self) -> None:
        existing = self.workspace / "existing.txt"
        existing.write_text("before\n", encoding="utf-8")
        patches = [
            {
                "path": existing.name, "expected_hash": sha256("before\n"),
                "edits": [{"old_text": "before", "new_text": "after"}],
            },
            {
                "path": "created.txt", "expected_hash": None,
                "edits": [{"old_text": "", "new_text": "created\n"}],
            },
        ]
        _request, result = await self.approve(
            self.call("call-mixed-batch", patches), "turn-mixed-batch"
        )
        self.assertTrue(result.ok)
        self.assertEqual(existing.read_text(), "after\n")
        self.assertEqual((self.workspace / "created.txt").read_text(), "created\n")
        self.assertEqual(
            [item["operation"] for item in result.data["mutations"]],
            ["modify", "create"],
        )

    async def test_journal_failure_restores_every_written_file(self) -> None:
        first, second, patches = self.two_modifications()
        self.store.fail_batch = True
        _request, result = await self.approve(
            self.call("call-store-failure", patches), "turn-store-failure"
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TOOL_FAILED")
        self.assertIn("simulated batch journal failure", result.message)
        self.assertEqual(first.read_text(), "first-old\n")
        self.assertEqual(second.read_text(), "second-old\n")
        self.assertEqual((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal, ())

    async def test_error_after_second_replace_still_restores_both_files(self) -> None:
        first, second, patches = self.two_modifications()
        original_commit = kernel_module.commit_prepared_workspace_mutation
        calls = 0

        def fail_after_replace(prepared, filesystem):
            nonlocal calls
            calls += 1
            original_commit(prepared, filesystem)
            if calls == 2:
                raise OSError("simulated failure after atomic replace")

        with patch.object(
            kernel_module, "commit_prepared_workspace_mutation",
            side_effect=fail_after_replace,
        ):
            _request, result = await self.approve(
                self.call("call-post-replace-failure", patches),
                "turn-post-replace-failure",
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TOOL_FAILED")
        self.assertEqual(first.read_text(), "first-old\n")
        self.assertEqual(second.read_text(), "second-old\n")
        self.assertEqual((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal, ())

    async def test_compensation_conflict_is_reported_and_preserves_external_write(self) -> None:
        first, second, patches = self.two_modifications()
        original_commit = kernel_module.commit_prepared_workspace_mutation
        calls = 0

        def external_change_after_replace(prepared, filesystem):
            nonlocal calls
            calls += 1
            original_commit(prepared, filesystem)
            if calls == 2:
                prepared.path.write_text("external\n", encoding="utf-8")
                raise OSError("simulated external change after replace")

        with patch.object(
            kernel_module, "commit_prepared_workspace_mutation",
            side_effect=external_change_after_replace,
        ):
            _request, result = await self.approve(
                self.call("call-compensation-conflict", patches),
                "turn-compensation-conflict",
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TOOL_FAILED")
        self.assertIn("could not be proven safe", result.message)
        self.assertEqual(first.read_text(), "first-old\n")
        self.assertEqual(second.read_text(), "external\n")
        self.assertEqual((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal, ())

    async def test_completed_call_reuses_result_without_second_patch(self) -> None:
        first, _second, patches = self.two_modifications()
        call = self.call("call-reuse-batch", patches)
        _request, first_result = await self.approve(call, "turn-reuse-batch")
        second_result = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-reuse-batch", call
        )
        self.assertEqual(second_result, first_result)
        self.assertEqual(first.read_text(), "first-new\n")
        self.assertEqual(len((await self.application.kernel.get_task(
            self.task.task_id
        )).mutation_journal), 2)

    async def test_readonly_composition_does_not_expose_batch_patch(self) -> None:
        application = compose_readonly_application()
        await application.registry.start_all()
        try:
            names = [tool.name for tool in await application.kernel.list_tools()]
            self.assertNotIn("core.apply_patches", names)
        finally:
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
