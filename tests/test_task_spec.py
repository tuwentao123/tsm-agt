from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AcceptanceStatus, AgentTurnCheckpoint, ProjectTrustLevel, SteeringKind,
    TaskAcceptanceCriterion, TaskCriterionKind, TaskState,
)
from tsm_agt.ports import Message, MessageRole, TextBlock, ToolCall


async def move(application, task_id, states):
    task = await application.kernel.get_task(task_id)
    for state in states:
        task = await application.kernel.transition_task(task_id, state, state.value)
    return task


class TaskSpecKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_contains_authoritative_primary_workspace_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = EchoModelProvider()
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "inspect another repository if the user asks", root
                )
                task = await move(app, task.task_id, (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ))
                message = await app.kernel._task_spec_context_message(task.task_id)
                self.assertEqual(message.role, MessageRole.SYSTEM)
                self.assertIn(str(root.resolve()), message.text)
                self.assertIn("relative_path_base", message.text)
                self.assertIn("not evidence", message.text)
                self.assertIn("Task-scoped approval", message.text)
            finally:
                await app.registry.stop_all()

    async def _verification_for_persisted_answer(
        self, answer_content: list[dict[str, object]],
    ):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        app = compose_fixture_application(tool_adapters=())
        await app.registry.start_all()
        task = await app.kernel.create_task("verify final answer", root)
        task = await move(app, task.task_id, (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ))
        await app.kernel._append_events(task.task_id, (
            ("llm.completed", {
                "turn_id": "turn-answer",
                "message": {
                    "message_id": "assistant-answer",
                    "role": "assistant",
                    "content": answer_content,
                },
                "finish_reason": "stop",
            }),
            ("turn.completed", {"turn_id": "turn-answer"}),
        ))
        await app.kernel.transition_task(
            task.task_id, TaskState.VERIFYING, "verify"
        )
        return directory, app, await app.kernel.verify_task_acceptance(task.task_id)

    async def test_answer_completeness_accepts_user_facing_text(self):
        directory, app, verification = await self._verification_for_persisted_answer([
            {"type": "text", "text": "The requested analysis is complete."}
        ])
        try:
            criterion = next(
                item for item in verification.criteria
                if item.criterion_id == "answer-completeness"
            )
            self.assertEqual(criterion.status, AcceptanceStatus.PASSED)
            self.assertEqual(verification.status, AcceptanceStatus.PASSED)
        finally:
            await app.registry.stop_all()
            directory.cleanup()

    async def test_workspace_integrity_cannot_hide_protocol_as_final_answer(self):
        markup = (
            '<tool_use name="core__read_file" id="call-final">'
            '{"path":"screen.tsx"}</tool_use>'
        )
        directory, app, verification = await self._verification_for_persisted_answer([
            {"type": "text", "text": markup}
        ])
        try:
            by_id = {item.criterion_id: item for item in verification.criteria}
            self.assertEqual(
                by_id["answer-completeness"].status, AcceptanceStatus.FAILED
            )
            self.assertEqual(
                by_id["workspace-integrity"].status, AcceptanceStatus.PASSED
            )
            self.assertEqual(verification.status, AcceptanceStatus.FAILED)
        finally:
            await app.registry.stop_all()
            directory.cleanup()

    async def test_pending_structured_tool_call_is_not_a_final_answer(self):
        directory, app, verification = await self._verification_for_persisted_answer([
            {
                "type": "tool_call",
                "call": {
                    "call_id": "call-final", "name": "core.read_file",
                    "arguments": {"path": "screen.tsx"},
                },
            }
        ])
        try:
            criterion = next(
                item for item in verification.criteria
                if item.criterion_id == "answer-completeness"
            )
            self.assertEqual(criterion.status, AcceptanceStatus.FAILED)
            self.assertEqual(verification.status, AcceptanceStatus.FAILED)
        finally:
            await app.registry.stop_all()
            directory.cleanup()

    async def test_unverified_guess_after_failed_reads_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                tool_adapters=(CoreReadOnlyToolProvider(),)
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "determine behavior from source", root,
                    "task-unverified-answer",
                )
                task = await move(app, task.task_id, (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ))
                failed = await app.kernel.invoke_tool(
                    task.task_id, "turn-unverified", ToolCall(
                        "read-missing", "core.read_file",
                        {"path": "src/Missing.java"},
                    )
                )
                self.assertEqual(failed.error_code, "NOT_FOUND")
                await app.kernel._append_events(task.task_id, (
                    ("llm.completed", {
                        "turn_id": "turn-unverified",
                        "message": {
                            "message_id": "assistant-unverified",
                            "role": "assistant",
                            "content": [{
                                "type": "text",
                                "text": (
                                    "当前这轮我没法继续读取文件内容，所以不能"
                                    "百分百确认。不过大概率这个实现支持角色区分。"
                                ),
                            }],
                        },
                        "finish_reason": "stop",
                    }),
                    ("turn.completed", {"turn_id": "turn-unverified"}),
                ))
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify unverified answer"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                by_id = {item.criterion_id: item for item in verification.criteria}
                self.assertEqual(
                    by_id["answer-evidence-sufficiency"].status,
                    AcceptanceStatus.BLOCKED,
                )
                self.assertEqual(verification.status, AcceptanceStatus.BLOCKED)
            finally:
                await app.registry.stop_all()

    async def test_successful_read_does_not_trigger_unverified_answer_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.py").write_text("ROLE = 'driver'\n")
            app = compose_fixture_application(
                tool_adapters=(CoreReadOnlyToolProvider(),)
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "inspect source behavior", root,
                    "task-verified-answer",
                )
                task = await move(app, task.task_id, (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ))
                read = await app.kernel.invoke_tool(
                    task.task_id, "turn-verified", ToolCall(
                        "read-source", "core.read_file",
                        {"path": "source.py"},
                    )
                )
                self.assertTrue(read.ok)
                await app.kernel._append_events(task.task_id, (
                    ("llm.completed", {
                        "turn_id": "turn-verified",
                        "message": {
                            "message_id": "assistant-verified",
                            "role": "assistant",
                            "content": [{
                                "type": "text",
                                "text": "已读取源码；其他路径尚未验证。",
                            }],
                        },
                        "finish_reason": "stop",
                    }),
                    ("turn.completed", {"turn_id": "turn-verified"}),
                ))
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify successful read"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(verification.status, AcceptanceStatus.PASSED)
                self.assertNotIn(
                    "answer-evidence-sufficiency",
                    {item.criterion_id for item in verification.criteria},
                )
            finally:
                await app.registry.stop_all()

    async def test_each_task_has_private_runtime_spec_without_workspace_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await first.registry.start_all()
            try:
                one = await first.kernel.create_task("first goal", root, "task-one")
                two = await first.kernel.create_task("second goal", root, "task-two")
                self.assertEqual((await first.kernel.get_task_spec(one.task_id)).goal, "first goal")
                self.assertEqual((await first.kernel.get_task_spec(two.task_id)).goal, "second goal")
                self.assertFalse(any(root.glob("*spec*")))
            finally:
                await first.registry.stop_all()
            second = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await second.registry.start_all()
            try:
                self.assertEqual((await second.kernel.get_task_spec("task-one")).revision, 1)
            finally:
                await second.registry.stop_all()

    async def test_revision_cannot_change_goal_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("fixed goal", Path(directory))
                criterion = TaskAcceptanceCriterion(
                    "evidence", "inspection evidence exists",
                    TaskCriterionKind.EVIDENCE_REFERENCE, "event:1",
                )
                updated = await app.kernel.revise_task_spec(
                    task.task_id, 1, scope=("src",), constraints=("no API changes",),
                    acceptance_criteria=(criterion,), operation_id="spec-op",
                    writer="test",
                )
                replay = await app.kernel.revise_task_spec(
                    task.task_id, 1, scope=("src",), constraints=("no API changes",),
                    acceptance_criteria=(criterion,), operation_id="spec-op",
                    writer="test",
                )
                self.assertEqual(updated, replay)
                with self.assertRaisesRegex(ValueError, "only through Runtime Replace"):
                    await app.kernel.revise_task_spec(
                        task.task_id, 2, scope=(), constraints=(),
                        acceptance_criteria=(criterion,), operation_id="goal-change",
                        writer="test", goal="changed goal",
                    )
            finally:
                await app.registry.stop_all()

    async def test_verifier_blocks_missing_reference_and_passes_real_event(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("verify", Path(directory))
                task = await move(app, task.task_id, (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ))
                missing = TaskAcceptanceCriterion(
                    "proof", "proof exists", TaskCriterionKind.EVIDENCE_REFERENCE,
                    "event:999999",
                )
                await app.kernel.revise_task_spec(
                    task.task_id, 1, scope=(), constraints=(),
                    acceptance_criteria=(missing,), operation_id="missing", writer="test",
                )
                await app.kernel.transition_task(task.task_id, TaskState.VERIFYING, "verify")
                blocked = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(blocked.status, AcceptanceStatus.BLOCKED)
            finally:
                await app.registry.stop_all()

    async def test_steer_adds_constraint_and_replace_resets_goal(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("old goal", Path(directory))
                task = await move(app, task.task_id, (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
                ))
                checkpoint = AgentTurnCheckpoint(
                    task.task_id, "turn-spec", 1,
                    (Message("user-spec", MessageRole.USER, (TextBlock("start"),)),),
                    (), (), 0, 0, 0, 0, 5, 5, 256, 10.0,
                )
                checkpoint = await app.kernel._bind_agent_checkpoint(
                    task, checkpoint, await app.kernel.list_tools()
                )
                await app.kernel._save_agent_checkpoint(checkpoint, "test")
                await app.kernel.queue_steering(task.task_id, SteeringKind.STEER, "兼容 Windows", "steer-1")
                checkpoint, _ = await app.kernel._apply_pending_steering(checkpoint, "test")
                self.assertIn("兼容 Windows", (await app.kernel.get_task_spec(task.task_id)).constraints)
                await app.kernel.queue_steering(task.task_id, SteeringKind.REPLACE, "new goal", "replace-1")
                await app.kernel._apply_pending_steering(checkpoint, "test")
                spec = await app.kernel.get_task_spec(task.task_id)
                self.assertEqual(spec.goal, "new goal")
                self.assertFalse(spec.constraints)
            finally:
                await app.registry.stop_all()
