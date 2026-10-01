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
    AgentTurnResult, AgentTurnSuspended, ApprovalDecision,
    ProjectTrustLevel, TaskSpecProposal, TaskSpecSnapshot, TaskState,
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
        initial = await application.kernel.get_task_spec(task.task_id)
        proposal = TaskSpecProposal.from_data({
            "schema_version": 1, "goal": task.goal,
            "scope": ["."], "constraints": [],
            "acceptance_criteria": [{
                "criterion_id": "post-mutation-command",
                "description": "Run a successful foreground test or build after changes",
                "verification_kind": "post_mutation_command",
            }],
            "outcomes": [{
                "outcome_id": "verification_required",
                "description": "Run the verification required by this coding task",
                "kind": "COMMAND_RESULT",
                "required_effects": ["execute"],
                "required": True,
            }],
            "continuation_policy": {"mode": "NONE"},
        })
        spec = TaskSpecSnapshot.from_proposal(
            task.task_id, initial.revision + 1, proposal,
            initial.acceptance_criteria,
        )
        await application.kernel._append_events(task.task_id, ((
            "task_spec.revised", {"snapshot": spec.to_data()},
        ),))
        return task

    async def test_verification_command_is_eligible_after_workspace_effects(self) -> None:
        """A modify→verify DAG must not require final delivery before testing."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self._compose(root, root / "runtime.db", CodingLoopModel())
            await app.registry.start_all()
            try:
                task = await self._create_executing_task(app, root)
                initial = await app.kernel.get_task_spec(task.task_id)
                proposal = TaskSpecProposal.from_data({
                    "schema_version": 1,
                    "goal": task.goal,
                    "scope": ["."],
                    "constraints": [],
                    "outcomes": [
                        {
                            "outcome_id": "workspace_search_fix",
                            "description": "Implement the workspace change",
                            "kind": "WORKSPACE_DELIVERY",
                            "required_effects": ["observe", "mutate"],
                            "required": True,
                        },
                        {
                            "outcome_id": "verification_results",
                            "description": "Compile and verify the change",
                            "kind": "COMMAND_RESULT",
                            "required_effects": ["execute", "observe"],
                            "required": True,
                            "depends_on": ["workspace_search_fix"],
                        },
                    ],
                    "continuation_policy": {"mode": "NONE"},
                })
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, initial.revision + 1, proposal,
                    initial.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))

                from tsm_agt.core import ApprovalRequired
                with self.assertRaises(ApprovalRequired) as caught:
                    await app.kernel.invoke_tool(
                        task.task_id, "turn-verify-dag", ToolCall(
                            "compile-change", "core.run_command", {
                                "argv": [
                                    __import__("sys").executable, "-m",
                                    "py_compile", "calc.py",
                                ],
                                "mode": "foreground",
                            },
                            outcome_ref="verification_results",
                        ),
                    )

                self.assertIsNone(caught.exception.request.call.outcome_ref)
                events = await app.kernel.dependencies.store.read_events(
                    task.task_id
                )
                self.assertFalse(any(
                    event.event_type == "task_outcome.binding_decided"
                    for event in events
                ))
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
