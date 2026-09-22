from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreProcessToolProvider, CoreReadOnlyToolProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    ApprovalDecision, ApprovalPayloadMismatch, ApprovalRequired,
    ProjectTrustLevel, TaskState,
)
from tsm_agt.ports import RuntimeStorePort, ToolCall


class ProjectTrustTest(unittest.IsolatedAsyncioTestCase):
    async def _executing_task(self, application, workspace: Path, task_id: str):
        task = await application.kernel.create_task(
            "project trust test", workspace, task_id
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, state, state.value
            )
        return task

    async def test_new_project_is_untrusted_read_still_works_command_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "README.md").write_text("safe data\n", encoding="utf-8")
            application = compose_fixture_application(tool_adapters=(
                CoreReadOnlyToolProvider(), CoreProcessToolProvider(),
            ))
            await application.registry.start_all()
            try:
                task = await self._executing_task(application, workspace, "task-untrusted")
                self.assertEqual(task.project_trust, ProjectTrustLevel.UNTRUSTED)
                read = await application.kernel.invoke_tool(
                    task.task_id, "turn-read",
                    ToolCall("call-read", "core.read_file", {"path": "README.md"}),
                )
                self.assertTrue(read.ok)
                denied = await application.kernel.invoke_tool(
                    task.task_id, "turn-command",
                    ToolCall("call-command", "core.run_command", {
                        "argv": ["python3", "-m", "pytest"]
                    }),
                )
                self.assertFalse(denied.ok)
                self.assertEqual(denied.error_code, "PERMISSION_DENIED")
                self.assertIn("TRUSTED_BUILD", denied.message)
                events = await application.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                self.assertNotIn("approval.requested", [event.event_type for event in events[-2:]])
            finally:
                await application.registry.stop_all()

    async def test_trusted_build_persists_in_sqlite_and_is_loaded_by_new_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            database = workspace / "runtime.db"
            first = compose_fixture_application(
                store_adapter=SQLiteRuntimeStore(database), tool_adapters=()
            )
            await first.registry.start_all()
            try:
                saved = await first.kernel.set_project_trust(
                    workspace, ProjectTrustLevel.TRUSTED_BUILD
                )
            finally:
                await first.registry.stop_all()
            second = compose_fixture_application(
                store_adapter=SQLiteRuntimeStore(database), tool_adapters=()
            )
            await second.registry.start_all()
            try:
                loaded = await second.kernel.get_project_trust(workspace)
                self.assertEqual(loaded, saved)
                task = await second.kernel.create_task(
                    "restored trust", workspace, "task-restored-trust"
                )
                self.assertEqual(task.project_trust, ProjectTrustLevel.TRUSTED_BUILD)
                self.assertEqual(task.project_fingerprint, saved.fingerprint)
            finally:
                await second.registry.stop_all()

    async def test_identity_file_change_invalidates_saved_trust(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / "pyproject.toml"
            config.write_text("[project]\nname='before'\n", encoding="utf-8")
            application = compose_fixture_application(tool_adapters=())
            await application.registry.start_all()
            try:
                saved = await application.kernel.set_project_trust(
                    workspace, ProjectTrustLevel.TRUSTED_BUILD
                )
                config.write_text("[project]\nname='after'\n", encoding="utf-8")
                effective = await application.kernel.get_project_trust(workspace)
                self.assertEqual(effective.level, ProjectTrustLevel.UNTRUSTED)
                self.assertNotEqual(effective.fingerprint, saved.fingerprint)
            finally:
                await application.registry.stop_all()

    async def test_identity_change_while_approval_pending_invalidates_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / "package.json"
            config.write_text('{"name":"before"}\n', encoding="utf-8")
            application = compose_fixture_application(
                tool_adapters=(CoreProcessToolProvider(),)
            )
            await application.registry.start_all()
            try:
                await application.kernel.set_project_trust(
                    workspace, ProjectTrustLevel.TRUSTED_BUILD
                )
                task = await self._executing_task(application, workspace, "task-stale-approval")
                with self.assertRaises(ApprovalRequired) as caught:
                    await application.kernel.invoke_tool(
                        task.task_id, "turn-test",
                        ToolCall("call-test", "core.run_command", {
                            "argv": ["npm", "test"]
                        }),
                    )
                request = caught.exception.request
                config.write_text('{"name":"after"}\n', encoding="utf-8")
                with self.assertRaisesRegex(
                    ApprovalPayloadMismatch, "project identity or trust changed"
                ):
                    await application.kernel.resolve_approval(
                        task.task_id, request.request_id, request.payload_hash,
                        ApprovalDecision.APPROVE, "stale project approval",
                    )
            finally:
                await application.registry.stop_all()

    async def test_trusted_full_routes_legacy_r4_command_to_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            application = compose_fixture_application(
                tool_adapters=(CoreProcessToolProvider(),)
            )
            await application.registry.start_all()
            try:
                await application.kernel.set_project_trust(
                    workspace, ProjectTrustLevel.TRUSTED_FULL
                )
                task = await self._executing_task(
                    application, workspace, "task-full-r3-push"
                )
                with self.assertRaises(ApprovalRequired) as caught:
                    await application.kernel.invoke_tool(
                        task.task_id, "turn-r3-push",
                        ToolCall("call-r3-push", "core.run_command", {
                            "argv": ["git", "push", "origin", "main"]
                        }),
                    )
                self.assertEqual(caught.exception.request.risk.value, "R3")
                self.assertEqual(
                    (await application.kernel.get_task(task.task_id)).state,
                    TaskState.AWAITING_APPROVAL,
                )
            finally:
                await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
