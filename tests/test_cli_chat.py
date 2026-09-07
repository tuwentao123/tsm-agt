from __future__ import annotations

import tempfile
import unittest
import asyncio
from unittest.mock import patch
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.cli import _chat, _chat_error_guidance
from tsm_agt.core import ProjectTrustLevel, TaskState
from tsm_agt.ports import RuntimeStorePort
from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, FinishReason, HealthState, HealthStatus,
    Message, MessageRole, ModelRequest, ModelResponse, ModelUsage,
    ProviderCapabilities, TextBlock, ToolCall, ToolCallBlock, ToolResultBlock,
    ToolIdempotency, ToolRisk, ToolSpec, ToolResult,
)


class ApprovalModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        result = next((
            block.result for message in reversed(request.messages)
            for block in message.content if isinstance(block, ToolResultBlock)
        ), None)
        if result is None:
            return ModelResponse(
                Message(
                    "approval-call", MessageRole.ASSISTANT,
                    (ToolCallBlock(ToolCall(
                        "approval-call", "fixture.confirmed_action", {}
                    )),),
                ), FinishReason.TOOL_CALL, ModelUsage(1, 1),
            )
        return ModelResponse(
            Message(
                "approval-result", MessageRole.ASSISTANT,
                (TextBlock("action approved" if result.ok else "action rejected"),),
            ), FinishReason.STOP, ModelUsage(1, 1),
        )


class ApprovalTool:
    descriptor = AdapterDescriptor(
        "fixture.confirmed-action", "1.0", "ToolProviderPort", "1.0"
    )

    def __init__(self) -> None:
        self.invocations = 0

    async def start(self, context: AdapterContext) -> None:
        pass

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY, "ready")

    async def stop(self, deadline) -> None:
        pass

    async def list_tools(self):
        return (ToolSpec(
            "fixture.confirmed_action", "Perform one approval-gated action",
            {"type": "object", "properties": {}, "additionalProperties": False},
            ToolRisk.R1, idempotency=ToolIdempotency.KEYED,
        ),)

    async def invoke(self, call, context):
        self.invocations += 1
        return ToolResult(call.call_id, True, {"done": True})


class ScriptedInput:
    def __init__(self, values: list[str]) -> None:
        self._values = iter(values)

    def __call__(self, prompt: str) -> str:
        try:
            return next(self._values)
        except StopIteration as error:
            raise EOFError from error


class AsyncScriptedTerminal:
    def __init__(self, values: list[str]) -> None:
        self.values = iter(values)
        self.prompts: list[str] = []
        self.supports_live_input = True

    async def read(self, prompt: str) -> str:
        self.prompts.append(prompt)
        await asyncio.sleep(0)
        try:
            return next(self.values)
        except StopIteration as error:
            raise EOFError from error


class BlockingChatModel(EchoModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(0.03)
        return await super().complete(request)


class FailOnceChatModel(EchoModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("fixture connection dropped")
        return await super().complete(request)


class InteractiveChatCliTest(unittest.IsolatedAsyncioTestCase):
    def test_error_guidance_is_actionable(self):
        root = Path("/fixture")
        self.assertIn(
            "doctor --model-check",
            _chat_error_guidance(RuntimeError("model connection timeout"), root)[0],
        )
        self.assertIn(
            "TRUSTED_BUILD",
            _chat_error_guidance(RuntimeError("PERMISSION_DENIED untrusted"), root)[0],
        )

    async def test_interactive_chat_accepts_runtime_input_while_model_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = BlockingChatModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            terminal = AsyncScriptedTerminal([
                "initial request", "另外不要修改公共 API", "/exit",
            ])
            output: list[str] = []
            with patch("tsm_agt.cli.platform_line_input", return_value=terminal):
                result = await _chat(
                    root, output_fn=output.append,
                    application_factory=lambda: application,
                )
            self.assertEqual(result, 0)
            self.assertTrue(any(
                "accepted additional instruction" in line for line in output
            ))
            self.assertIn("control> ", terminal.prompts)
            session_id = next(
                line.removeprefix("session: ") for line in output
                if line.startswith("session: ")
            )
            await application.registry.start_all()
            try:
                tasks = await application.kernel.list_session_tasks(session_id)
                self.assertEqual(len(tasks), 1)
                events = await application.kernel.dependencies.store.read_events(
                    tasks[0].task_id
                )
                self.assertTrue(any(
                    event.event_type == "runtime_input.routed" for event in events
                ))
            finally:
                await application.registry.stop_all()

    async def test_continue_resumes_same_task_after_unexpected_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = FailOnceChatModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            output: list[str] = []
            result = await _chat(
                root, input_fn=ScriptedInput([
                    "inspect the original target", "继续", "/exit",
                ]),
                output_fn=output.append, application_factory=lambda: application,
            )
            self.assertEqual(result, 0)
            self.assertIn("agent> inspect the original target", output)
            self.assertTrue(any(
                line.startswith("[恢复] 正在继续上次意外中断的任务")
                for line in output
            ))
            session_id = next(
                line.removeprefix("session: ") for line in output
                if line.startswith("session: ")
            )
            task_ids = [
                line.removeprefix("task: ") for line in output
                if line.startswith("task: ")
            ]
            self.assertEqual(len(task_ids), 2)
            self.assertEqual(task_ids[0], task_ids[1])
            await application.registry.start_all()
            try:
                tasks = await application.kernel.list_session_tasks(session_id)
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0].goal, "inspect the original target")
                self.assertEqual(tasks[0].state, TaskState.SUCCEEDED)
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(tasks[0].task_id)
                event_types = [event.event_type for event in events]
                self.assertIn("turn.interrupted", event_types)
                self.assertIn("turn.resumed", event_types)
            finally:
                await application.registry.stop_all()

    async def test_retry_with_steering_resumes_original_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = FailOnceChatModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            output: list[str] = []
            await _chat(
                root, input_fn=ScriptedInput([
                    "original target",
                    "重新试试，但不要改公共组件",
                    "/exit",
                ]), output_fn=output.append,
                application_factory=lambda: application,
            )
            self.assertTrue(any(
                "已加入补充要求" in line for line in output
            ))
            session_id = next(
                line.removeprefix("session: ") for line in output
                if line.startswith("session: ")
            )
            await application.registry.start_all()
            try:
                tasks = await application.kernel.list_session_tasks(session_id)
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0].goal, "original target")
                spec = await application.kernel.get_task_spec(tasks[0].task_id)
                self.assertIn("但不要改公共组件", spec.constraints)
            finally:
                await application.registry.stop_all()

    async def test_queue_mode_runs_follow_up_as_next_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = BlockingChatModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            terminal = AsyncScriptedTerminal([
                "first request", "/followup queue",
                "second request",
            ])
            output: list[str] = []
            with patch("tsm_agt.cli.platform_line_input", return_value=terminal):
                result = await _chat(
                    root, output_fn=output.append,
                    application_factory=lambda: application,
                )
            self.assertEqual(result, 0)
            self.assertTrue(any(
                "[队列] 已保存" in line for line in output
            ))
            self.assertIn("agent> first request", output)
            self.assertIn("agent> second request", output)
            session_id = next(
                line.removeprefix("session: ") for line in output
                if line.startswith("session: ")
            )
            await application.registry.start_all()
            try:
                tasks = await application.kernel.list_session_tasks(session_id)
                self.assertEqual(
                    [task.goal for task in tasks],
                    ["first request", "second request"],
                )
                pending = await application.kernel.list_queued_session_follow_ups(
                    session_id
                )
                self.assertEqual(pending, ())
            finally:
                await application.registry.stop_all()

    async def test_continue_without_checkpoint_does_not_create_a_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=()
            )
            output: list[str] = []
            await _chat(
                root, input_fn=ScriptedInput(["继续", "/exit"]),
                output_fn=output.append, application_factory=lambda: application,
            )
            self.assertTrue(any(
                "没有可以安全恢复" in line for line in output
            ))
            self.assertFalse(any(line.startswith("task: ") for line in output))

    async def test_untrusted_workspace_explains_and_can_continue_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=EchoModelProvider(),
                tool_adapters=(CoreProcessToolProvider(),),
            )
            output: list[str] = []
            result = await _chat(
                root, input_fn=ScriptedInput(["no", "/exit"]),
                output_fn=output.append, application_factory=lambda: application,
            )
            self.assertEqual(result, 0)
            self.assertIn("workspace trust: UNTRUSTED", output)
            self.assertIn(
                "read/search: enabled; project build/test commands: disabled",
                output,
            )
            self.assertTrue(any(
                "tsm-agt trust set TRUSTED_BUILD" in line for line in output
            ))
            await application.registry.start_all()
            try:
                trust = await application.kernel.get_project_trust(root)
                self.assertEqual(trust.level, ProjectTrustLevel.UNTRUSTED)
            finally:
                await application.registry.stop_all()

    async def test_untrusted_workspace_can_grant_trusted_build_before_chat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            application = compose_fixture_application(
                model_adapter=EchoModelProvider(),
                store_adapter=SQLiteRuntimeStore(database),
                tool_adapters=(CoreProcessToolProvider(),),
            )
            output: list[str] = []
            result = await _chat(
                root, input_fn=ScriptedInput(["yes", "hello", "/exit"]),
                output_fn=output.append, application_factory=lambda: application,
            )
            self.assertEqual(result, 0)
            self.assertTrue(any(
                line.startswith("workspace trust: TRUSTED_BUILD")
                for line in output
            ))
            await application.registry.start_all()
            try:
                trust = await application.kernel.get_project_trust(root)
                self.assertEqual(trust.level, ProjectTrustLevel.TRUSTED_BUILD)
                tasks = await application.kernel.list_session_tasks(
                    next(
                        line.removeprefix("session: ") for line in output
                        if line.startswith("session: ")
                    )
                )
                self.assertEqual(tasks[0].project_trust, ProjectTrustLevel.TRUSTED_BUILD)
            finally:
                await application.registry.stop_all()

    async def test_multi_turn_chat_persists_session_tasks_and_visible_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=EchoModelProvider(),
                store_adapter=SQLiteRuntimeStore(root / ".agent" / "runtime.db"),
                tool_adapters=(),
            )
            output: list[str] = []
            result = await _chat(
                root, title="Fixture conversation",
                input_fn=ScriptedInput(["first request", "second request", "/exit"]),
                output_fn=output.append, application_factory=lambda: application,
            )
            self.assertEqual(result, 0)
            self.assertIn("agent> first request", output)
            self.assertIn("agent> second request", output)
            session_id = next(
                line.removeprefix("session: ") for line in output
                if line.startswith("session: ")
            )

            await application.registry.start_all()
            try:
                session = await application.kernel.get_session(session_id)
                self.assertEqual(len(session.task_ids), 2)
                tasks = await application.kernel.list_session_tasks(session_id)
                self.assertEqual(
                    [task.state for task in tasks],
                    [TaskState.SUCCEEDED, TaskState.SUCCEEDED],
                )
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_session_events(session_id)
                results = [
                    event for event in events
                    if event.event_type == "session.task_result_recorded"
                ]
                self.assertEqual(len(results), 2)
                self.assertEqual(
                    results[0].payload["user_message"]["content"][0]["text"],
                    "first request",
                )
                self.assertEqual(
                    results[1].payload["assistant_message"]["content"][0]["text"],
                    "second request",
                )
            finally:
                await application.registry.stop_all()

    async def test_chat_resumes_existing_session_and_eof_exits_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=()
            )
            first_output: list[str] = []
            await _chat(
                root, input_fn=ScriptedInput(["one", "/exit"]),
                output_fn=first_output.append, application_factory=lambda: application,
            )
            session_id = next(
                line.removeprefix("session: ") for line in first_output
                if line.startswith("session: ")
            )
            second_output: list[str] = []
            await _chat(
                root, session_id=session_id, input_fn=ScriptedInput(["two"]),
                output_fn=second_output.append, application_factory=lambda: application,
            )
            self.assertIn("agent> two", second_output)
            self.assertTrue(any(
                line.startswith("session saved:") for line in second_output
            ))
            await application.registry.start_all()
            try:
                self.assertEqual(
                    len((await application.kernel.get_session(session_id)).task_ids), 2
                )
            finally:
                await application.registry.stop_all()

    async def test_chat_commands_do_not_create_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=()
            )
            output: list[str] = []
            await _chat(
                root, input_fn=ScriptedInput([
                    "/help", "/session", "/sessions", "/permissions",
                    "/status", "/plan", "/spec", "/diff", "/tasks", "/flow",
                    "/unknown", "/exit",
                ]), output_fn=output.append, application_factory=lambda: application,
            )
            self.assertIn("no tasks", output)
            self.assertTrue(any("unknown command" in line for line in output))
            self.assertFalse(any(line.startswith("task: ") for line in output))
            self.assertTrue(any(line.startswith("project trust:") for line in output))
            self.assertEqual(output.count("no active task in this session"), 4)

    async def test_status_plan_and_diff_inspect_existing_task_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = BlockingChatModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            output: list[str] = []
            await _chat(
                root, input_fn=ScriptedInput([
                    "first task", "/status", "/plan", "/spec", "/diff", "/exit",
                ]), output_fn=output.append, application_factory=lambda: application,
            )
            self.assertEqual(model.calls, 1)
            self.assertTrue(any(line.startswith("status: session=") for line in output))
            self.assertIn("goal: first task", output)
            self.assertTrue(any(line.startswith("Task SPEC r1:") for line in output))
            self.assertTrue(any(line.startswith("diff: +") for line in output))

    async def test_chat_handles_approval_without_leaving_repl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = ApprovalTool()
            application = compose_fixture_application(
                model_adapter=ApprovalModel(), tool_adapters=(tool,)
            )
            output: list[str] = []
            await _chat(
                root, input_fn=ScriptedInput(["change it", "yes", "/exit"]),
                output_fn=output.append, application_factory=lambda: application,
            )
            self.assertIn("approval required", output)
            self.assertIn("agent> action approved", output)
            self.assertEqual(tool.invocations, 1)


if __name__ == "__main__":
    unittest.main()
