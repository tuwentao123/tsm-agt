from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AgentCheckpointConflict, AgentTurnResult, FlowNodeKind, TaskState,
    WorkingMemorySnapshot, WorkingPlanStepStatus,
)
from tsm_agt.cli import _working_memory_show
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ModelUsage, ProviderCapabilities, RuntimeStorePort, TextBlock,
    ToolCall, ToolCallBlock, ToolResultBlock,
)


def working_state(goal: str = "Ship the feature") -> dict[str, object]:
    return {
        "goal": goal,
        "constraints": ["Do not require Git"],
        "facts": ["The project uses Python"],
        "decisions": ["Use deterministic event projection"],
        "hypotheses": ["The failure may be in prompt assembly"],
        "open_questions": ["Does restart preserve the snapshot?"],
        "plan": [{
            "step_id": "inspect", "description": "Inspect the runtime",
            "status": "COMPLETED",
            "completion_criteria": "Relevant code paths identified",
        }, {
            "step_id": "verify", "description": "Run tests",
            "status": "IN_PROGRESS",
            "completion_criteria": "Targeted and full tests pass",
        }],
        "completed_work": ["Inspected the runtime"],
        "remaining_work": ["Run the full test suite"],
        "evidence": [{
            "evidence_id": "created", "kind": "runtime_event",
            "reference": "event:1", "summary": "Task creation is persisted",
        }],
    }


class WorkingMemoryModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)
    descriptor = AdapterDescriptor(
        "fixture.working-memory-model", "1.0", "ModelProviderPort", "1.0"
    )

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        result = next((
            block.result for message in reversed(request.messages)
            for block in message.content if isinstance(block, ToolResultBlock)
            and block.result.call_id == "working-update"
        ), None)
        if result is None:
            return ModelResponse(
                Message(
                    "assistant-update", MessageRole.ASSISTANT,
                    (TextBlock("I will record inspectable progress."), ToolCallBlock(
                        ToolCall("working-update", "core.working_memory_update", {
                            "expected_revision": 1, "state": working_state(),
                            "operation_id": "first-progress",
                        })
                    )),
                ), FinishReason.TOOL_CALL, ModelUsage(5, 3),
            )
        return ModelResponse(
            Message(
                "assistant-final", MessageRole.ASSISTANT,
                (TextBlock("Progress recorded and evidence linked."),),
            ), FinishReason.STOP, ModelUsage(4, 4),
        )


class BlockingWorkingMemoryModel(WorkingMemoryModel):
    descriptor = AdapterDescriptor(
        "fixture.blocking-working-memory-model", "1.0",
        "ModelProviderPort", "1.0"
    )

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.entered.set()
        await asyncio.Event().wait()


class WorkingMemoryTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _executing(application, root: Path, task_id: str, session_id: str | None = None):
        task = await application.kernel.create_task(
            "Implement working memory", root, task_id, session_id
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(task_id, state, state.value)
        return task

    def test_snapshot_round_trip_and_secret_rejection(self) -> None:
        snapshot = WorkingMemorySnapshot.from_update(
            task_id="task-1", revision=2, state=working_state(),
            source_event_sequences=(8,),
        )
        self.assertEqual(WorkingMemorySnapshot.from_data(snapshot.to_data()), snapshot)
        self.assertEqual(snapshot.plan[1].status, WorkingPlanStepStatus.IN_PROGRESS)
        unsafe = working_state()
        unsafe["facts"] = ["api_key=top-secret-value"]
        with self.assertRaisesRegex(ValueError, "credentials"):
            WorkingMemorySnapshot.from_update(
                task_id="task-1", revision=2, state=unsafe,
                source_event_sequences=(8,),
            )

    async def test_agent_updates_scratchpad_without_approval_and_carries_state_to_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = WorkingMemoryModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=(),
                enable_working_memory=True,
            )
            await application.registry.start_all()
            try:
                session = await application.kernel.create_session(
                    "working state", "session-working"
                )
                task = await self._executing(
                    application, root, "task-working", session.session_id
                )
                result = await application.kernel.run_agent_turn(
                    task.task_id, "Implement it"
                )
                self.assertIsInstance(result, AgentTurnResult)
                snapshot = await application.kernel.get_working_memory(task.task_id)
                self.assertEqual(snapshot.revision, 2)
                self.assertEqual(snapshot.goal, "Ship the feature")
                self.assertEqual(snapshot.decisions, ("Use deterministic event projection",))
                self.assertEqual(snapshot.remaining_work, ("Run the full test suite",))
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                types = [event.event_type for event in events]
                self.assertIn("working_memory.updated", types)
                self.assertNotIn("approval.requested", types)
                policy = next(
                    event for event in events
                    if event.event_type == "policy.evaluated"
                    and event.payload.get("tool_name") == "core.working_memory_update"
                )
                self.assertEqual(policy.payload["decision"]["action"], "allow")
                flow = await application.kernel.get_flow_projection(task.task_id)
                working_node = next(
                    node for node in flow.nodes
                    if node.label == "Working memory updated"
                )
                self.assertEqual(working_node.kind, FlowNodeKind.MEMORY)
                diagnostic = flow.inspect_node(working_node.node_id)
                encoded_diagnostic = json.dumps(
                    diagnostic.to_data(), ensure_ascii=False
                )
                self.assertIn(snapshot.content_hash, encoded_diagnostic)
                self.assertNotIn("Do not require Git", encoded_diagnostic)
                conversation = await application.kernel.get_session_conversation(
                    session.session_id
                )
                self.assertEqual(conversation.working_state.goal, "Ship the feature")
                self.assertEqual(
                    conversation.working_state.decisions,
                    ("Use deterministic event projection",),
                )
                second = await self._executing(
                    application, root, "task-follow-up", session.session_id
                )
                context = await application.kernel._session_context_message(second.task_id)
                assert context is not None
                body = json.loads(context.text)
                self.assertEqual(body["working_state"]["goal"], "Ship the feature")
            finally:
                await application.registry.stop_all()

    async def test_sqlite_restart_rebuilds_identical_working_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".agent").mkdir()
            database = root / ".agent" / "runtime.db"
            first = compose_fixture_application(
                model_adapter=WorkingMemoryModel(),
                store_adapter=SQLiteRuntimeStore(database), tool_adapters=(),
            )
            await first.registry.start_all()
            try:
                task = await self._executing(first, root, "task-restart")
                current = await first.kernel.get_working_memory(task.task_id)
                updated = await first.kernel.update_working_memory(
                    task.task_id, current.revision, working_state(), "operation-1",
                    "test",
                )
            finally:
                await first.registry.stop_all()

            second = compose_fixture_application(
                model_adapter=WorkingMemoryModel(),
                store_adapter=SQLiteRuntimeStore(database), tool_adapters=(),
            )
            await second.registry.start_all()
            try:
                rebuilt = await second.kernel.get_working_memory("task-restart")
                self.assertEqual(rebuilt, updated)
                replay = await second.kernel.update_working_memory(
                    "task-restart", 1, working_state(), "operation-1", "test"
                )
                self.assertEqual(replay, updated)
                with self.assertRaisesRegex(ValueError, "reused differently"):
                    await second.kernel.update_working_memory(
                        "task-restart", 1, working_state("different"),
                        "operation-1", "test",
                    )
            finally:
                await second.registry.stop_all()

            output = io.StringIO()
            with redirect_stdout(output):
                result = await _working_memory_show(
                    "task-restart", root, as_json=True
                )
            self.assertEqual(result, 0)
            rendered = json.loads(output.getvalue())
            self.assertEqual(rendered["content_hash"], updated.content_hash)
            self.assertEqual(rendered["remaining_work"], ["Run the full test suite"])

    async def test_fake_evidence_and_stale_revision_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=WorkingMemoryModel(), tool_adapters=(),
                enable_working_memory=True,
            )
            await application.registry.start_all()
            try:
                task = await self._executing(application, root, "task-invalid")
                invalid = working_state()
                invalid["evidence"] = [{
                    "evidence_id": "fake", "kind": "test",
                    "reference": "event:999999", "summary": "not real",
                }]
                with self.assertRaisesRegex(ValueError, "does not exist"):
                    await application.kernel.update_working_memory(
                        task.task_id, 1, invalid, "fake", "test"
                    )
                await application.kernel.update_working_memory(
                    task.task_id, 1, working_state(), "valid", "test"
                )
                with self.assertRaisesRegex(ValueError, "revision conflict"):
                    await application.kernel.update_working_memory(
                        task.task_id, 1, working_state("stale"), "stale", "test"
                    )
            finally:
                await application.registry.stop_all()

    async def test_external_scratchpad_change_conflicts_with_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = BlockingWorkingMemoryModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=(), enable_working_memory=True
            )
            await application.registry.start_all()
            try:
                task = await self._executing(application, root, "task-conflict")
                running = asyncio.create_task(
                    application.kernel.run_agent_turn(task.task_id, "start")
                )
                await model.entered.wait()
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
                await application.kernel.update_working_memory(
                    task.task_id, 1, working_state(), "external", "user"
                )
                with self.assertRaisesRegex(
                    AgentCheckpointConflict, "working_memory_hash"
                ):
                    await application.kernel.resume_checkpointed_agent_turn(task.task_id)
                self.assertEqual(
                    (await application.kernel.get_task(task.task_id)).state,
                    TaskState.CONFLICT,
                )
            finally:
                await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
