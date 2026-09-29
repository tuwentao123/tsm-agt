"""task-cancellation-spec.md Phase 0 + Phase 1.

Covers §12 #1/#2/#3/#9 and the D7-b intent contract: cancel writes a single
``task.state_changed(next_state=CANCELLED, intent="cancel")`` without passing
through INTERRUPTING, and startup reconciliation settles a crash-left
INTERRUPTING by its recorded intent.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.rule_based_completion_readiness import (
    RuleBasedCompletionReadinessPolicy,
)
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    CANCELLATION_METRICS,
    AgentLoopLimitExceeded,
    AgentTurnCheckpoint,
    CancelTaskInput,
    TaskState,
)
from tsm_agt.core.kernel import InvalidTurnState
from tsm_agt.ports import (
    FinishReason,
    Message,
    MessageRole,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    RuntimeStorePort,
    TextBlock,
    ToolCall,
    ToolCallBlock,
    ToolResultBlock,
)


async def _executing_task(app, root: Path, task_id: str):
    task = await app.kernel.create_task("cancel me", root, task_id=task_id)
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await app.kernel.transition_task(task.task_id, state, state.value)
    return task


async def _state_changes(app, task_id: str):
    events = await app.registry.require(RuntimeStorePort).read_events(task_id)
    return [event for event in events if event.event_type == "task.state_changed"]


class CancelCommandTest(unittest.IsolatedAsyncioTestCase):
    async def _app(self):
        app = compose_fixture_application(tool_adapters=())
        await app.registry.start_all()
        return app

    async def test_cancel_records_intent_without_interrupting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = await self._app()
            try:
                task = await _executing_task(app, Path(directory), "cancel-intent")
                await app.kernel.dispatch_input_event(
                    CancelTaskInput(task.task_id, "user cancelled")
                )
                current = await app.kernel.get_task(task.task_id)
                self.assertIs(current.state, TaskState.CANCELLED)
                changes = await _state_changes(app, task.task_id)
                cancels = [
                    event for event in changes
                    if event.payload.get("next_state") == "CANCELLED"
                ]
                self.assertEqual(len(cancels), 1)
                self.assertEqual(cancels[0].payload.get("intent"), "cancel")
                # D7-b: cancel must not go through INTERRUPTING.
                self.assertFalse(any(
                    event.payload.get("next_state") == "INTERRUPTING"
                    for event in changes
                ))
            finally:
                await app.registry.stop_all()

    async def test_repeated_cancel_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = await self._app()
            try:
                task = await _executing_task(app, Path(directory), "cancel-twice")
                await app.kernel.dispatch_input_event(
                    CancelTaskInput(task.task_id, "first")
                )
                again = await app.kernel.dispatch_input_event(
                    CancelTaskInput(task.task_id, "second")
                )
                self.assertIs(again.state, TaskState.CANCELLED)
                cancels = [
                    event for event in await _state_changes(app, task.task_id)
                    if event.payload.get("next_state") == "CANCELLED"
                ]
                self.assertEqual(len(cancels), 1)
            finally:
                await app.registry.stop_all()

    async def test_cancel_terminal_task_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = await self._app()
            try:
                task = await _executing_task(app, Path(directory), "cancel-terminal")
                await app.kernel.transition_task(
                    task.task_id, TaskState.FAILED, "definite failure"
                )
                with self.assertRaises(InvalidTurnState):
                    await app.kernel.dispatch_input_event(
                        CancelTaskInput(task.task_id, "too late")
                    )
            finally:
                await app.registry.stop_all()

    async def test_cancel_needs_review_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = await self._app()
            try:
                task = await _executing_task(app, Path(directory), "cancel-review")
                await app.kernel.transition_task(
                    task.task_id, TaskState.NEEDS_REVIEW, "stall cap",
                )
                self.assertIs(
                    (await app.kernel.get_task(task.task_id)).state,
                    TaskState.NEEDS_REVIEW,
                )
                await app.kernel.dispatch_input_event(
                    CancelTaskInput(task.task_id, "abandon")
                )
                self.assertIs(
                    (await app.kernel.get_task(task.task_id)).state,
                    TaskState.CANCELLED,
                )
            finally:
                await app.registry.stop_all()


class InterruptIntentTest(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_records_interrupt_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await _executing_task(app, Path(directory), "interrupt-intent")
                await app.kernel.interrupt_agent_turn(task.task_id, "user stopped")
                changes = await _state_changes(app, task.task_id)
                interrupting = [
                    event for event in changes
                    if event.payload.get("next_state") == "INTERRUPTING"
                ]
                self.assertEqual(len(interrupting), 1)
                self.assertEqual(
                    interrupting[0].payload.get("intent"), "interrupt",
                )
            finally:
                await app.registry.stop_all()


class InterruptingReconciliationTest(unittest.IsolatedAsyncioTestCase):
    async def _interrupting(self, app, root: Path, task_id: str, intent):
        task = await _executing_task(app, root, task_id)
        await app.kernel.transition_task(
            task.task_id, TaskState.INTERRUPTING, "crash simulation",
            **({"intent": intent} if intent is not None else {}),
        )
        return task

    async def _settled(self, app, task_id: str):
        return [
            event for event in await _state_changes(app, task_id)
            if event.payload.get("reason") == "interrupting_settled_on_startup"
        ]

    async def test_reconciles_by_recorded_intent(self) -> None:
        cases = (
            ("cancel", TaskState.CANCELLED),
            ("interrupt", TaskState.INTERRUPTED),
            (None, TaskState.INTERRUPTED),  # historical: safe side
        )
        for intent, expected in cases:
            with self.subTest(intent=intent), tempfile.TemporaryDirectory() as directory:
                app = compose_fixture_application(tool_adapters=())
                await app.registry.start_all()
                try:
                    task = await self._interrupting(
                        app, Path(directory), f"reconcile-{intent}", intent,
                    )
                    settled = await app.kernel.reconcile_interrupting_tasks()
                    self.assertIn(task.task_id, settled)
                    self.assertIs(
                        (await app.kernel.get_task(task.task_id)).state,
                        expected,
                    )
                    self.assertEqual(len(await self._settled(app, task.task_id)), 1)
                finally:
                    await app.registry.stop_all()

    async def test_reconciliation_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await self._interrupting(
                    app, Path(directory), "reconcile-idempotent", "interrupt",
                )
                first = await app.kernel.reconcile_interrupting_tasks()
                second = await app.kernel.reconcile_interrupting_tasks()
                self.assertIn(task.task_id, first)
                self.assertNotIn(task.task_id, second)
                self.assertEqual(len(await self._settled(app, task.task_id)), 1)
            finally:
                await app.registry.stop_all()


class CancellationMetricsTest(unittest.IsolatedAsyncioTestCase):
    """Phase 3: minimal process-local counters (assert deltas)."""

    async def test_cancel_increments_requested_metric(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await _executing_task(app, Path(directory), "metric-cancel")
                before = CANCELLATION_METRICS.cancel_requested
                await app.kernel.dispatch_input_event(
                    CancelTaskInput(task.task_id, "metric")
                )
                self.assertEqual(
                    CANCELLATION_METRICS.cancel_requested, before + 1,
                )
            finally:
                await app.registry.stop_all()

    async def test_reconcile_increments_settled_metric(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await _executing_task(app, Path(directory), "metric-settle")
                await app.kernel.transition_task(
                    task.task_id, TaskState.INTERRUPTING, "crash",
                    intent="interrupt",
                )
                before = CANCELLATION_METRICS.interrupting_settled
                await app.kernel.reconcile_interrupting_tasks()
                self.assertEqual(
                    CANCELLATION_METRICS.interrupting_settled, before + 1,
                )
            finally:
                await app.registry.stop_all()


class PendingBatchCancelTest(unittest.IsolatedAsyncioTestCase):
    """§12 #5: cancelling with an accepted-but-unstarted batch call."""

    async def test_cancel_settles_unstarted_batch_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await _executing_task(app, Path(directory), "cancel-batch")
                user = Message("u", MessageRole.USER, (TextBlock("old"),))
                pending = ToolCall("never-run", "fixture.missing", {})
                assistant = Message(
                    "a", MessageRole.ASSISTANT, (ToolCallBlock(pending),),
                )
                checkpoint = AgentTurnCheckpoint(
                    task.task_id, "turn-cancel-batch", 1, (user, assistant),
                    (pending,), (),
                    0, 0, 0, 0, 5, 5, 256, 10.0,
                )
                visible = await app.kernel.list_tools()
                checkpoint = await app.kernel._bind_agent_checkpoint(
                    task, checkpoint, visible
                )
                await app.kernel._save_agent_checkpoint(
                    checkpoint, "test-pending"
                )
                await app.kernel.dispatch_input_event(
                    CancelTaskInput(task.task_id, "cancel batch")
                )
                current = await app.kernel.get_task(task.task_id)
                self.assertIs(current.state, TaskState.CANCELLED)
                events = await app.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                # The batch was settled before the Task terminalised; the
                # terminal transition clears the live checkpoint by design.
                settled = [
                    event for event in events
                    if event.event_type == "checkpoint.saved"
                    and event.payload.get("reason")
                    == "tool-batch-cancelled-by-user"
                ]
                self.assertEqual(len(settled), 1)
                self.assertEqual(settled[0].payload["pending_tool_calls"], 0)
                # No orphan execution record, and the call never ran.
                self.assertFalse(any(
                    execution.call.call_id == "never-run"
                    for execution in current.tool_executions.values()
                ))
                self.assertFalse(any(
                    event.event_type == "tool.requested"
                    and event.payload.get("call", {}).get("call_id") == "never-run"
                    for event in events
                ))
            finally:
                await app.registry.stop_all()


class _ToolCallingModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request) -> ModelResponse:
        return ModelResponse(
            Message(
                "tool-call", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall("loop-call", "fixture.missing", {})),),
            ),
            FinishReason.TOOL_CALL, ModelUsage(1, 1),
        )


class CancelInteractionMatrixTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_while_awaiting_approval(self) -> None:
        """§12 #4: cancelling during AWAITING_APPROVAL settles the Task."""
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await _executing_task(app, Path(directory), "cancel-approval")
                await app.kernel.transition_task(
                    task.task_id, TaskState.AWAITING_APPROVAL, "awaiting",
                )
                await app.kernel.dispatch_input_event(
                    CancelTaskInput(task.task_id, "cancel approval")
                )
                self.assertIs(
                    (await app.kernel.get_task(task.task_id)).state,
                    TaskState.CANCELLED,
                )
                events = await app.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                # No approval decision may be recorded for an abandoned request.
                self.assertFalse(any(
                    event.event_type.startswith("approval")
                    for event in events
                ))
            finally:
                await app.registry.stop_all()

    async def test_budget_exhaustion_never_cancels(self) -> None:
        """§12 #11: a spent budget is not a cancel."""
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=_ToolCallingModel(), tool_adapters=(),
            )
            await app.registry.start_all()
            try:
                task = await _executing_task(app, Path(directory), "cancel-budget")
                with self.assertRaises(AgentLoopLimitExceeded):
                    await app.kernel.run_agent_turn(
                        task.task_id, "loop", max_model_calls=1, max_tool_calls=5,
                    )
                state = (await app.kernel.get_task(task.task_id)).state
                self.assertIsNot(state, TaskState.CANCELLED)
            finally:
                await app.registry.stop_all()


class ScopeWiringTest(unittest.IsolatedAsyncioTestCase):
    """Phase 2: the per-Task scope is owned by the Kernel and closed on settle."""

    async def test_terminal_transition_closes_task_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await _executing_task(app, Path(directory), "scope-close")
                scope = app.kernel._cancellation_scope(task.task_id)
                calls: list[str] = []

                async def cleanup() -> None:
                    calls.append("closed")

                scope.register_cleanup("x", cleanup)
                self.assertIn(task.task_id, app.kernel._cancellation_scopes)
                await app.kernel.transition_task(
                    task.task_id, TaskState.CANCELLED, "settle",
                )
                self.assertNotIn(task.task_id, app.kernel._cancellation_scopes)
                self.assertEqual(calls, ["closed"])
            finally:
                await app.registry.stop_all()

    async def test_scope_metrics_track_open_and_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await _executing_task(app, Path(directory), "scope-metric")
                before = CANCELLATION_METRICS.active_scopes
                app.kernel._cancellation_scope(task.task_id)
                self.assertEqual(CANCELLATION_METRICS.active_scopes, before + 1)
                await app.kernel.transition_task(
                    task.task_id, TaskState.CANCELLED, "settle",
                )
                self.assertEqual(CANCELLATION_METRICS.active_scopes, before)
            finally:
                await app.registry.stop_all()


    async def test_model_stream_registers_stop_event_in_scope(self) -> None:
        from unittest.mock import patch

        from tsm_agt.adapters.http_json import UrllibHttpJsonTransport
        from tsm_agt.core import CancellationScope

        class _FakeStreamResponse:
            def readline(self) -> bytes:
                return b""

            def close(self) -> None:
                return None

        scope = CancellationScope()
        transport = UrllibHttpJsonTransport()
        with patch(
            # The transport is shared infrastructure and lives in http_json;
            # this patch target must follow the implementation.
            "tsm_agt.adapters.http_json.urlopen",
            return_value=_FakeStreamResponse(),
        ):
            chunks = [
                chunk async for chunk in transport.stream_sse(
                    "http://provider/chat", {}, {}, 1.0,
                    cancellation_scope=scope,
                )
            ]
        self.assertEqual(chunks, [])
        names = [entry.name for entry in scope._entries]
        self.assertIn("model-stream", names)


class CancellationSignalTest(unittest.IsolatedAsyncioTestCase):
    """§5: the facade reflects and produces durable cancel intent."""

    async def test_requested_tracks_durable_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await _executing_task(app, Path(directory), "signal-read")
                signal = app.kernel.cancellation_signal(task.task_id)
                self.assertFalse(signal.requested())
                await app.kernel.dispatch_input_event(
                    CancelTaskInput(task.task_id, "via command")
                )
                self.assertTrue(signal.requested())
            finally:
                await app.registry.stop_all()

    async def test_request_writes_durable_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await _executing_task(app, Path(directory), "signal-write")
                signal = app.kernel.cancellation_signal(task.task_id)
                signal.request("via facade")
                for _ in range(50):
                    if signal.requested():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(signal.requested())
                self.assertIs(
                    (await app.kernel.get_task(task.task_id)).state,
                    TaskState.CANCELLED,
                )
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
