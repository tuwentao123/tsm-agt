from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider, EchoToolProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AgentCheckpointConflict, AgentTurnCheckpoint, AgentTurnResult, TaskState,
)
from tsm_agt.core.configuration import canonical_hash
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ModelUsage, ProviderCapabilities, RuntimeStorePort, TextBlock,
    ToolCall, ToolCallBlock, ToolResultBlock,
)


class RestartableToolModel(EchoModelProvider):
    descriptor = AdapterDescriptor(
        "fixture.restartable-tool-model", "1.0", "ModelProviderPort", "1.0",
        frozenset({"text", "tools"}),
    )
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    def __init__(self, *, block_after_tool: bool = False, block_first: bool = False):
        super().__init__()
        self.block_after_tool = block_after_tool
        self.block_first = block_first
        self.entered = asyncio.Event()
        self.invocation_count = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.invocation_count += 1
        result = next((
            block.result
            for message in request.messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ), None)
        if (self.block_first and result is None) or (
            self.block_after_tool and result is not None
        ):
            self.entered.set()
            await asyncio.Event().wait()
        if result is None:
            return ModelResponse(
                Message(
                    "assistant-tool", MessageRole.ASSISTANT,
                    (ToolCallBlock(ToolCall(
                        "checkpoint-call", "fixture.echo", {"text": "once"}
                    )),),
                ), FinishReason.TOOL_CALL, ModelUsage(1, 1),
            )
        return ModelResponse(
            Message(
                "assistant-final", MessageRole.ASSISTANT,
                (TextBlock(f"resumed: {result.data['text']}"),),
            ), FinishReason.STOP, ModelUsage(1, 1),
        )


class CountingEchoTool(EchoToolProvider):
    def __init__(self) -> None:
        super().__init__()
        self.invocation_count = 0

    async def invoke(self, call, context):
        self.invocation_count += 1
        return await super().invoke(call, context)


class AgentCheckpointResumeTest(unittest.IsolatedAsyncioTestCase):
    async def _create_task(self, application, workspace: Path):
        task = await application.kernel.create_task(
            "checkpoint resume", workspace, "task-checkpoint"
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

    async def test_restart_after_completed_tool_reuses_result(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        database = root / "runtime.db"
        first_model = RestartableToolModel(block_after_tool=True)
        first_tool = CountingEchoTool()
        first = compose_fixture_application(
            model_adapter=first_model, tool_adapters=(first_tool,),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await first.registry.start_all()
        task = await self._create_task(first, root)
        running = asyncio.create_task(
            first.kernel.run_agent_turn(task.task_id, "start")
        )
        await first_model.entered.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        interrupted = await first.kernel.get_task(task.task_id)
        self.assertIsNotNone(interrupted.active_agent_checkpoint)
        self.assertEqual(first_tool.invocation_count, 1)
        await first.registry.stop_all()

        second_model = RestartableToolModel()
        second_tool = CountingEchoTool()
        second = compose_fixture_application(
            model_adapter=second_model, tool_adapters=(second_tool,),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            result = await second.kernel.resume_checkpointed_agent_turn(task.task_id)
            self.assertIsInstance(result, AgentTurnResult)
            assert isinstance(result, AgentTurnResult)
            self.assertEqual(result.assistant_message.text, "resumed: once")
            self.assertEqual(second_tool.invocation_count, 0)
            restored = await second.kernel.get_task(task.task_id)
            self.assertIsNone(restored.active_agent_checkpoint)
            events = await second.registry.require(RuntimeStorePort).read_events(
                task.task_id
            )
            self.assertIn("turn.resumed", [event.event_type for event in events])
            self.assertEqual(
                sum(event.event_type == "tool.completed" for event in events), 1
            )
        finally:
            await second.registry.stop_all()
            temporary.cleanup()

    async def test_restart_before_first_model_call_resamples_safely(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        database = root / "runtime.db"
        first_model = RestartableToolModel(block_first=True)
        first = compose_fixture_application(
            model_adapter=first_model, store_adapter=SQLiteRuntimeStore(database),
        )
        await first.registry.start_all()
        task = await self._create_task(first, root)
        running = asyncio.create_task(
            first.kernel.run_agent_turn(task.task_id, "start")
        )
        await first_model.entered.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        await first.registry.stop_all()

        second = compose_fixture_application(
            model_adapter=RestartableToolModel(),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            result = await second.kernel.resume_checkpointed_agent_turn(task.task_id)
            assert isinstance(result, AgentTurnResult)
            self.assertEqual(result.assistant_message.text, "resumed: once")
        finally:
            await second.registry.stop_all()
            temporary.cleanup()

    async def test_finder_recovers_executing_task_left_by_process_exit(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        database = root / "runtime.db"
        first_model = RestartableToolModel(block_first=True)
        first = compose_fixture_application(
            model_adapter=first_model, store_adapter=SQLiteRuntimeStore(database),
        )
        await first.registry.start_all()
        session = await first.kernel.create_session("crashed process")
        task = await first.kernel.create_task(
            "keep original goal", root, session_id=session.session_id
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await first.kernel.transition_task(
                task.task_id, state, state.value
            )
        running = asyncio.create_task(
            first.kernel.run_agent_turn(task.task_id, "keep original goal")
        )
        await first_model.entered.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        stranded = await first.kernel.get_task(task.task_id)
        self.assertEqual(stranded.state, TaskState.EXECUTING)
        self.assertIsNotNone(stranded.active_agent_checkpoint)
        await first.registry.stop_all()

        second = compose_fixture_application(
            model_adapter=RestartableToolModel(),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            candidate = await second.kernel.find_recoverable_session_task(
                session.session_id, root
            )
            self.assertIsNotNone(candidate)
            assert candidate is not None
            self.assertEqual(candidate.task_id, task.task_id)
            self.assertEqual(candidate.goal, "keep original goal")
            result = await second.kernel.resume_checkpointed_agent_turn(
                candidate.task_id
            )
            self.assertIsInstance(result, AgentTurnResult)
        finally:
            await second.registry.stop_all()
            temporary.cleanup()

    async def test_finder_does_not_skip_latest_completed_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = RestartableToolModel(block_first=True)
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("latest wins")
                old = await app.kernel.create_task(
                    "old interrupted task", root, session_id=session.session_id
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    old = await app.kernel.transition_task(
                        old.task_id, state, state.value
                    )
                running = asyncio.create_task(app.kernel.run_agent_turn(
                    old.task_id, "old interrupted task"
                ))
                await model.entered.wait()
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
                old = await app.kernel.interrupt_agent_turn(
                    old.task_id, "fixture interruption"
                )
                self.assertEqual(old.state, TaskState.INTERRUPTED)
                self.assertIsNotNone(old.active_agent_checkpoint)
                # Create a newer successful Task and prove the finder never
                # scans behind it to revive the stale interrupted Task.
                latest = await app.kernel.create_task(
                    "new completed task", root, session_id=session.session_id
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING, TaskState.VERIFYING,
                    TaskState.FINALIZING, TaskState.SUCCEEDED,
                ):
                    latest = await app.kernel.transition_task(
                        latest.task_id, state, state.value
                    )
                self.assertIsNone(
                    await app.kernel.find_recoverable_session_task(
                        session.session_id, root
                    )
                )
            finally:
                await app.registry.stop_all()

    async def test_workspace_identity_change_enters_conflict(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        database = root / "runtime.db"
        model = RestartableToolModel(block_first=True)
        first = compose_fixture_application(
            model_adapter=model, store_adapter=SQLiteRuntimeStore(database),
        )
        await first.registry.start_all()
        task = await self._create_task(first, root)
        running = asyncio.create_task(first.kernel.run_agent_turn(task.task_id, "start"))
        await model.entered.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        await first.registry.stop_all()
        (root / "package.json").write_text('{"changed":true}', encoding="utf-8")

        second = compose_fixture_application(
            model_adapter=RestartableToolModel(),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            with self.assertRaisesRegex(
                AgentCheckpointConflict, "workspace_fingerprint"
            ):
                await second.kernel.resume_checkpointed_agent_turn(task.task_id)
            conflicted = await second.kernel.get_task(task.task_id)
            self.assertEqual(conflicted.state, TaskState.CONFLICT)
            self.assertIsNotNone(conflicted.active_agent_checkpoint)
        finally:
            await second.registry.stop_all()
            temporary.cleanup()

    def test_checkpoint_integrity_hash_rejects_tampering(self) -> None:
        checkpoint = AgentTurnCheckpoint(
            "task", "turn", 1, (), (), (), 0, 0, 0, 0, 2, 2, 100, 1.0,
            "workspace", "config", "tools", "prompt",
            action_progress={
                "signature": "action-1",
                "relevant_state_hash": "state-1",
                "consecutive_no_progress": 2,
            },
            evidence_inventory={
                "fingerprints": {"new_paths": ["path-hash"]},
                "consecutive_zero_delta": 1,
            },
            read_hits_state={
                "question_hash": "question-hash",
                "candidate_path_hashes": ["path-hash"],
                "candidate_count": 1,
                "source_tool": "core.search_text",
                "source_family": "SEARCH_DEFINITION",
            },
            artifact_read_state={
                "records": [{
                    "path_hash": "path-hash",
                    "content_hash": "content-hash",
                    "start_line": 1, "end_line": 2, "total_lines": 10,
                    "requested_start_line": 1, "requested_max_lines": 2,
                    "question_hash": "question-hash",
                }],
            },
            progressive_scope_state={
                "tracks": [{
                    "semantic_signature": "semantic-hash",
                    "scope_hash": "scope-hash",
                    "ancestor_hashes": ["workspace-hash"],
                    "depth": 1, "last_zero_results": False,
                }],
            },
            exploration_budget_state={
                "scored_actions": 2,
                "cumulative_tool_milliseconds": 1500,
                "total_new_evidence": 4,
                "low_value_streak": 1,
                "broad_searches": 1,
                "repeated_semantic_actions": 1,
                "semantic_counts": {"semantic-hash": 2},
            },
            stop_or_pivot_state={
                "last_decision": "CHANGE_METHOD",
                "blocked_semantic_signature": "semantic-hash",
                "change_method_attempts": 1,
            },
        )
        self.assertEqual(
            AgentTurnCheckpoint.from_data(checkpoint.to_data()), checkpoint
        )
        data = checkpoint.to_data()
        data["max_tool_calls"] = 999
        with self.assertRaisesRegex(ValueError, "integrity hash"):
            AgentTurnCheckpoint.from_data(data)

    def test_checkpoint_from_before_evidence_guided_state_still_loads(self) -> None:
        checkpoint = AgentTurnCheckpoint(
            "task-old", "turn-old", 1, (), (), (), 0, 0, 0, 0,
            2, 2, 100, 1.0,
        )
        legacy = checkpoint._content_data()
        for field in (
            "evidence_relation_state", "rejection_loop_state",
            "exploration_outcome_state",
        ):
            legacy.pop(field)
        legacy["checkpoint_hash"] = canonical_hash(legacy)
        restored = AgentTurnCheckpoint.from_data(legacy)
        self.assertEqual(restored.evidence_relation_state, {})
        self.assertEqual(restored.rejection_loop_state, {})
        self.assertEqual(restored.exploration_outcome_state, {})


if __name__ == "__main__":
    unittest.main()
