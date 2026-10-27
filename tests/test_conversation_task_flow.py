"""P0 behavior for conversation Tasks: one Task per Session, one Turn per input.

These pin the facts-only routing contract: an idle conversation takes the next
ordinary input as another turn, a running one is interrupted and restarted on
the same Task without asking a model, and only an explicit new-task intent
creates a second Task.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.default_session_input_relation.judge import (
    DefaultSessionInputRelationJudge,
)
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import RuntimeInputIntent, TaskState
from tsm_agt.core.conversation_orchestration import (
    TaskBindingKind,
    TaskBindingRequest,
    TaskBindingTask,
    bind_task_input,
)
from tsm_agt.core.task import TaskKind
from tsm_agt.ports import (
    ImageBlock,
    MessageRole,
    ModelAttemptFailure,
    ModelFailureCategory,
    ModelRecoveryAction,
    ModelRecoveryDecision,
    ModelRecoveryExhausted,
    ModelRequest,
    ModelResponse,
    ModelRetrySafety,
    RuntimeStorePort,
    SessionInputRelation,
    SessionInputRelationJudgement,
)
from tsm_agt.sdk import EngineeringAgentClient


class _BlockingEchoModel(EchoModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.entered.set()
        await self.release.wait()
        return await super().complete(request)


class _GateModel(EchoModelProvider):
    """Blocks each model call until the test opens the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = 0
        self.gate = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.entered += 1
        await self.gate.wait()
        return await super().complete(request)


class _BlockingTurnModel(EchoModelProvider):
    """Holds one chosen Turn inside its model call until the test releases it."""

    def __init__(self, *, block_at_call: int) -> None:
        super().__init__()
        self.calls = 0
        self._block_at_call = block_at_call
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls == self._block_at_call:
            self.entered.set()
            await self.release.wait()
        return await super().complete(request)


class _DroppingModel(EchoModelProvider):
    """Reproduces a provider/gateway dropping the stream during a call.

    Retry safety is ``SAFE_SAME_REQUEST``, which is exactly the case that is
    *resumable* rather than failed: the conversation must survive it.
    """

    def __init__(self, *, fail_at_call: int) -> None:
        super().__init__()
        self.calls = 0
        self._fail_at_call = fail_at_call

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls >= self._fail_at_call:
            raise ModelRecoveryExhausted(
                ModelAttemptFailure(
                    ModelFailureCategory.TRANSIENT_PROVIDER,
                    ModelRetrySafety.SAFE_SAME_REQUEST,
                    "connection_failed",
                    "provider connection failed: unexpected EOF",
                ),
                ModelRecoveryDecision(
                    ModelRecoveryAction.PRESERVE_AND_INTERRUPT, "exhausted",
                ),
                attempts=3,
            )
        return await super().complete(request)


class _SpyRelationJudge(DefaultSessionInputRelationJudge):
    """Records whether routing ever asks for a semantic relation."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def judge_input_relation(self, text, context):
        self.calls.append(text)
        return SessionInputRelationJudgement(
            SessionInputRelation.UNRELATED, 1.0, "spy",
        )


class ConversationTaskFlowTest(unittest.IsolatedAsyncioTestCase):
    async def _client(self, *, model=None, judge=None):
        application = compose_fixture_application(
            model_adapter=model or EchoModelProvider(),
            tool_adapters=(),
            session_input_relation_adapter=judge,
        )
        root = Path(tempfile.mkdtemp())
        client = EngineeringAgentClient(
            root, application_factory=lambda: application, poll_interval=0.001
        )
        await client.start()
        return application, client, root

    @staticmethod
    async def _events(application, task_id):
        return await application.registry.require(
            RuntimeStorePort
        ).read_events(task_id)

    async def _send(self, client, root, text, *, input_id, intent=None, images=()):
        return await client.submit_user_input(
            "conv", text, input_id=input_id, explicit_intent=intent,
            images=tuple(images), workspace=root,
        )

    async def test_conversation_reuses_one_task_across_inputs(self):
        application, client, root = await self._client()
        try:
            first = dict((await self._send(client, root, "hello", input_id="i1")).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)
            for index, text in enumerate(("继续", "愿意"), start=2):
                await self._send(client, root, text, input_id=f"i{index}")
                await client.wait_task(task_id, timeout=5)

            events = await self._events(application, task_id)
            types = [event.event_type for event in events]
            self.assertEqual(types.count("task.created"), 1)
            self.assertEqual(types.count("turn.started"), 3)
            task = await application.kernel.get_task(task_id)
            self.assertIs(task.task_kind, TaskKind.CONVERSATION)
            self.assertIs(task.state, TaskState.AWAITING_INPUT)
            self.assertIsNone(task.active_agent_checkpoint)
        finally:
            await client.close()

    async def test_every_turn_input_survives_in_the_session_history(self) -> None:
        # A conversation Task records one input per turn. Only the first one
        # rides the attachment event; the rest ride their own result event, so
        # a reload must still show every question next to its answer.
        application, client, root = await self._client()
        try:
            first = dict((await self._send(
                client, root, "看下今天的新闻", input_id="i1"
            )).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)
            for index, text in enumerate(("再看下天气", "那继续"), start=2):
                await self._send(client, root, text, input_id=f"i{index}")
                await client.wait_task(task_id, timeout=5)

            projection = await application.kernel.get_session_conversation("conv")

            asked = [
                item.text for item in projection.messages
                if item.role is MessageRole.USER
            ]
            self.assertEqual(asked, ["看下今天的新闻", "再看下天气", "那继续"])
        finally:
            await client.close()

    async def test_a_stopped_first_turn_is_recorded_not_hung(self) -> None:
        # The provider drops the stream. The Task must carry the stop as an
        # event instead of staying EXECUTING forever with the exception lost in
        # an unretrieved background task.
        application, client, root = await self._client(
            model=_DroppingModel(fail_at_call=1)
        )
        try:
            first = dict((await self._send(
                client, root, "hello", input_id="i1"
            )).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)

            task = await application.kernel.get_task(task_id)
            self.assertIs(task.state, TaskState.INTERRUPTED)
            types = [event.event_type for event in await self._events(
                application, task_id
            )]
            self.assertIn("llm.failed", types)
            self.assertNotIn("turn.completed", types)
        finally:
            await client.close()

    async def test_a_stopped_continuation_turn_is_recorded_not_hung(self) -> None:
        application, client, root = await self._client(
            model=_DroppingModel(fail_at_call=2)
        )
        try:
            first = dict((await self._send(
                client, root, "hello", input_id="i1"
            )).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)

            # The second input's turn is the one the provider drops.
            await self._send(client, root, "again", input_id="i2")
            await client.wait_task(task_id, timeout=5)

            task = await application.kernel.get_task(task_id)
            self.assertIs(task.state, TaskState.INTERRUPTED)
            self.assertIs(task.task_kind, TaskKind.CONVERSATION)
            events = await self._events(application, task_id)
            types = [event.event_type for event in events]
            self.assertEqual(types.count("task.created"), 1)
            self.assertEqual(types.count("llm.failed"), 1)
            self.assertIn("turn.interrupted", types)
        finally:
            await client.close()

    async def test_turn_counters_ride_the_session_result(self) -> None:
        # The Web shows "模型 N 次 · 工具 N 次" per Turn. Those counters are
        # retired with the checkpoint, so the Session record must keep them.
        application, client, root = await self._client()
        try:
            first = dict((await self._send(
                client, root, "hello", input_id="i1"
            )).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)

            events = await application.registry.require(
                RuntimeStorePort
            ).read_session_events("conv")
            recorded = [
                event for event in events
                if event.event_type == "session.task_result_recorded"
            ]

            self.assertEqual(len(recorded), 1)
            counters = recorded[0].payload["turn_counters"]
            self.assertEqual(counters["model_calls"], 1)
            self.assertEqual(counters["tool_calls"], 0)
        finally:
            await client.close()

    async def test_turn_input_is_history_from_the_moment_it_starts(self) -> None:
        # A Turn that is stopped before it answers still has to show what was
        # asked. The input is therefore recorded when the Turn starts, and the
        # result event's copy of the same message must not appear twice.
        application, client, root = await self._client()
        try:
            first = dict((await self._send(
                client, root, "first question", input_id="i1"
            )).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)
            await self._send(client, root, "second question", input_id="i2")
            await client.wait_task(task_id, timeout=5)

            stored = await application.registry.require(
                RuntimeStorePort
            ).read_session_events("conv")
            recorded = [
                event for event in stored
                if event.event_type == "session.turn_input_recorded"
            ]
            self.assertEqual(len(recorded), 2)
            self.assertEqual(
                [event.payload["user_message"]["content"][0]["text"]
                 for event in recorded],
                ["first question", "second question"],
            )

            projection = await application.kernel.get_session_conversation("conv")
            asked = [
                item.text for item in projection.messages
                if item.role is MessageRole.USER
            ]
            self.assertEqual(asked, ["first question", "second question"])
        finally:
            await client.close()

    async def test_a_stopped_turn_keeps_its_question_in_history(self) -> None:
        model = _BlockingTurnModel(block_at_call=2)
        application, client, root = await self._client(model=model)
        try:
            first = dict((await self._send(
                client, root, "answered question", input_id="i1"
            )).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)

            # The second Turn is held inside the model call, then interrupted.
            await self._send(client, root, "unanswered question", input_id="i2")
            await asyncio.wait_for(model.entered.wait(), timeout=5)
            await client.interrupt(
                task_id, command_id="stop", reason="test stop",
            )
            model.release.set()
            # Interrupting cancels the turn's runner, so awaiting it re-raises.
            with contextlib.suppress(asyncio.CancelledError):
                await client.wait_task(task_id, timeout=5)

            projection = await application.kernel.get_session_conversation("conv")
            asked = [
                item.text for item in projection.messages
                if item.role is MessageRole.USER
            ]
            self.assertEqual(
                asked, ["answered question", "unanswered question"]
            )
        finally:
            await client.close()

    async def test_a_new_message_restarts_an_interrupted_conversation(self) -> None:
        # The next message is a new Turn. Resuming the interrupted Turn instead
        # would keep its checkpoint and silently drop the new text.
        model = _DroppingModel(fail_at_call=2)
        application, client, root = await self._client(model=model)
        try:
            first = dict((await self._send(
                client, root, "hello", input_id="i1"
            )).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)

            await self._send(client, root, "dropped", input_id="i2")
            await client.wait_task(task_id, timeout=5)
            self.assertIs(
                (await application.kernel.get_task(task_id)).state,
                TaskState.INTERRUPTED,
            )

            # The provider recovers; the third message must produce a real Turn.
            model._fail_at_call = 99
            await self._send(client, root, "third question", input_id="i3")
            await client.wait_task(task_id, timeout=5)

            events = await self._events(application, task_id)
            turns = [
                event.payload["turn_id"] for event in events
                if event.event_type == "turn.started"
            ]
            self.assertEqual(len(turns), 3)
            task = await application.kernel.get_task(task_id)
            self.assertIs(task.state, TaskState.AWAITING_INPUT)
            # The stale checkpoint of the dropped Turn is gone, so the new Turn
            # cannot re-enter it.
            self.assertIsNone(task.active_agent_checkpoint)

            projection = await application.kernel.get_session_conversation("conv")
            asked = [
                item.text for item in projection.messages
                if item.role is MessageRole.USER
            ]
            self.assertIn("third question", asked)
        finally:
            await client.close()

    async def test_turn_completion_is_atomic(self):
        application, client, root = await self._client()
        try:
            first = dict((await self._send(client, root, "hello", input_id="i1")).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)

            events = await self._events(application, task_id)
            completed = [
                event.sequence for event in events
                if event.event_type == "turn.completed"
            ]
            self.assertEqual(len(completed), 1)
            # The close transition shares the turn.completed commit: no later
            # state_changed leaves EXECUTING, and no EXECUTING+empty checkpoint
            # window exists.
            idle = [
                event.sequence for event in events
                if event.event_type == "task.state_changed"
                and event.payload.get("next_state") == "AWAITING_INPUT"
            ]
            self.assertEqual(len(idle), 1)
            # Both land in the same commit; the close event is written with it.
            self.assertLessEqual(idle[0], completed[0])
            self.assertLessEqual(completed[0] - idle[0], 2)
        finally:
            await client.close()

    async def test_unrelated_input_still_continues_the_conversation(self):
        application, client, root = await self._client()
        try:
            first = dict((await self._send(client, root, "compare two frameworks", input_id="i1")).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)
            await self._send(client, root, "无关的新话题：today weather", input_id="i2")
            await client.wait_task(task_id, timeout=5)

            events = await self._events(application, task_id)
            self.assertEqual(
                [e.event_type for e in events].count("task.created"), 1
            )
        finally:
            await client.close()

    async def test_idle_interrupt_reports_not_running(self):
        application, client, root = await self._client()
        try:
            first = dict((await self._send(client, root, "hello", input_id="i1")).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)
            result = dict((await self._send(
                client, root, "stop", input_id="i2",
                intent=RuntimeInputIntent.INTERRUPT,
            )).result)
            self.assertEqual(result.get("kind"), "not_running")
            task = await application.kernel.get_task(task_id)
            self.assertIs(task.state, TaskState.AWAITING_INPUT)
        finally:
            await client.close()

    async def test_idle_attachment_continues_the_conversation(self):
        application, client, root = await self._client()
        try:
            first = dict((await self._send(client, root, "hello", input_id="i1")).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)

            image = ImageBlock("data:image/png;base64,aGVsbG8=")
            await self._send(
                client, root, "look", input_id="i2", images=(image,)
            )
            await client.wait_task(task_id, timeout=5)

            events = await self._events(application, task_id)
            types = [event.event_type for event in events]
            self.assertEqual(types.count("task.created"), 1)
            self.assertEqual(types.count("turn.started"), 2)
        finally:
            await client.close()

    async def test_explicit_new_task_supersedes_idle_conversation(self):
        application, client, root = await self._client()
        try:
            first = dict((await self._send(client, root, "first", input_id="i1")).result)
            first_id = first["task"]["task_id"]
            await client.wait_task(first_id, timeout=5)

            second = dict((await self._send(
                client, root, "second", input_id="i2",
                intent=RuntimeInputIntent.NEW_TASK,
            )).result)
            second_id = second["task"]["task_id"]
            await client.wait_task(second_id, timeout=5)

            old = await application.kernel.get_task(first_id)
            self.assertIs(old.state, TaskState.SUCCEEDED)
            self.assertNotEqual(first_id, second_id)
            idle = []
            for task in await application.kernel.list_session_tasks("conv"):
                if (
                    task.task_kind is TaskKind.CONVERSATION
                    and task.state is TaskState.AWAITING_INPUT
                ):
                    idle.append(task.task_id)
            self.assertEqual(idle, [second_id])
        finally:
            await client.close()

    async def test_turn_scoped_events_share_one_turn_id(self):
        application, client, root = await self._client()
        try:
            first = dict((await self._send(client, root, "hello", input_id="i1")).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)
            await self._send(client, root, "again", input_id="i2")
            await client.wait_task(task_id, timeout=5)

            events = await self._events(application, task_id)
            scoped = [
                event for event in events
                if event.event_type in {
                    "checkpoint.saved", "llm.completed", "turn.completed",
                    "conversation.model_response_recorded",
                    "conversation.text_completed", "turn.started",
                }
            ]
            self.assertTrue(scoped)
            self.assertTrue(all(event.turn_id for event in scoped))
            # Each turn's events share one id, and the two turns differ.
            self.assertEqual(len({event.turn_id for event in scoped}), 2)
        finally:
            await client.close()

    async def test_continuation_request_returns_without_running_the_turn_inline(self):
        """A later turn is scheduled like a first turn, not awaited in the request.

        The turn runs in the background, so the HTTP request returns as soon as
        the input is accepted even when the model call is still in flight. An
        inline await here is what produced the 500 (local API call timeout).
        """

        model = _GateModel()
        application, client, root = await self._client(model=model)
        try:
            model.gate.set()
            first = dict((await self._send(client, root, "hello", input_id="i1")).result)
            task_id = first["task"]["task_id"]
            await client.wait_task(task_id, timeout=5)
            self.assertEqual(model.entered, 1)

            # The next turn's model call is blocked and the gate stays closed.
            model.gate.clear()
            accepted = await asyncio.wait_for(
                self._send(client, root, "continue", input_id="i2"), timeout=2.0
            )
            self.assertIn(dict(accepted.result).get("kind"), {"continuation", "task"})
            for _ in range(100):
                if model.entered >= 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(model.entered, 2)

            model.gate.set()
            await client.wait_task(task_id, timeout=5)
            events = await self._events(application, task_id)
            types = [event.event_type for event in events]
            self.assertEqual(types.count("task.created"), 1)
            self.assertEqual(types.count("turn.started"), 2)
        finally:
            await client.close()

    async def test_running_input_restarts_same_task_without_a_model_judgement(self):
        model = _BlockingEchoModel()
        judge = _SpyRelationJudge()
        application, client, root = await self._client(model=model, judge=judge)
        try:
            first = dict((await self._send(client, root, "long task", input_id="i1")).result)
            task_id = first["task"]["task_id"]
            await asyncio.wait_for(model.entered.wait(), timeout=5)

            redirected = dict((await self._send(
                client, root, "more context", input_id="i2"
            )).result)
            self.assertEqual(judge.calls, [])
            self.assertEqual(redirected.get("kind"), "continuation")
            self.assertEqual(
                redirected.get("relation"), "INTERRUPT_AND_REDIRECT"
            )
            self.assertEqual(redirected.get("routed_task_id"), task_id)

            model.release.set()
            await client.wait_task(task_id, timeout=5)
            events = await self._events(application, task_id)
            self.assertEqual(
                [e.event_type for e in events].count("task.created"), 1
            )
        finally:
            await client.close()


class ConversationBindingTest(unittest.TestCase):
    """``bind_task_input`` reads only state and kind -- never text."""

    def test_idle_conversation_is_continued(self):
        decision = bind_task_input(TaskBindingRequest(
            current_input_event_ref="input:1",
            tasks=(TaskBindingTask(
                "task-conv", "AWAITING_INPUT", task_kind="CONVERSATION",
            ),),
        ))
        self.assertIs(
            decision.binding_kind, TaskBindingKind.CONVERSATION_CONTINUATION
        )
        self.assertEqual(decision.task_id, "task-conv")

    def test_interrupted_conversation_is_continued(self):
        decision = bind_task_input(TaskBindingRequest(
            current_input_event_ref="input:1",
            tasks=(TaskBindingTask(
                "task-conv", "INTERRUPTED", task_kind="CONVERSATION",
            ),),
        ))
        self.assertIs(
            decision.binding_kind, TaskBindingKind.CONVERSATION_CONTINUATION
        )

    def test_work_item_never_continues_as_a_conversation(self):
        decision = bind_task_input(TaskBindingRequest(
            current_input_event_ref="input:1",
            tasks=(TaskBindingTask(
                "task-work", "AWAITING_INPUT", task_kind="WORK_ITEM",
            ),),
        ))
        self.assertIs(decision.binding_kind, TaskBindingKind.NEW)

    def test_explicit_new_task_wins_over_continuation(self):
        decision = bind_task_input(TaskBindingRequest(
            current_input_event_ref="input:1",
            tasks=(TaskBindingTask(
                "task-conv", "AWAITING_INPUT", task_kind="CONVERSATION",
            ),),
            new_task_requested=True,
        ))
        self.assertIs(decision.binding_kind, TaskBindingKind.NEW)


if __name__ == "__main__":
    unittest.main()
