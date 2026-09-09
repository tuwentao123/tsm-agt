from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AgentTurnResult, AgentTurnSuspended, ApprovalDecision, ApprovalKind,
    ApprovalRequired, TaskState, WorkspaceAccessCapability,
)
from tsm_agt.ports import (
    FinishReason, Message, MessageRole, ModelRequest, ModelResponse, ModelUsage,
    ProviderCapabilities, TextBlock, ToolCall, ToolCallBlock, ToolResultBlock,
)


class ExternalReadModel(EchoModelProvider):
    """Ask for one external file, then finish from the resumed result."""

    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    async def complete(self, request: ModelRequest) -> ModelResponse:
        result = next((
            block.result
            for message in reversed(request.messages)
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ), None)
        if result is None:
            return ModelResponse(
                Message(
                    "assistant-external-read", MessageRole.ASSISTANT,
                    (ToolCallBlock(ToolCall(
                        "call-external-read", "core.read_file",
                        {"path": str(self.path)},
                    )),),
                ),
                FinishReason.TOOL_CALL, ModelUsage(2, 1),
            )
        text = result.data["content"].strip() if result.ok else result.error_code
        return ModelResponse(
            Message(
                "assistant-external-final", MessageRole.ASSISTANT,
                (TextBlock(f"External result: {text}"),),
            ),
            FinishReason.STOP, ModelUsage(3, 2),
        )


class WorkspaceReadApprovalTest(unittest.IsolatedAsyncioTestCase):
    async def _executing_task(
        self, application, workspace: Path,
        task_id: str = "task-external-read",
    ):
        task = await application.kernel.create_task(
            "read the related project", workspace, task_id
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, state, state.value
            )
        return task

    async def test_kernel_approval_adds_task_read_grant_and_resumes_call(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        workspace = root / "workspace"
        external = root / "related-project"
        workspace.mkdir()
        external.mkdir()
        target = external / "service.py"
        target.write_text("external evidence\n", encoding="utf-8")
        application = compose_fixture_application(
            tool_adapters=(CoreReadOnlyToolProvider(),)
        )
        await application.registry.start_all()
        try:
            task = await self._executing_task(application, workspace)
            with self.assertRaises(ApprovalRequired) as caught:
                await application.kernel.invoke_tool(
                    task.task_id, "turn-1", ToolCall(
                        "call-1", "core.read_file", {"path": str(target)}
                    )
                )
            request = caught.exception.request
            self.assertEqual(request.kind, ApprovalKind.WORKSPACE_READ)
            self.assertEqual(request.workspace_access_root, str(external.resolve()))
            self.assertEqual((await application.kernel.get_task(task.task_id)).tool_executions, {})

            result = await application.kernel.resolve_approval(
                task.task_id, request.request_id, request.payload_hash,
                ApprovalDecision.APPROVE, "allow related project read",
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.data["content"], "external evidence\n")
            resumed = await application.kernel.get_task(task.task_id)
            self.assertEqual(len(resumed.workspace_access_grants), 1)
            self.assertEqual(
                resumed.workspace_access_grants[0].capability,
                WorkspaceAccessCapability.READ,
            )
            second = await application.kernel.invoke_tool(
                task.task_id, "turn-1", ToolCall(
                    "call-2", "core.search_text",
                    {"path": str(external), "query": "evidence"},
                )
            )
            self.assertTrue(second.ok)
        finally:
            await application.registry.stop_all()
            temporary.cleanup()

    async def test_agent_turn_suspends_and_resumes_after_sqlite_restart(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        workspace = root / "workspace"
        external = root / "related-project"
        workspace.mkdir()
        external.mkdir()
        target = external / "service.py"
        target.write_text("persisted evidence\n", encoding="utf-8")
        database = root / "runtime.db"
        first = compose_fixture_application(
            model_adapter=ExternalReadModel(target),
            tool_adapters=(CoreReadOnlyToolProvider(),),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await first.registry.start_all()
        task = await self._executing_task(first, workspace)
        suspended = await first.kernel.run_agent_turn(
            task.task_id, "inspect the related project"
        )
        self.assertIsInstance(suspended, AgentTurnSuspended)
        assert isinstance(suspended, AgentTurnSuspended)
        self.assertEqual(suspended.approval_kind, "workspace_read")
        await first.registry.stop_all()

        second = compose_fixture_application(
            model_adapter=ExternalReadModel(target),
            tool_adapters=(CoreReadOnlyToolProvider(),),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            completed = await second.kernel.resume_agent_turn(
                task.task_id, suspended.approval_request_id,
                suspended.payload_hash, ApprovalDecision.APPROVE,
                "approved after restart",
            )
            self.assertIsInstance(completed, AgentTurnResult)
            assert isinstance(completed, AgentTurnResult)
            self.assertEqual(
                completed.assistant_message.text,
                "External result: persisted evidence",
            )
            restored = await second.kernel.get_task(task.task_id)
            self.assertEqual(
                restored.workspace_access_grants[0].canonical_root,
                str(external.resolve()),
            )
        finally:
            await second.registry.stop_all()
            temporary.cleanup()

    async def test_denial_does_not_create_grant(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        workspace = root / "workspace"
        external = root / "external"
        workspace.mkdir()
        external.mkdir()
        target = external / "source.txt"
        target.write_text("private source\n", encoding="utf-8")
        application = compose_fixture_application(
            tool_adapters=(CoreReadOnlyToolProvider(),)
        )
        await application.registry.start_all()
        try:
            task = await self._executing_task(application, workspace)
            with self.assertRaises(ApprovalRequired) as caught:
                await application.kernel.invoke_tool(
                    task.task_id, "turn-denied", ToolCall(
                        "call-denied", "core.read_file",
                        {"path": str(target)},
                    )
                )
            request = caught.exception.request
            denied = await application.kernel.resolve_approval(
                task.task_id, request.request_id, request.payload_hash,
                ApprovalDecision.DENY, "do not expose related project",
            )
            self.assertFalse(denied.ok)
            self.assertEqual(denied.error_code, "PERMISSION_DENIED")
            restored = await application.kernel.get_task(task.task_id)
            self.assertEqual(restored.workspace_access_grants, ())
        finally:
            await application.registry.stop_all()
            temporary.cleanup()

    async def test_discovered_external_resource_can_be_read_by_reference(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        workspace = root / "workspace"
        external = root / "related-project"
        workspace.mkdir()
        external.mkdir()
        target = external / "src" / "service.py"
        target.parent.mkdir()
        target.write_text("class ExternalService:\n    pass\n", encoding="utf-8")
        application = compose_fixture_application(
            tool_adapters=(CoreReadOnlyToolProvider(),)
        )
        await application.registry.start_all()
        try:
            task = await self._executing_task(application, workspace)
            find_call = ToolCall(
                "find-resource", "core.find_files",
                {"path": str(external), "pattern": "service.py"},
            )
            with self.assertRaises(ApprovalRequired) as caught:
                await application.kernel.invoke_tool(
                    task.task_id, "turn-resource", find_call
                )
            request = caught.exception.request
            found = await application.kernel.resolve_approval(
                task.task_id, request.request_id, request.payload_hash,
                ApprovalDecision.APPROVE, "allow resource discovery",
            )
            match = found.data["matches"][0]
            self.assertEqual(match["path"], "src/service.py")
            self.assertEqual(match["resolved_path"], str(target.resolve()))
            self.assertEqual(match["resolved_root"], str(external.resolve()))
            self.assertEqual(match["root_kind"], "TASK_APPROVED_READ_ROOT")
            self.assertTrue(match["resource_ref"].startswith("resource-"))

            read = await application.kernel.invoke_tool(
                task.task_id, "turn-resource", ToolCall(
                    "read-resource", "core.read_file",
                    {"resource_ref": match["resource_ref"]},
                )
            )
            self.assertTrue(read.ok)
            self.assertIn("ExternalService", read.data["content"])
            self.assertEqual(read.data["resolved_path"], str(target.resolve()))

            misplaced = await application.kernel.invoke_tool(
                task.task_id, "turn-resource", ToolCall(
                    "read-relative", "core.read_file",
                    {"path": "src/service.py"},
                )
            )
            self.assertFalse(misplaced.ok)
            self.assertEqual(misplaced.error_code, "PATH_CONTEXT_REQUIRED")
            self.assertTrue(misplaced.retryable)
            self.assertEqual(
                misplaced.data["candidates"][0]["resource_ref"],
                match["resource_ref"],
            )
        finally:
            await application.registry.stop_all()
            temporary.cleanup()

    async def test_resource_reference_survives_sqlite_restart_and_is_task_local(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        workspace = root / "workspace"
        external = root / "related-project"
        workspace.mkdir()
        external.mkdir()
        target = external / "service.py"
        target.write_text("persisted resource\n", encoding="utf-8")
        database = root / "runtime.db"
        first = compose_fixture_application(
            tool_adapters=(CoreReadOnlyToolProvider(),),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await first.registry.start_all()
        task = await self._executing_task(first, workspace)
        with self.assertRaises(ApprovalRequired) as caught:
            await first.kernel.invoke_tool(
                task.task_id, "turn-persist", ToolCall(
                    "find-persist", "core.find_files",
                    {"path": str(external), "pattern": "service.py"},
                )
            )
        request = caught.exception.request
        found = await first.kernel.resolve_approval(
            task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "allow persisted discovery",
        )
        resource_ref = found.data["matches"][0]["resource_ref"]
        await first.registry.stop_all()

        second = compose_fixture_application(
            tool_adapters=(CoreReadOnlyToolProvider(),),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            read = await second.kernel.invoke_tool(
                task.task_id, "turn-after-restart", ToolCall(
                    "read-after-restart", "core.read_file",
                    {"resource_ref": resource_ref},
                )
            )
            self.assertTrue(read.ok)
            self.assertEqual(read.data["content"], "persisted resource\n")

            other = await self._executing_task(
                second, workspace, "task-other-resource"
            )
            unknown = await second.kernel.invoke_tool(
                other.task_id, "turn-other", ToolCall(
                    "read-other", "core.read_file",
                    {"resource_ref": resource_ref},
                )
            )
            self.assertFalse(unknown.ok)
            self.assertEqual(unknown.error_code, "INVALID_PARAM")
        finally:
            await second.registry.stop_all()
            temporary.cleanup()

    async def test_external_sensitive_file_is_denied_without_approval(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        workspace = root / "workspace"
        external = root / "external"
        workspace.mkdir()
        external.mkdir()
        secret = external / ".env"
        secret.write_text("TOKEN=secret\n", encoding="utf-8")
        application = compose_fixture_application(
            tool_adapters=(CoreReadOnlyToolProvider(),)
        )
        await application.registry.start_all()
        try:
            task = await self._executing_task(application, workspace)
            result = await application.kernel.invoke_tool(
                task.task_id, "turn-secret", ToolCall(
                    "call-secret", "core.read_file", {"path": str(secret)}
                )
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "PERMISSION_DENIED")
            restored = await application.kernel.get_task(task.task_id)
            self.assertIsNone(restored.pending_approval)
            self.assertEqual(restored.workspace_access_grants, ())
        finally:
            await application.registry.stop_all()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
