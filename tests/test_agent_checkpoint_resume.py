from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider, EchoToolProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AgentCheckpointConflict, AgentTurnCheckpoint, AgentTurnResult,
    SessionContinuationMode, SessionResumeSafety, TaskState,
)
from tsm_agt.core.configuration import canonical_hash
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ModelUsage, ProviderCapabilities, RuntimeStorePort, TextBlock,
    CheckpointCompatibilityAction, CheckpointCompatibilityDecision,
    EvidenceQuestion, ToolCall, ToolCallBlock, ToolResultBlock,
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


class UpgradedRestartableModel(RestartableToolModel):
    descriptor = AdapterDescriptor(
        "fixture.restartable-tool-model", "2.0",
        "ModelProviderPort", "1.0", frozenset({"text", "tools"}),
    )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.invocation_count += 1
        return ModelResponse(
            Message(
                "assistant-upgraded-final", MessageRole.ASSISTANT,
                (TextBlock("resumed with current runtime"),),
            ), FinishReason.STOP, ModelUsage(1, 1),
        )


class EvidenceCheckpointModel(RestartableToolModel):
    """Expose the commit-to-checkpoint crash window deterministically."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.invocation_count += 1
        result = next((
            block.result
            for message in request.messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ), None)
        if self.block_after_tool and result is not None:
            self.entered.set()
            await asyncio.Event().wait()
        if result is None:
            return ModelResponse(
                Message(
                    "assistant-evidence-tool", MessageRole.ASSISTANT,
                    (ToolCallBlock(ToolCall(
                        "checkpoint-evidence-call", "fixture.echo",
                        {"text": "durable-result"},
                        EvidenceQuestion("Q1", "What did the tool return?"),
                    )),),
                ), FinishReason.TOOL_CALL, ModelUsage(1, 1),
            )
        return ModelResponse(
            Message(
                "assistant-evidence-final", MessageRole.ASSISTANT,
                (TextBlock(f"resumed: {result.data['text']}"),),
            ), FinishReason.STOP, ModelUsage(1, 1),
        )


class AgentCheckpointResumeTest(unittest.IsolatedAsyncioTestCase):
    async def test_awaiting_continuation_rebases_after_runtime_upgrade(self):
        """An old completed-unit boundary uses the normal safe rebase path."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
            )
            await application.registry.start_all()
            try:
                session = await application.kernel.create_session(
                    "upgrade continuation"
                )
                task = await application.kernel.create_task(
                    "finish remaining analysis", root,
                    session_id=session.session_id,
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await application.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                checkpoint = AgentTurnCheckpoint(
                    task.task_id, "turn-upgrade-continuation", 1,
                    (Message(
                        "continuation-user", MessageRole.USER,
                        (TextBlock(task.goal),),
                    ),),
                    (), (), 1, 0, 1, 0, 4, 4, 256, 10.0,
                    pending_user_action={
                        "kind": "CONTINUATION",
                        "reason": "incomplete_recoverable",
                        "completed_outcome_ids": [],
                        "remaining_outcome_ids": ["remaining-analysis"],
                    },
                )
                checkpoint = await application.kernel._bind_agent_checkpoint(
                    task, checkpoint, await application.kernel.list_tools()
                )
                await application.kernel._save_agent_checkpoint(
                    checkpoint, "old-runtime-continuation"
                )
                task = await application.kernel.transition_task(
                    task.task_id, TaskState.AWAITING_USER,
                    "completed unit awaits continuation",
                )
                decision = CheckpointCompatibilityDecision(
                    CheckpointCompatibilityAction.REBASE_REQUIRED,
                    "safe_runtime_upgrade_rebase",
                    rebase_reasons=("adapter_lock_hash",),
                    discard_pending_tool_calls=True,
                    reset_transient_state=True,
                )
                with unittest.mock.patch.object(
                    application.kernel, "_evaluate_checkpoint_compatibility",
                    unittest.mock.AsyncMock(return_value=decision),
                ):
                    result = await application.kernel.resume_agent_continuation(
                        task.task_id, "continue with the remaining work",
                        input_id="input-after-upgrade",
                    )
                self.assertIsInstance(result, AgentTurnResult)
                live = await application.kernel.get_task(task.task_id)
                self.assertEqual(live.state, TaskState.EXECUTING)
                events = await application.kernel.dependencies.store.read_events(
                    task.task_id
                )
                self.assertTrue(any(
                    event.event_type == "turn.rebased" for event in events
                ))
                self.assertTrue(any(
                    event.event_type == "continuation.resolved"
                    for event in events
                ))
                self.assertFalse(any(
                    event.event_type == "checkpoint.conflict" for event in events
                ))
            finally:
                await application.registry.stop_all()

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

    async def test_restart_reconciles_event_log_ahead_of_checkpoint(self) -> None:
        """Committed result/Evidence wins without replaying the Tool."""
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        database = root / "runtime.db"
        first_model = EvidenceCheckpointModel(block_after_tool=True)
        first_tool = CountingEchoTool()
        first = compose_fixture_application(
            model_adapter=first_model, tool_adapters=(first_tool,),
            store_adapter=SQLiteRuntimeStore(database),
            require_evidence_questions=True,
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
        await first.kernel.interrupt_agent_turn(
            task.task_id, "crash after durable evidence event"
        )
        interrupted = await first.kernel.get_task(task.task_id)
        assert interrupted.active_agent_checkpoint is not None
        live = AgentTurnCheckpoint.from_data(
            interrupted.active_agent_checkpoint
        )
        execution = next(iter(interrupted.tool_executions.values()))
        live_evidence = dict(live.evidence_question_state)
        stale_records = [dict(item) for item in live_evidence["records"]]
        stale_records[0].update({
            "status": "OPEN", "evidence_references": [],
            "observation_kind": None, "blocking_reason": None,
            "revision": stale_records[0]["revision"] - 1,
            "updated_event_sequence": max(
                0, stale_records[0]["updated_event_sequence"] - 1
            ),
        })
        stale_evidence = {**live_evidence, "records": stale_records}
        stale = replace(
            live, revision=live.revision + 1,
            messages=tuple(
                message for message in live.messages
                if not any(
                    isinstance(block, ToolResultBlock)
                    for block in message.content
                )
            ),
            pending_tool_calls=(execution.call,), tool_calls=0,
            evidence_question_state=stale_evidence,
        )
        await first.kernel._save_agent_checkpoint(
            stale, "fixture-stale-after-event-commit"
        )
        self.assertEqual(first_tool.invocation_count, 1)
        await first.registry.stop_all()

        second_model = EvidenceCheckpointModel()
        second_tool = CountingEchoTool()
        second = compose_fixture_application(
            model_adapter=second_model, tool_adapters=(second_tool,),
            store_adapter=SQLiteRuntimeStore(database),
            require_evidence_questions=True,
        )
        await second.registry.start_all()
        try:
            candidates = await second.kernel.list_session_resume_candidates(
                interrupted.session_id, root
            )
            selected = next(
                item for item in candidates if item.task_id == task.task_id
            )
            self.assertEqual(
                selected.safety, SessionResumeSafety.RECONCILE_REQUIRED
            )
            self.assertEqual(
                selected.reason_code, "checkpoint_projection_refresh_required"
            )
            result = await second.kernel.resume_checkpointed_agent_turn(
                task.task_id
            )
            self.assertIsInstance(result, AgentTurnResult)
            assert isinstance(result, AgentTurnResult)
            self.assertEqual(
                result.assistant_message.text, "resumed: durable-result"
            )
            self.assertEqual(second_tool.invocation_count, 0)
            completed = await second.kernel.get_task(task.task_id)
            self.assertIsNone(completed.active_agent_checkpoint)
            events = await second.registry.require(
                RuntimeStorePort
            ).read_events(task.task_id)
            reconciled = next(
                event for event in events
                if event.event_type == "checkpoint.reconciled"
            )
            self.assertFalse(reconciled.payload["tool_calls_replayed"])
            saved = next(
                event for event in events
                if event.event_type == "checkpoint.saved"
                and event.payload.get("reason") == "event-log-reconciled"
            )
            self.assertEqual(saved.payload["pending_tool_calls"], 0)
            self.assertEqual(
                sum(event.event_type == "tool.completed" for event in events),
                1,
            )
        finally:
            await second.registry.stop_all()
            temporary.cleanup()

    async def test_active_checkpoint_projection_is_safe_deduplicated_and_restartable(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        database = root / "runtime.db"
        first_model = RestartableToolModel(block_after_tool=True)
        first = compose_fixture_application(
            model_adapter=first_model, tool_adapters=(CountingEchoTool(),),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await first.registry.start_all()
        task = await self._create_task(first, root)
        running = asyncio.create_task(
            first.kernel.run_agent_turn(task.task_id, "private turn input")
        )
        await first_model.entered.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        await first.kernel.interrupt_agent_turn(
            task.task_id, "simulate process interruption"
        )
        interrupted = await first.kernel.get_task(task.task_id)
        assert interrupted.active_agent_checkpoint is not None
        checkpoint = AgentTurnCheckpoint.from_data(
            interrupted.active_agent_checkpoint
        )
        # Add a visible result for the same Task to prove the active projection
        # replaces, rather than duplicates, its recent Task summary.
        await first.kernel._record_session_task_result(
            task.task_id, checkpoint.turn_id,
            Message(
                "visible-user", MessageRole.USER,
                (TextBlock("visible historical request"),),
            ),
            Message(
                "visible-assistant", MessageRole.ASSISTANT,
                (TextBlock("visible partial result"),),
            ),
        )
        first_projection = await first.kernel.get_session_prompt_projection(
            task.session_id
        )
        assert first_projection.message is not None
        first_body = json.loads(first_projection.message.text)
        active = first_body["active_checkpoint"]
        self.assertEqual(active["task_id"], task.task_id)
        self.assertEqual(active["task_state"], "INTERRUPTED")
        self.assertEqual(
            active["continuation"], "resume_from_authoritative_checkpoint"
        )
        self.assertEqual(active["progress"]["tool_calls"], 1)
        self.assertEqual(first_body["recent_task_summaries"], [])
        self.assertEqual(first_body["earlier_summary"]["tasks"], [])
        encoded = json.dumps(active, ensure_ascii=False)
        self.assertNotIn("private turn input", encoded)
        self.assertNotIn('\"text\": \"once\"', encoded)
        self.assertNotIn("messages", active)
        self.assertNotIn("pending_tool_calls", active)
        suspended = first_body["suspended_tasks"]["items"]
        self.assertEqual(len(suspended), 1)
        self.assertEqual(suspended[0]["task_id"], task.task_id)
        suspended_encoded = json.dumps(suspended, ensure_ascii=False)
        self.assertNotIn("private turn input", suspended_encoded)
        self.assertNotIn('"text": "once"', suspended_encoded)
        self.assertNotIn("pending_tool_calls", suspended_encoded)
        await first.registry.stop_all()

        restarted = compose_fixture_application(
            model_adapter=RestartableToolModel(),
            tool_adapters=(CountingEchoTool(),),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await restarted.registry.start_all()
        try:
            restarted_projection = (
                await restarted.kernel.get_session_prompt_projection(
                    task.session_id
                )
            )
            assert restarted_projection.message is not None
            restarted_body = json.loads(restarted_projection.message.text)
            self.assertEqual(
                restarted_body["active_checkpoint"],
                first_body["active_checkpoint"],
            )
        finally:
            await restarted.registry.stop_all()
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

    async def test_finder_keeps_older_interrupted_task_addressable(self) -> None:
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
                # A newer unrelated Task may move the Session cursor, but must
                # not erase the older Task's durable recovery address.
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
                candidate = await app.kernel.find_recoverable_session_task(
                    session.session_id, root
                )
                self.assertIsNotNone(candidate)
                assert candidate is not None
                self.assertEqual(candidate.task_id, old.task_id)
            finally:
                await app.registry.stop_all()

    async def test_multiple_interrupted_tasks_require_explicit_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                model_adapter=RestartableToolModel(), tool_adapters=()
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("two suspended tasks")
                for index in (1, 2):
                    task = await app.kernel.create_task(
                        f"unfinished goal {index}", root,
                        session_id=session.session_id,
                    )
                    for state in (
                        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                        TaskState.EXECUTING,
                    ):
                        task = await app.kernel.transition_task(
                            task.task_id, state, state.value
                        )
                    model = app.kernel.dependencies.model
                    assert isinstance(model, RestartableToolModel)
                    model.block_first = True
                    model.entered.clear()
                    running = asyncio.create_task(
                        app.kernel.run_agent_turn(task.task_id, task.goal)
                    )
                    await model.entered.wait()
                    running.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await running
                    await app.kernel.interrupt_agent_turn(
                        task.task_id, "fixture interruption"
                    )
                    model.block_first = False
                decision = await app.kernel.resolve_session_continuation(
                    session.session_id, root
                )
                self.assertEqual(
                    decision.mode, SessionContinuationMode.MULTIPLE_CANDIDATES
                )
                self.assertEqual(len(decision.candidates), 2)
                selected = await app.kernel.resolve_session_continuation(
                    session.session_id, root,
                    task_id=decision.candidates[-1].task_id,
                )
                self.assertEqual(selected.mode, SessionContinuationMode.RECOVER_TASK)
                self.assertEqual(
                    selected.task_id, decision.candidates[-1].task_id
                )
            finally:
                await app.registry.stop_all()

    async def test_resume_candidate_survives_sqlite_restart(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        database = root / "runtime.db"
        first_model = RestartableToolModel(block_first=True)
        first = compose_fixture_application(
            model_adapter=first_model, tool_adapters=(),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await first.registry.start_all()
        task = await self._create_task(first, root)
        running = asyncio.create_task(
            first.kernel.run_agent_turn(task.task_id, "persist me")
        )
        await first_model.entered.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        await first.kernel.interrupt_agent_turn(task.task_id, "restart")
        before = await first.kernel.list_session_resume_candidates(
            task.session_id, root
        )
        await first.registry.stop_all()
        second = compose_fixture_application(
            model_adapter=RestartableToolModel(), tool_adapters=(),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            after = await second.kernel.list_session_resume_candidates(
                task.session_id, root
            )
            self.assertEqual(after, before)
            self.assertEqual(after[0].safety, SessionResumeSafety.EXACT_RESUME)
        finally:
            await second.registry.stop_all()
            temporary.cleanup()

    async def test_zero_side_effect_runtime_upgrade_rebases_and_resumes(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        database = root / "runtime.db"
        first_model = RestartableToolModel(block_first=True)
        first = compose_fixture_application(
            model_adapter=first_model, tool_adapters=(),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await first.registry.start_all()
        task = await self._create_task(first, root)
        running = asyncio.create_task(
            first.kernel.run_agent_turn(task.task_id, "preserve this goal")
        )
        await first_model.entered.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        await first.kernel.interrupt_agent_turn(task.task_id, "runtime upgrade")
        await first.registry.stop_all()

        second = compose_fixture_application(
            model_adapter=UpgradedRestartableModel(), tool_adapters=(),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            candidates = await second.kernel.list_session_resume_candidates(
                task.session_id, root
            )
            self.assertEqual(len(candidates), 1)
            self.assertEqual(
                candidates[0].safety, SessionResumeSafety.REBASE_REQUIRED
            )
            self.assertEqual(
                candidates[0].reason_code, "safe_runtime_upgrade_rebase"
            )
            self.assertIn("adapter_lock_hash", candidates[0].rebase_reasons)

            result = await second.kernel.resume_checkpointed_agent_turn(
                task.task_id
            )
            self.assertIsInstance(result, AgentTurnResult)
            restored = await second.kernel.get_task(task.task_id)
            self.assertEqual(restored.goal, "checkpoint resume")
            self.assertEqual(len(restored.effective_configurations), 2)
            events = await second.registry.require(RuntimeStorePort).read_events(
                task.task_id
            )
            event_types = [event.event_type for event in events]
            self.assertIn("config.rebased", event_types)
            self.assertIn("turn.rebased", event_types)
            self.assertIn("turn.resumed", event_types)
            rebased = next(
                event for event in events if event.event_type == "turn.rebased"
            )
            self.assertFalse(rebased.payload["tool_calls_replayed"])
        finally:
            await second.registry.stop_all()
            temporary.cleanup()

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
            evidence_question_state={
                "schema_version": 1, "task_id": "task",
                "records": [],
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
            completion_readiness_state={
                "continue_attempts": 1,
                "disclosure_attempts": 1,
                "last_action": "REPORT_BLOCKED",
                "schema_version": 1,
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
            "exploration_outcome_state", "evidence_question_state",
            "completion_readiness_state",
            "task_spec_revision", "task_spec_hash",
            "active_outcome_ids", "pending_user_action",
        ):
            legacy.pop(field)
        legacy["checkpoint_hash"] = canonical_hash(legacy)
        restored = AgentTurnCheckpoint.from_data(legacy)
        self.assertEqual(restored.evidence_relation_state, {})
        self.assertEqual(restored.rejection_loop_state, {})
        self.assertEqual(restored.exploration_outcome_state, {})
        self.assertEqual(restored.evidence_question_state, {})
        self.assertEqual(restored.completion_readiness_state, {})
        self.assertEqual(restored.task_spec_revision, 0)
        self.assertEqual(restored.task_spec_hash, "")
        self.assertEqual(restored.active_outcome_ids, ())
        self.assertEqual(restored.pending_user_action, {})


if __name__ == "__main__":
    unittest.main()
