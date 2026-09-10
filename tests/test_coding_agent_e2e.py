from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import (
    CoreProcessToolProvider, CoreReadOnlyToolProvider,
    CoreWorkspaceMutationToolProvider,
)
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.local_process import LocalProcessExecutor
from tsm_agt.adapters.local_sandbox import LocalWorkspaceSandbox
from tsm_agt.adapters.posix_path import PosixWorkspacePath
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AcceptanceStatus, AgentTurnResult, AgentTurnSuspended, ApprovalDecision,
    ProjectTrustLevel, TaskOutcomeStatus, TaskSpecProposal, TaskSpecSnapshot,
    TaskState,
)
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ModelUsage, ProviderCapabilities, RuntimeStorePort, TextBlock,
    ToolCall, ToolCallBlock, ToolResultBlock,
)


class CodingLoopModel(EchoModelProvider):
    descriptor = AdapterDescriptor(
        "fixture.coding-loop", "1.0", "ModelProviderPort", "1.0",
        frozenset({"text", "tools"}),
    )
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    def __init__(
        self, *, block_after_patch: bool = False, bind_outcomes: bool = False,
    ) -> None:
        super().__init__()
        self.block_after_patch = block_after_patch
        self.bind_outcomes = bind_outcomes
        self.blocked = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        tool_results = [
            block.result for message in request.messages
            for block in message.content if isinstance(block, ToolResultBlock)
        ]
        if not tool_results:
            return self._tool_response(
                request, "read", "core.read_file", {"path": "calc.py"}
            )
        if len(tool_results) == 1:
            read = tool_results[-1]
            return self._tool_response(request, "patch", "core.apply_patch", {
                "path": "calc.py", "expected_hash": read.data["sha256"],
                "edits": [{
                    "old_text": "return left - right",
                    "new_text": "return left + right",
                }],
            })
        if len(tool_results) == 2:
            if self.block_after_patch:
                self.blocked.set()
                await asyncio.Event().wait()
            return self._tool_response(request, "test", "core.run_command", {
                "argv": [
                    __import__("sys").executable, "-m", "unittest",
                    "discover", "-s", "tests", "-v",
                ],
                "mode": "foreground", "timeout_seconds": 30,
            })
        command = tool_results[-1]
        text = (
            "Fixed calc.py and verified the test suite."
            if command.ok and command.data["exit_code"] == 0
            else "Verification failed."
        )
        return ModelResponse(
            Message(
                f"final-{request.turn_id}", MessageRole.ASSISTANT,
                (TextBlock(text),),
            ), FinishReason.STOP, ModelUsage(4, 4),
        )

    def _tool_response(
        self, request: ModelRequest, suffix: str, name: str, arguments: dict,
    ) -> ModelResponse:
        outcome_ref = (
            {"read": "inspect", "patch": "change", "test": "verify"}.get(
                suffix
            )
            if self.bind_outcomes else None
        )
        return ModelResponse(
            Message(
                f"assistant-{suffix}-{request.turn_id}", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    f"call-{suffix}-{request.turn_id}", name, arguments,
                    outcome_ref=outcome_ref,
                )),),
            ), FinishReason.TOOL_CALL, ModelUsage(3, 2),
        )


class CodingAgentEndToEndTest(unittest.IsolatedAsyncioTestCase):
    def _compose(
        self, root: Path, database: Path, model: CodingLoopModel,
    ):
        workspace_path = PosixWorkspacePath()
        return compose_fixture_application(
            model_adapter=model,
            tool_adapters=(
                CoreReadOnlyToolProvider(), CoreWorkspaceMutationToolProvider(),
                CoreProcessToolProvider(),
            ),
            store_adapter=SQLiteRuntimeStore(database),
            process_adapter=LocalProcessExecutor(),
            sandbox_adapter=LocalWorkspaceSandbox(workspace_path),
            workspace_path_adapter=workspace_path,
        )

    async def _create_executing_task(self, application, root: Path):
        await application.kernel.set_project_trust(
            root, ProjectTrustLevel.TRUSTED_BUILD
        )
        task = await application.kernel.create_task(
            "Fix add() and verify its tests", root, "task-coding-e2e"
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

    async def test_real_files_process_approvals_interrupt_resume_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "calc.py").write_text(
                "def add(left, right):\n    return left - right\n", encoding="utf-8"
            )
            (root / "tests" / "test_calc.py").write_text(
                "import unittest\nfrom calc import add\n\n"
                "class CalcTest(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
                encoding="utf-8",
            )
            database = root / "runtime.db"
            first_model = CodingLoopModel(
                block_after_patch=True, bind_outcomes=True
            )
            first = self._compose(root, database, first_model)
            await first.registry.start_all()
            task = await self._create_executing_task(first, root)
            initial_spec = await first.kernel.get_task_spec(task.task_id)
            proposal = TaskSpecProposal.from_data({
                "schema_version": 1,
                "goal": task.goal,
                "scope": ["calc.py", "tests"],
                "constraints": ["Use Runtime-authorized tools only"],
                "outcomes": [
                    {
                        "outcome_id": "inspect",
                        "description": "Inspect the existing implementation",
                        "kind": "EVIDENCE",
                        "required_effects": ["observe"],
                        "required": True,
                    },
                    {
                        "outcome_id": "change",
                        "description": "Deliver the requested workspace fix",
                        "kind": "WORKSPACE_DELIVERY",
                        "required_effects": ["mutate"],
                        "required": True,
                    },
                    {
                        "outcome_id": "verify",
                        "description": "Run relevant verification",
                        "kind": "COMMAND_RESULT",
                        "required_effects": ["execute"],
                        "required": True,
                    },
                ],
                "continuation_policy": {"mode": "NONE"},
            })
            runtime_spec = TaskSpecSnapshot.from_proposal(
                task.task_id, initial_spec.revision + 1, proposal,
                initial_spec.acceptance_criteria,
            )
            await first.kernel._append_events(task.task_id, ((
                "task_spec.revised", {"snapshot": runtime_spec.to_data()},
            ),))

            patch_wait = await first.kernel.run_agent_turn(task.task_id, task.goal)
            self.assertIsInstance(patch_wait, AgentTurnSuspended)
            assert isinstance(patch_wait, AgentTurnSuspended)
            self.assertEqual(
                (root / "calc.py").read_text(encoding="utf-8"),
                "def add(left, right):\n    return left - right\n",
            )
            resolving = asyncio.create_task(first.kernel.resolve_agent_approval(
                patch_wait.approval_request_id, ApprovalDecision.APPROVE,
                "approve exact bug fix",
            ))
            await first_model.blocked.wait()
            resolving.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await resolving
            interrupted = await first.kernel.interrupt_agent_turn(
                task.task_id, "end-to-end restart checkpoint"
            )
            self.assertEqual(interrupted.state, TaskState.INTERRUPTED)
            self.assertIn(
                "return left + right",
                (root / "calc.py").read_text(encoding="utf-8"),
            )
            await first.registry.stop_all()

            second = self._compose(
                root, database, CodingLoopModel(bind_outcomes=True)
            )
            await second.registry.start_all()
            try:
                command_wait = await second.kernel.resume_checkpointed_agent_turn(
                    task.task_id
                )
                self.assertIsInstance(command_wait, AgentTurnSuspended)
                assert isinstance(command_wait, AgentTurnSuspended)
                completed = await second.kernel.resolve_agent_approval(
                    command_wait.approval_request_id, ApprovalDecision.APPROVE,
                    "approve exact local unittest command",
                )
                self.assertIsInstance(completed, AgentTurnResult)
                assert isinstance(completed, AgentTurnResult)
                self.assertIn("verified", completed.assistant_message.text)

                await second.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "run trusted verifier"
                )
                verification = await second.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(verification.status, AcceptanceStatus.PASSED)
                outcomes = await second.kernel.get_task_spec(task.task_id)
                self.assertEqual(
                    {item.outcome_id: item.status for item in outcomes.outcomes},
                    {
                        "inspect": TaskOutcomeStatus.DELIVERED,
                        "change": TaskOutcomeStatus.DELIVERED,
                        "verify": TaskOutcomeStatus.DELIVERED,
                    },
                )
                outcome_criterion = next(
                    item for item in verification.criteria
                    if item.criterion_id == "task-outcome-fulfillment"
                )
                self.assertEqual(outcome_criterion.status, AcceptanceStatus.PASSED)
                await second.kernel.transition_task(
                    task.task_id, TaskState.FINALIZING, "verified"
                )
                final = await second.kernel.transition_task(
                    task.task_id, TaskState.SUCCEEDED, "all evidence passed"
                )
                self.assertEqual(final.state, TaskState.SUCCEEDED)
                self.assertEqual(len(final.mutation_journal), 1)

                # The Session handoff is rebuilt from SQLite, not from the
                # in-memory Agent loop. It must retain the real changed file,
                # trusted verification result, and terminal Task state.
                conversation = await second.kernel.get_session_conversation(
                    task.session_id
                )
                summary = next(
                    item for item in conversation.task_summaries
                    if item.task_id == task.task_id
                )
                self.assertEqual(summary.recorded_task_state, "SUCCEEDED")
                self.assertEqual(summary.verification_status, "passed")
                self.assertEqual(
                    [item["path"] for item in summary.mutations], ["calc.py"]
                )

                # Push the completed coding Task outside the recent-message
                # window. Its engineering handoff must then move into the
                # structured earlier summary instead of degrading to clipped
                # chat text.
                for index in range(6):
                    follow_up = await second.kernel.create_task(
                        f"follow-up {index}", root,
                        task_id=f"task-follow-up-{index}",
                        session_id=task.session_id,
                    )
                    await second.kernel._record_session_task_result(
                        follow_up.task_id, f"turn-follow-up-{index}",
                        Message(
                            f"user-follow-up-{index}", MessageRole.USER,
                            (TextBlock(f"question {index}"),),
                        ),
                        Message(
                            f"assistant-follow-up-{index}",
                            MessageRole.ASSISTANT,
                            (TextBlock(f"answer {index}"),),
                        ),
                    )
                compacted = await second.kernel.get_session_conversation(
                    task.session_id
                )
                prompt = (
                    second.kernel.dependencies.session_context_projector.for_prompt(
                        compacted
                    )
                )
                assert prompt.message is not None
                prompt_body = json.loads(prompt.message.text)
                historical = next(
                    item for item in prompt_body["earlier_summary"]["tasks"]
                    if item["task_id"] == task.task_id
                )
                self.assertEqual(historical["status"], "SUCCEEDED")
                self.assertEqual(historical["verification_status"], "passed")
                self.assertEqual(
                    [item["path"] for item in historical["mutations"]],
                    ["calc.py"],
                )

                events = await second.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                event_types = [event.event_type for event in events]
                for expected in (
                    "workspace.mutation_committed", "turn.interrupted",
                    "turn.resumed", "process.exited", "verify.started",
                    "verify.criterion_completed", "verify.completed",
                ):
                    self.assertIn(expected, event_types)
                self.assertEqual(
                    event_types.count("workspace.mutation_committed"), 1
                )
                projection = await second.kernel.get_flow_projection(task.task_id)
                flow_task = next(
                    node for node in projection.nodes if node.kind.value == "task"
                )
                self.assertEqual(flow_task.status.value, "SUCCEEDED")
            finally:
                await second.registry.stop_all()

    async def test_verifier_blocks_mutation_without_post_change_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calc.py").write_text("value = 1\n", encoding="utf-8")
            app = self._compose(root, root / "runtime.db", CodingLoopModel())
            await app.registry.start_all()
            try:
                task = await self._create_executing_task(app, root)
                call = ToolCall("manual-patch", "core.apply_patch", {
                    "path": "calc.py",
                    "expected_hash": __import__("hashlib").sha256(
                        b"value = 1\n"
                    ).hexdigest(),
                    "edits": [{"old_text": "1", "new_text": "2"}],
                })
                from tsm_agt.core import ApprovalRequired
                with self.assertRaises(ApprovalRequired) as caught:
                    await app.kernel.invoke_tool(task.task_id, "manual-turn", call)
                request = caught.exception.request
                await app.kernel.resolve_approval(
                    task.task_id, request.request_id, request.payload_hash,
                    ApprovalDecision.APPROVE, "approve exact fixture mutation",
                )
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify missing command"
                )
                result = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(result.status, AcceptanceStatus.BLOCKED)
                self.assertFalse(result.passed)
            finally:
                await app.registry.stop_all()

    async def test_verifier_rejects_unrelated_successful_command_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calc.py").write_text("value = 1\n", encoding="utf-8")
            app = self._compose(root, root / "runtime.db", CodingLoopModel())
            await app.registry.start_all()
            try:
                task = await self._create_executing_task(app, root)
                from tsm_agt.core import ApprovalRequired
                patch = ToolCall("unrelated-patch", "core.apply_patch", {
                    "path": "calc.py",
                    "expected_hash": __import__("hashlib").sha256(
                        b"value = 1\n"
                    ).hexdigest(),
                    "edits": [{"old_text": "1", "new_text": "2"}],
                })
                with self.assertRaises(ApprovalRequired) as patch_approval:
                    await app.kernel.invoke_tool(task.task_id, "unrelated-turn", patch)
                request = patch_approval.exception.request
                await app.kernel.resolve_approval(
                    task.task_id, request.request_id, request.payload_hash,
                    ApprovalDecision.APPROVE, "approve fixture patch",
                )
                command = ToolCall("unrelated-command", "core.run_command", {
                    "argv": ["git", "status"], "mode": "foreground",
                })
                with self.assertRaises(ApprovalRequired) as command_approval:
                    await app.kernel.invoke_tool(
                        task.task_id, "unrelated-turn", command
                    )
                command_request = command_approval.exception.request
                await app.kernel.resolve_approval(
                    task.task_id, command_request.request_id,
                    command_request.payload_hash, ApprovalDecision.APPROVE,
                    "approve unrelated status command",
                )
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify unrelated command"
                )
                result = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(result.status, AcceptanceStatus.BLOCKED)
            finally:
                await app.registry.stop_all()

    async def test_verifier_uses_latest_post_mutation_command_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calc.py").write_text("value = 1\n", encoding="utf-8")
            app = self._compose(root, root / "runtime.db", CodingLoopModel())
            await app.registry.start_all()
            try:
                task = await self._create_executing_task(app, root)
                from tsm_agt.core import ApprovalRequired
                patch = ToolCall("latest-patch", "core.apply_patch", {
                    "path": "calc.py",
                    "expected_hash": __import__("hashlib").sha256(
                        b"value = 1\n"
                    ).hexdigest(),
                    "edits": [{"old_text": "1", "new_text": "2"}],
                })
                with self.assertRaises(ApprovalRequired) as caught:
                    await app.kernel.invoke_tool(task.task_id, "latest-turn", patch)
                request = caught.exception.request
                await app.kernel.resolve_approval(
                    task.task_id, request.request_id, request.payload_hash,
                    ApprovalDecision.APPROVE, "approve fixture patch",
                )
                for index, argv in enumerate((
                    [__import__("sys").executable, "-m", "unittest", "-h"],
                    [
                        __import__("sys").executable, "-m", "unittest",
                        "missing_test_module",
                    ],
                )):
                    call = ToolCall(f"verify-{index}", "core.run_command", {
                        "argv": argv, "mode": "foreground",
                    })
                    with self.assertRaises(ApprovalRequired) as command_approval:
                        await app.kernel.invoke_tool(
                            task.task_id, "latest-turn", call
                        )
                    command_request = command_approval.exception.request
                    await app.kernel.resolve_approval(
                        task.task_id, command_request.request_id,
                        command_request.payload_hash, ApprovalDecision.APPROVE,
                        "approve ordered verification command",
                    )
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify latest failure"
                )
                result = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(result.status, AcceptanceStatus.FAILED)
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
