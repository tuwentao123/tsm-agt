from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.default_session_input_relation import (
    DefaultSessionInputRelationJudge,
)
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import RuntimeInputIntent, TaskState
from tsm_agt.ports import (
    AdapterContext,
    SessionInputRelation,
    SessionInputRelationPort,
)
from tsm_agt.sdk import EngineeringAgentClient


class _BlockingModel(EchoModelProvider):
    """Blocks inside one model call so a Task stays EXECUTING on demand."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request):
        self.entered.set()
        await self.release.wait()
        return await super().complete(request)


def _client(root: Path, model=None) -> EngineeringAgentClient:
    application = compose_fixture_application(
        model_adapter=model or EchoModelProvider(), tool_adapters=(),
    )
    return EngineeringAgentClient(
        root, application_factory=lambda: application, poll_interval=0.001,
    )


async def _finish_running_tasks(client: EngineeringAgentClient, session_id: str) -> None:
    session = await client.application.kernel.get_session(session_id)
    interruptible = {
        TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW,
        TaskState.AWAITING_APPROVAL, TaskState.AWAITING_USER,
    }
    for task_id in session.task_ids:
        task = await client.application.kernel.get_task(task_id)
        if task.state in interruptible:
            await client.interrupt(
                task_id, command_id=f"cleanup-{task_id}", reason="test cleanup",
            )


class DefaultSessionInputRelationJudgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_judge_always_supplements(self) -> None:
        judge = DefaultSessionInputRelationJudge()
        await judge.start(AdapterContext(config={}, emit_event=lambda *_a: None))
        judgement = await judge.judge_input_relation("anything", {})
        self.assertIs(judgement.relation, SessionInputRelation.SUPPLEMENT)
        self.assertEqual(judgement.confidence, 1.0)
        self.assertEqual(judgement.reason_code, "default_supplement")

    async def test_composition_registers_the_default_judge(self) -> None:
        application = compose_fixture_application(
            model_adapter=EchoModelProvider(), tool_adapters=(),
        )
        judge = application.registry.require(SessionInputRelationPort)
        self.assertIsInstance(judge, DefaultSessionInputRelationJudge)


class UnifiedUserInputTest(unittest.IsolatedAsyncioTestCase):
    async def test_plain_input_starts_a_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with _client(Path(directory)) as client:
                result = await client.submit_user_input(
                    "s1", "hello", input_id="u1"
                )
                data = result.result
                self.assertEqual(data["kind"], "task")
                final = await client.wait_task(
                    data["task"]["task_id"], timeout=5
                )
                self.assertEqual(final.state, TaskState.SUCCEEDED.value)
                self.assertEqual(final.assistant_text, "hello")

    async def test_running_task_receives_supplement_instead_of_new_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = _BlockingModel()
            async with _client(Path(directory), model) as client:
                first = await client.submit_user_input(
                    "s1", "work", input_id="u1"
                )
                task_id = first.result["task"]["task_id"]
                await asyncio.wait_for(model.entered.wait(), 5)

                second = await client.submit_user_input(
                    "s1", "also do this", input_id="u2"
                )
                self.assertEqual(second.result["kind"], "steered")
                self.assertEqual(second.result["relation"], "SUPPLEMENT")
                self.assertEqual(second.result["routed_task_id"], task_id)

                session = await client.application.kernel.get_session("s1")
                self.assertEqual(session.task_ids, (task_id,))
                await _finish_running_tasks(client, "s1")

    async def test_explicit_new_task_stops_the_old_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = _BlockingModel()
            async with _client(Path(directory), model) as client:
                first = await client.submit_user_input(
                    "s1", "work", input_id="u1"
                )
                old_id = first.result["task"]["task_id"]
                await asyncio.wait_for(model.entered.wait(), 5)

                second = await client.submit_user_input(
                    "s1", "different work", input_id="u2",
                    explicit_intent=RuntimeInputIntent.NEW_TASK,
                )
                new_id = second.result["task"]["task_id"]
                self.assertNotEqual(old_id, new_id)

                old = await client.application.kernel.get_task(old_id)
                self.assertEqual(old.state, TaskState.INTERRUPTED)
                await _finish_running_tasks(client, "s1")

    async def test_explicit_interrupt_stops_the_running_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = _BlockingModel()
            async with _client(Path(directory), model) as client:
                first = await client.submit_user_input(
                    "s1", "work", input_id="u1"
                )
                task_id = first.result["task"]["task_id"]
                await asyncio.wait_for(model.entered.wait(), 5)

                stopped = await client.submit_user_input(
                    "s1", "", input_id="u-stop",
                    explicit_intent=RuntimeInputIntent.INTERRUPT,
                    target_task_id=task_id,
                )
                self.assertEqual(stopped.result["kind"], "interrupted")
                task = await client.application.kernel.get_task(task_id)
                self.assertEqual(task.state, TaskState.INTERRUPTED)

    async def test_concurrent_input_keeps_a_single_running_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = _BlockingModel()
            async with _client(Path(directory), model) as client:
                await asyncio.gather(
                    client.submit_user_input("s1", "one", input_id="u1"),
                    client.submit_user_input("s1", "two", input_id="u2"),
                )
                session = await client.application.kernel.get_session("s1")
                tasks = [
                    await client.application.kernel.get_task(task_id)
                    for task_id in session.task_ids
                ]
                running = [
                    task for task in tasks
                    if task.state in {
                        TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW,
                    }
                ]
                self.assertLessEqual(len(running), 1)
                await _finish_running_tasks(client, "s1")
