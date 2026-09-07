from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ApprovalDecision, TaskState
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ModelUsage, ProviderCapabilities, TextBlock, ToolCall,
    ToolCallBlock, ToolIdempotency, ToolResult, ToolResultBlock, ToolRisk, ToolSpec,
)
from tsm_agt.sdk import EngineeringAgentClient


def fixture_client(root: Path, store=None) -> EngineeringAgentClient:
    application = compose_fixture_application(
        model_adapter=EchoModelProvider(), tool_adapters=(),
        store_adapter=store,
    )
    return EngineeringAgentClient(
        root, application_factory=lambda: application, poll_interval=0.001
    )


class BlockingModel(EchoModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.entered.set()
        await self.release.wait()
        return await super().complete(request)


class ApprovalModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        result = next((
            block.result for message in reversed(request.messages)
            for block in message.content if isinstance(block, ToolResultBlock)
        ), None)
        if result is None:
            return ModelResponse(Message(
                "sdk-approval-call", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "sdk-approval-call", "fixture.sdk_approval", {}
                )),),
            ), FinishReason.TOOL_CALL, ModelUsage(1, 1))
        return ModelResponse(Message(
            "sdk-approval-final", MessageRole.ASSISTANT,
            (TextBlock("approval resolved"),),
        ), FinishReason.STOP, ModelUsage(1, 1))


class ApprovalTool:
    descriptor = AdapterDescriptor(
        "fixture.sdk-approval", "1.0", "ToolProviderPort", "1.0"
    )

    async def start(self, context):
        pass

    async def stop(self, deadline):
        pass

    async def health(self):
        from tsm_agt.ports import HealthState, HealthStatus
        return HealthStatus(HealthState.HEALTHY)

    async def list_tools(self):
        return (ToolSpec(
            "fixture.sdk_approval", "approval test",
            {"type": "object", "properties": {},
             "additionalProperties": False},
            ToolRisk.R1, idempotency=ToolIdempotency.KEYED,
        ),)

    async def invoke(self, call, context):
        return ToolResult(call.call_id, True, {"approved": True})


class FailingModel(EchoModelProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise RuntimeError("private provider detail must not escape")


class PythonSdkRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_input_routes_and_command_replays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = fixture_client(root)
            async with client:
                task = await client.create_task(
                    "runtime input", command_id="sdk-input-create"
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    await client.application.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                first = await client.route_input(
                    task.task_id, "记得兼容 Windows",
                    command_id="sdk-input-1",
                )
                replay = await client.route_input(
                    task.task_id, "记得兼容 Windows",
                    command_id="sdk-input-1",
                )
                self.assertEqual(first.result["intent"], "STEER")
                self.assertTrue(first.result["applied"])
                self.assertTrue(replay.replayed)

    async def test_background_failure_converges_task_instead_of_hanging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=FailingModel(), tool_adapters=()
            )
            client = EngineeringAgentClient(
                root, application_factory=lambda: application, poll_interval=0.001
            )
            async with client:
                accepted = await client.submit_task(
                    "fail safely", command_id="sdk-failing-create"
                )
                final = await client.wait_task(accepted.task_id, timeout=5)
                self.assertEqual(final.state, TaskState.FAILED.value)
                self.assertNotIn(
                    "private provider detail", str(final.to_data())
                )

    async def test_concurrent_duplicate_submit_starts_only_one_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = BlockingModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            client = EngineeringAgentClient(
                root, application_factory=lambda: application, poll_interval=0.001
            )
            async with client:
                first, second = await asyncio.gather(
                    client.submit_task(
                        "one run", command_id="sdk-concurrent-create"
                    ),
                    client.submit_task(
                        "one run", command_id="sdk-concurrent-create"
                    ),
                )
                self.assertEqual(first.task_id, second.task_id)
                self.assertEqual(len(client._run_tasks), 1)
                await asyncio.wait_for(model.entered.wait(), 5)
                await client.interrupt(
                    first.task_id, command_id="sdk-concurrent-interrupt",
                    reason="finish duplicate submit test",
                )

    async def test_submit_wait_and_cursor_resume_expose_no_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with fixture_client(root) as client:
                accepted = await client.submit_task(
                    "hello SDK", command_id="sdk-create-1"
                )
                final = await client.wait_task(accepted.task_id, timeout=5)
                self.assertEqual(final.state, TaskState.SUCCEEDED.value)
                self.assertEqual(final.assistant_text, "hello SDK")
                first = await client.read_events(final.task_id, after=0, limit=2)
                rest = await client.read_events(
                    final.task_id, after=first[-1].cursor
                )
                all_events = first + rest
                self.assertEqual(
                    [event.cursor for event in all_events],
                    list(range(1, final.cursor + 1)),
                )
                self.assertFalse(any(
                    "payload" in event.to_data() for event in all_events
                ))

    async def test_subscribe_from_acknowledged_cursor_has_no_gap_or_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with fixture_client(root) as client:
                task = await client.create_task(
                    "stream", command_id="sdk-stream-create"
                )
                runner = asyncio.create_task(client.run_task(task.task_id, "stream"))
                cursors = []
                async for event in client.subscribe_events(
                    task.task_id, after=0, timeout=5
                ):
                    cursors.append(event.cursor)
                final = await runner
                self.assertEqual(final.state, TaskState.SUCCEEDED.value)
                self.assertEqual(cursors, list(range(1, final.cursor + 1)))
                resumed = [event.cursor async for event in client.subscribe_events(
                    task.task_id, after=cursors[-1], timeout=0.01
                )]
                self.assertEqual(resumed, [])

    async def test_live_progress_subscription_is_concrete_and_ephemeral(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with fixture_client(root) as client:
                task = await client.create_task(
                    "显示真实进度目标", command_id="sdk-progress-create"
                )
                runner = asyncio.create_task(client.run_task(
                    task.task_id, "显示真实进度目标"
                ))
                progress = [
                    item async for item in client.subscribe_progress(
                        task.task_id, after=0, timeout=5
                    )
                ]
                final = await runner
                self.assertEqual(final.state, TaskState.SUCCEEDED.value)
                self.assertTrue(progress)
                self.assertEqual(
                    [item.sequence for item in progress],
                    list(range(1, len(progress) + 1)),
                )
                self.assertTrue(all(
                    item.progress.goal == "显示真实进度目标"
                    for item in progress
                ))
                self.assertEqual(
                    client.read_progress(
                        task.task_id, after=progress[-1].sequence
                    ), ()
                )
                events = await client.read_events(task.task_id)
                self.assertTrue(all(
                    "goal" not in event.to_data() for event in events
                ))

    async def test_create_command_id_is_idempotent_and_conflicts_on_changed_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with fixture_client(root) as client:
                first = await client.create_task(
                    "same", command_id="sdk-create-idempotent"
                )
                second = await client.create_task(
                    "same", command_id="sdk-create-idempotent"
                )
                self.assertEqual(first.task_id, second.task_id)
                with self.assertRaisesRegex(ValueError, "different Task input"):
                    await client.create_task(
                        "changed", command_id="sdk-create-idempotent"
                    )

    async def test_control_command_receipt_survives_sqlite_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first = fixture_client(root, SQLiteRuntimeStore(database))
            async with first:
                task = await first.create_task(
                    "interrupt me", command_id="sdk-create-interrupt"
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    await first.application.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                original = await first.interrupt(
                    task.task_id, command_id="sdk-interrupt-1",
                    reason="external client requested safe stop",
                )
                self.assertFalse(original.replayed)
            second = fixture_client(root, SQLiteRuntimeStore(database))
            async with second:
                replayed = await second.interrupt(
                    task.task_id, command_id="sdk-interrupt-1",
                    reason="external client requested safe stop",
                )
                self.assertTrue(replayed.replayed)
                self.assertEqual(replayed.result, original.result)
                with self.assertRaisesRegex(ValueError, "different Runtime command"):
                    await second.interrupt(
                        task.task_id, command_id="sdk-interrupt-1",
                        reason="different request",
                    )

    async def test_invalid_control_requests_do_not_change_waiting_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with fixture_client(root) as client:
                task = await client.create_task(
                    "plain task", command_id="sdk-invalid-controls"
                )
                with self.assertRaises(LookupError):
                    await client.resolve_approval(
                        "missing", ApprovalDecision.APPROVE, "reviewed",
                        command_id="sdk-missing-approval",
                    )
                current = await client.application.kernel.get_task(task.task_id)
                self.assertEqual(current.state, TaskState.CREATED)

    async def test_external_interrupt_cancels_active_runner_after_safe_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = BlockingModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            client = EngineeringAgentClient(
                root, application_factory=lambda: application, poll_interval=0.001
            )
            async with client:
                accepted = await client.submit_task(
                    "block", command_id="sdk-blocking-create"
                )
                await asyncio.wait_for(model.entered.wait(), 5)
                result = await client.interrupt(
                    accepted.task_id, command_id="sdk-blocking-interrupt",
                    reason="user requested stop through SDK",
                )
                self.assertEqual(result.result["state"], "INTERRUPTED")
                current = await client.application.kernel.get_task(accepted.task_id)
                self.assertEqual(current.state, TaskState.INTERRUPTED)
                self.assertIsNotNone(current.active_agent_checkpoint)

    async def test_client_close_safely_interrupts_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            model = BlockingModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=(),
                store_adapter=SQLiteRuntimeStore(database),
            )
            client = EngineeringAgentClient(
                root, application_factory=lambda: application, poll_interval=0.001
            )
            await client.start()
            accepted = await client.submit_task(
                "block until close", command_id="sdk-close-create"
            )
            await asyncio.wait_for(model.entered.wait(), 5)
            await client.close()
            reopened = fixture_client(root, SQLiteRuntimeStore(database))
            async with reopened:
                current = await reopened.application.kernel.get_task(accepted.task_id)
                self.assertEqual(current.state, TaskState.INTERRUPTED)
                self.assertIsNotNone(current.active_agent_checkpoint)

    async def test_approval_command_resumes_same_task_and_replays_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=ApprovalModel(), tool_adapters=(ApprovalTool(),)
            )
            client = EngineeringAgentClient(
                root, application_factory=lambda: application, poll_interval=0.001
            )
            async with client:
                accepted = await client.submit_task(
                    "approval", command_id="sdk-approval-create"
                )
                waiting = await client.wait_task(accepted.task_id, timeout=5)
                self.assertEqual(waiting.status, "awaiting_approval")
                assert waiting.approval is not None
                request_id = str(waiting.approval["request_id"])
                resolved = await client.resolve_approval(
                    request_id, ApprovalDecision.APPROVE,
                    "reviewed exact fixture action",
                    command_id="sdk-approval-resolve",
                )
                self.assertEqual(resolved.result["state"], "SUCCEEDED")
                replayed = await client.resolve_approval(
                    request_id, ApprovalDecision.APPROVE,
                    "reviewed exact fixture action",
                    command_id="sdk-approval-resolve",
                )
                self.assertTrue(replayed.replayed)
                self.assertEqual(replayed.result, resolved.result)


if __name__ == "__main__":
    unittest.main()
