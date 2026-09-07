from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AgentTurnCheckpoint, SteeringKind, TaskState,
)
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ProviderCapabilities, TextBlock, ToolCall, ToolCallBlock,
    ToolIdempotency, ToolResult, ToolRisk, ToolSpec,
)
from tsm_agt.sdk import EngineeringAgentClient


async def executing_task(application, root: Path, task_id: str = "steering-task"):
    task = await application.kernel.create_task(
        "original goal", root, task_id=task_id
    )
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await application.kernel.transition_task(task.task_id, state, state.value)
    return task


class CapturingModel(EchoModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(Message(
            f"answer-{len(self.requests)}", MessageRole.ASSISTANT,
            (TextBlock("updated answer"),),
        ), FinishReason.STOP)


class PendingToolModel(EchoModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(Message(
            "replacement-final", MessageRole.ASSISTANT,
            (TextBlock("replacement observed"),),
        ), FinishReason.STOP)


class MidFlightModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            self.entered.set()
            await self.release.wait()
            return ModelResponse(Message(
                "mid-flight-call", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall("should-not-run", "fixture.safe", {})),),
            ), FinishReason.TOOL_CALL)
        return ModelResponse(Message(
            "mid-flight-final", MessageRole.ASSISTANT,
            (TextBlock("steering merged before tool"),),
        ), FinishReason.STOP)


class FinalMidFlightModel(EchoModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            self.entered.set()
            await self.release.wait()
            text = "answer produced before late steering"
        else:
            text = "answer after late steering"
        return ModelResponse(Message(
            f"late-{len(self.requests)}", MessageRole.ASSISTANT,
            (TextBlock(text),),
        ), FinishReason.STOP)


class CountingTool:
    descriptor = AdapterDescriptor(
        "fixture.counting", "1.0", "ToolProviderPort", "1.0"
    )

    def __init__(self) -> None:
        self.calls = 0

    async def start(self, context):
        pass

    async def stop(self, deadline):
        pass

    async def health(self):
        from tsm_agt.ports import HealthState, HealthStatus
        return HealthStatus(HealthState.HEALTHY)

    async def list_tools(self):
        return (ToolSpec(
            "fixture.safe", "A counted fixture action",
            {"type": "object", "properties": {},
             "additionalProperties": False},
            ToolRisk.R0, idempotency=ToolIdempotency.IDEMPOTENT,
        ),)

    async def invoke(self, call, context):
        self.calls += 1
        return ToolResult(call.call_id, True, {"called": True})


class RuntimeSteeringKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_steering_during_final_model_response_forces_another_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = FinalMidFlightModel()
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            await app.registry.start_all()
            try:
                task = await executing_task(app, root, "late-final-task")
                runner = asyncio.create_task(
                    app.kernel.run_agent_turn(task.task_id, "initial goal")
                )
                await asyncio.wait_for(model.entered.wait(), 5)
                await app.kernel.queue_steering(
                    task.task_id, SteeringKind.STEER,
                    "include the new constraint", "late-steer",
                )
                model.release.set()
                result = await asyncio.wait_for(runner, 5)
                self.assertEqual(
                    result.assistant_message.text, "answer after late steering"
                )
                self.assertEqual(len(model.requests), 2)
                steering = [
                    json.loads(message.text)
                    for message in model.requests[1].messages
                    if message.message_id.startswith("steering-")
                ]
                self.assertEqual(
                    steering[0]["text"], "include the new constraint"
                )
                events = await app.kernel.dependencies.store.read_events(
                    task.task_id
                )
                self.assertEqual(
                    sum(event.event_type == "turn.completed" for event in events), 1
                )
            finally:
                await app.registry.stop_all()

    async def test_mid_model_replace_is_applied_before_returned_tool_executes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = MidFlightModel()
            tool = CountingTool()
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=(tool,)
            )
            await app.registry.start_all()
            try:
                task = await executing_task(app, root, "mid-flight-task")
                runner = asyncio.create_task(
                    app.kernel.run_agent_turn(task.task_id, "old goal")
                )
                await asyncio.wait_for(model.entered.wait(), 5)
                await app.kernel.queue_steering(
                    task.task_id, SteeringKind.REPLACE,
                    "replace while model is running", "mid-flight-replace",
                )
                model.release.set()
                result = await asyncio.wait_for(runner, 5)
                self.assertEqual(
                    result.assistant_message.text, "steering merged before tool"
                )
                self.assertEqual(tool.calls, 0)
                steering = [
                    json.loads(message.text) for message in model.requests[1].messages
                    if message.message_id.startswith("steering-")
                ]
                self.assertEqual(steering[0]["text"], "replace while model is running")
                events = await app.kernel.dependencies.store.read_events(task.task_id)
                applied = next(
                    event for event in events if event.event_type == "steering.applied"
                )
                self.assertEqual(applied.payload["safe_point"], "before-tool-batch")
                self.assertEqual(applied.payload["replaced_pending_tool_calls"], 1)
                self.assertTrue(applied.payload["working_memory_revised"])
                memory = await app.kernel.get_working_memory(task.task_id)
                self.assertEqual(memory.goal, "replace while model is running")
                self.assertEqual(memory.revision, 2)
            finally:
                await app.registry.stop_all()

    async def test_ordered_steering_is_merged_before_model_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = CapturingModel()
            app = compose_fixture_application(model_adapter=model, tool_adapters=())
            await app.registry.start_all()
            try:
                task = await executing_task(app, root)
                await app.kernel.queue_steering(
                    task.task_id, SteeringKind.STEER, "Use concise output", "s-1"
                )
                await app.kernel.queue_steering(
                    task.task_id, SteeringKind.STEER, "Mention verification", "s-2"
                )
                result = await app.kernel.run_agent_turn(task.task_id, "start")
                self.assertEqual(result.assistant_message.text, "updated answer")
                steering_messages = [
                    json.loads(message.text) for message in model.requests[0].messages
                    if message.message_id.startswith("steering-")
                ]
                self.assertEqual(
                    [item["text"] for item in steering_messages],
                    ["Use concise output", "Mention verification"],
                )
                projection = await app.kernel.get_steering(task.task_id)
                self.assertEqual(projection.pending, ())
                self.assertEqual(projection.latest_inbound_sequence, 2)
                events = await app.registry.require(
                    __import__("tsm_agt.ports", fromlist=["RuntimeStorePort"]).RuntimeStorePort
                ).read_events(task.task_id)
                applied = [event for event in events if event.event_type == "steering.applied"]
                self.assertEqual(applied[-1].payload["safe_point"], "before-model")
            finally:
                await app.registry.stop_all()

    async def test_replace_drops_only_unstarted_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = PendingToolModel()
            app = compose_fixture_application(model_adapter=model, tool_adapters=())
            await app.registry.start_all()
            try:
                task = await executing_task(app, root, "replace-task")
                user = Message("original-user", MessageRole.USER, (TextBlock("old"),))
                pending = ToolCall("never-run", "fixture.missing", {})
                checkpoint = AgentTurnCheckpoint(
                    task.task_id, "turn-replace", 1, (user,), (pending,), (),
                    0, 0, 0, 0, 5, 5, 256, 10.0,
                )
                visible = await app.kernel.list_tools()
                checkpoint = await app.kernel._bind_agent_checkpoint(task, checkpoint, visible)
                await app.kernel._save_agent_checkpoint(checkpoint, "test-pending")
                await app.kernel.queue_steering(
                    task.task_id, SteeringKind.REPLACE, "Do the new goal instead", "r-1"
                )
                result = await app.kernel._continue_agent_turn(checkpoint, visible)
                self.assertEqual(result.assistant_message.text, "replacement observed")
                body = next(
                    json.loads(message.text) for message in model.requests[0].messages
                    if message.message_id.startswith("steering-")
                )
                self.assertEqual(body["kind"], "replace")
                events = await app.kernel.dependencies.store.read_events(task.task_id)
                applied = next(
                    event for event in events if event.event_type == "steering.applied"
                )
                self.assertEqual(applied.payload["replaced_pending_tool_calls"], 1)
                self.assertTrue(applied.payload["working_memory_revised"])
                memory = await app.kernel.get_working_memory(task.task_id)
                self.assertEqual(memory.goal, "Do the new goal instead")
                self.assertFalse(any(
                    event.event_type == "tool.requested"
                    and event.payload.get("call", {}).get("call_id") == "never-run"
                    for event in events
                ))
            finally:
                await app.registry.stop_all()

    async def test_queue_is_idempotent_and_rebuilds_after_sqlite_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first = compose_fixture_application(
                model_adapter=CapturingModel(), tool_adapters=(),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await first.registry.start_all()
            try:
                task = await executing_task(first, root, "restart-steering")
                one = await first.kernel.queue_steering(
                    task.task_id, SteeringKind.STEER, "persist me", "same-id"
                )
                two = await first.kernel.queue_steering(
                    task.task_id, SteeringKind.STEER, "persist me", "same-id"
                )
                self.assertEqual(one, two)
                with self.assertRaisesRegex(ValueError, "reused"):
                    await first.kernel.queue_steering(
                        task.task_id, SteeringKind.REPLACE, "changed", "same-id"
                    )
            finally:
                await first.registry.stop_all()
            second = compose_fixture_application(
                model_adapter=CapturingModel(), tool_adapters=(),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await second.registry.start_all()
            try:
                rebuilt = await second.kernel.get_steering("restart-steering")
                self.assertEqual(rebuilt, one)
            finally:
                await second.registry.stop_all()


class RuntimeSteeringSdkTest(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_commands_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                model_adapter=CapturingModel(), tool_adapters=()
            )
            client = EngineeringAgentClient(root, application_factory=lambda: app)
            async with client:
                task = await client.create_task("goal", command_id="create-steer")
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    await app.kernel.transition_task(task.task_id, state, state.value)
                first = await client.steer(
                    task.task_id, "new constraint", command_id="steer-command"
                )
                replay = await client.steer(
                    task.task_id, "new constraint", command_id="steer-command"
                )
                self.assertFalse(first.replayed)
                self.assertTrue(replay.replayed)
                self.assertEqual(first.result, replay.result)


if __name__ == "__main__":
    unittest.main()
