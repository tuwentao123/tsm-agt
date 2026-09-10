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
from tsm_agt.cli import (
    _chat, _chat_error_guidance, _resolve_session_input_with_progress,
)
from tsm_agt.core import ModelInvocationFailed, ProjectTrustLevel, TaskState
from tsm_agt.ports import RuntimeStorePort
from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, FinishReason, HealthState, HealthStatus,
    Message, MessageRole, ModelRequest, ModelResponse, ModelUsage,
    ProviderCapabilities, TextBlock, ToolCall, ToolCallBlock, ToolResultBlock,
    ToolIdempotency, ToolRisk, ToolSpec, ToolResult,
    ModelFailureCategory, ModelRecoveryAction, ModelRetrySafety,
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
        self.requests: list[ModelRequest] = []

    async def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        if self.calls == 1:
            raise ConnectionError("fixture connection dropped")
        return await super().complete(request)


class FailFirstNChatModel(EchoModelProvider):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("fixture interruption")
        return await super().complete(request)


class RecordingChatModel(EchoModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


class FixtureSessionInputResolver:
    descriptor = AdapterDescriptor(
        "fixture.session-input-resolver", "1.0",
        "SessionInputResolverPort", "1.0",
    )

    def __init__(self, *, new_task_inputs: tuple[str, ...] = ()) -> None:
        self.new_task_inputs = set(new_task_inputs)
        self.contexts: list[dict] = []

    async def start(self, context):
        pass

    async def health(self):
        return HealthStatus(HealthState.HEALTHY, "ready")

    async def stop(self, deadline):
        pass

    async def resolve_session_input(self, text, context):
        self.contexts.append(dict(context))
        if text in self.new_task_inputs:
            return {
                "action": "NEW_TASK", "task_id": None,
                "input_grounding": "SELF_CONTAINED",
                "confidence": 0.99, "reason_code": "fixture_new_task",
                "clarification": None,
            }
        return {
            "action": "RESUME_TASK",
            "input_grounding": "CONTEXT_DEPENDENT",
            "task_id": context["unfinished_tasks"][0]["task_id"],
            "confidence": 0.99, "reason_code": "fixture_resume",
            "clarification": None,
        }


class ClarifyingSessionInputResolver(FixtureSessionInputResolver):
    async def resolve_session_input(self, text, context):
        self.contexts.append(dict(context))
        return {
            "action": "CLARIFY", "task_id": None,
            "input_grounding": "AMBIGUOUS",
            "confidence": 0.99, "reason_code": "multiple_candidates",
            "clarification": "请选择要继续的未完成 Task。",
        }


class FixtureRuntimeInputClassifier:
    descriptor = AdapterDescriptor(
        "fixture.runtime-input-classifier", "1.0",
        "RuntimeInputClassifierPort", "1.0",
    )

    async def start(self, context):
        pass

    async def health(self):
        return HealthStatus(HealthState.HEALTHY)

    async def stop(self, deadline):
        pass

    async def classify_runtime_input(self, text, context):
        return {"intent": "STEER", "confidence": 0.99}


class FixtureRuntimeInputClassifier:
    descriptor = AdapterDescriptor(
        "fixture.runtime-input-classifier", "1.0",
        "RuntimeInputClassifierPort", "1.0",
    )

    async def start(self, context):
        pass

    async def health(self):
        return HealthStatus(HealthState.HEALTHY)

    async def stop(self, deadline):
        pass

    async def classify_runtime_input(self, text, context):
        return {"intent": "STEER", "confidence": 0.99}


class InteractiveChatCliTest(unittest.IsolatedAsyncioTestCase):
    async def test_clarify_then_fourth_choice_does_not_call_resolver_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = FailFirstNChatModel(4)
            resolver = ClarifyingSessionInputResolver()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=(),
                session_input_resolver_adapter=resolver,
                store_adapter=SQLiteRuntimeStore(root / "runtime.db"),
            )
            await application.registry.start_all()
            session = await application.kernel.create_session("clarify journey")
            try:
                task_ids = []
                for index in range(1, 5):
                    task = await application.kernel.create_task(
                        f"unfinished goal {index}", root,
                        session_id=session.session_id,
                    )
                    task_ids.append(task.task_id)
                    for state in (
                        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                        TaskState.EXECUTING,
                    ):
                        task = await application.kernel.transition_task(
                            task.task_id, state, state.value
                        )
                    with self.assertRaises(ModelInvocationFailed):
                        await application.kernel.run_agent_turn(
                            task.task_id, task.goal
                        )
                displayed = await application.kernel.list_session_resume_candidates(
                    session.session_id, root
                )
                displayed_fourth_id = displayed[3].task_id
            finally:
                await application.registry.stop_all()

            output: list[str] = []
            result = await _chat(
                root, session_id=session.session_id,
                input_fn=ScriptedInput(["继续呢", "第四个吧", "/exit"]),
                output_fn=output.append, application_factory=lambda: application,
            )
            self.assertEqual(result, 0)
            self.assertEqual(len(resolver.contexts), 1)
            self.assertTrue(any(line.startswith("4. ") for line in output))
            self.assertTrue(any(
                "编号选择不调用模型" in line for line in output
            ))
            self.assertTrue(any(
                "option-4" in line for line in output
            ))
            self.assertIn(f"task: {displayed_fourth_id}", output)
            await application.registry.start_all()
            try:
                events = await application.kernel.dependencies.store.read_session_events(
                    session.session_id
                )
                event_types = [event.event_type for event in events]
                self.assertIn("session.interaction_requested", event_types)
                self.assertIn("session.interaction_answered", event_types)
            finally:
                await application.registry.stop_all()

    async def test_persisted_fourth_choice_resumes_without_semantic_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = FailFirstNChatModel(4)
            resolver = FixtureSessionInputResolver()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=(),
                session_input_resolver_adapter=resolver,
                store_adapter=SQLiteRuntimeStore(root / "runtime.db"),
            )
            await application.registry.start_all()
            session = await application.kernel.create_session("choice journey")
            try:
                for index in range(1, 5):
                    task = await application.kernel.create_task(
                        f"unfinished goal {index}", root,
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
                    with self.assertRaises(ModelInvocationFailed):
                        await application.kernel.run_agent_turn(
                            task.task_id, task.goal
                        )
                candidates = await application.kernel.list_session_resume_candidates(
                    session.session_id, root
                )
                fourth_id = candidates[3].task_id
            finally:
                await application.registry.stop_all()

            output: list[str] = []
            result = await _chat(
                root, session_id=session.session_id,
                input_fn=ScriptedInput(["/resume", "第四个吧", "/exit"]),
                output_fn=output.append, application_factory=lambda: application,
            )
            self.assertEqual(result, 0)
            self.assertEqual(resolver.contexts, [])
            self.assertTrue(any(
                line.startswith("4. ") for line in output
            ))
            self.assertTrue(any(
                "option-4" in line for line in output
            ))
            self.assertIn(f"task: {fourth_id}", output)
            await application.registry.start_all()
            try:
                selected = await application.kernel.get_task(fourth_id)
                self.assertEqual(selected.state, TaskState.SUCCEEDED)
                events = await application.kernel.dependencies.store.read_session_events(
                    session.session_id
                )
                self.assertTrue(any(
                    event.event_type == "session.interaction_answered"
                    and event.payload["target_id"] == fourth_id
                    for event in events
                ))
            finally:
                await application.registry.stop_all()

    async def test_session_routing_wait_is_visible_until_result(self):
        class Kernel:
            async def resolve_session_input(self, session_id, prompt, root):
                await asyncio.sleep(0.035)
                return "resolved"

        class Application:
            kernel = Kernel()

        output: list[str] = []
        result = await _resolve_session_input_with_progress(
            Application(), "session-1", "ambiguous", Path("."),
            output.append, heartbeat_seconds=0.01,
        )
        self.assertEqual(result, "resolved")
        self.assertTrue(output[0].startswith("[会话] 正在识别"))
        self.assertTrue(any(
            "语义识别仍在进行" in line for line in output
        ))

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
        exhausted = ModelInvocationFailed(
            "turn-recovery", "provider attempts exhausted",
            failure_category=ModelFailureCategory.INVALID_RESPONSE,
            retry_safety=ModelRetrySafety.SAFE_RESAMPLE,
            recovery_action=ModelRecoveryAction.PRESERVE_AND_INTERRUPT,
        )
        guidance = _chat_error_guidance(exhausted, root)[0]
        self.assertIn("checkpoint", guidance)
        self.assertIn("/resume", guidance)
        self.assertNotIn("doctor --model-check", guidance)

    async def test_interactive_chat_accepts_runtime_input_while_model_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = BlockingChatModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=(),
                runtime_input_classifier_adapter=FixtureRuntimeInputClassifier(),
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

    async def test_semantic_resolver_resumes_same_task_after_unexpected_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = FailOnceChatModel()
            resolver = FixtureSessionInputResolver()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=(),
                session_input_resolver_adapter=resolver,
            )
            output: list[str] = []
            result = await _chat(
                root, input_fn=ScriptedInput([
                    "inspect the original target", "继续", "/exit",
                ]),
                output_fn=output.append, application_factory=lambda: application,
            )
            self.assertEqual(result, 0)
            self.assertTrue(any(
                '"text":"继续"' in line or '"text": "继续"' in line
                for line in output if line.startswith("agent> " )
            ))
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
            self.assertEqual(resolver.contexts[-1]["unfinished_tasks"][0][
                "goal"
            ], "inspect the original target")
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

    async def test_semantic_reference_finds_older_interrupted_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = FailOnceChatModel()
            resolver = FixtureSessionInputResolver(
                new_task_inputs=("answer an unrelated question",)
            )
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=(),
                session_input_resolver_adapter=resolver,
            )
            output: list[str] = []
            await _chat(
                root, input_fn=ScriptedInput([
                    "inspect the interrupted target",
                    "answer an unrelated question",
                    "return to the unfinished investigation and finish it",
                    "/exit",
                ]), output_fn=output.append,
                application_factory=lambda: application,
            )
            session_id = next(
                line.removeprefix("session: ") for line in output
                if line.startswith("session: ")
            )
            await application.registry.start_all()
            try:
                tasks = await application.kernel.list_session_tasks(session_id)
                self.assertEqual(len(tasks), 2)
                self.assertEqual(
                    [task.goal for task in tasks],
                    [
                        "inspect the interrupted target",
                        "answer an unrelated question",
                    ],
                )
                self.assertEqual(
                    [task.state for task in tasks],
                    [TaskState.SUCCEEDED, TaskState.SUCCEEDED],
                )
                self.assertTrue(any(
                    "正在继续上次意外中断的任务：inspect the interrupted target"
                    in line for line in output
                ))
                self.assertEqual(
                    resolver.contexts[-1]["unfinished_tasks"][0]["goal"],
                    "inspect the interrupted target",
                )
            finally:
                await application.registry.stop_all()

    async def test_resumed_task_receives_complete_current_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = FailOnceChatModel()
            resolver = FixtureSessionInputResolver()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=(),
                session_input_resolver_adapter=resolver,
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
                "已将本轮用户原文加入任务上下文" in line for line in output
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
                events = await application.kernel.dependencies.store.read_events(
                    tasks[0].task_id
                )
                queued = [
                    event for event in events
                    if event.event_type == "steering.queued"
                ]
                self.assertEqual(
                    queued[-1].payload["text"],
                    "重新试试，但不要改公共组件",
                )
            finally:
                await application.registry.stop_all()

    async def test_queue_mode_runs_follow_up_as_next_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = BlockingChatModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=(),
                runtime_input_classifier_adapter=FixtureRuntimeInputClassifier(),
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

    async def test_continue_in_empty_session_is_ordinary_model_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = RecordingChatModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            output: list[str] = []
            await _chat(
                root, input_fn=ScriptedInput(["继续", "/exit"]),
                output_fn=output.append, application_factory=lambda: application,
            )
            self.assertIn("agent> 继续", output)
            self.assertEqual(len(model.requests), 1)
            self.assertEqual(model.requests[0].messages[-1].text, "继续")
            self.assertFalse(any(
                message.message_id.startswith("session-context-")
                for message in model.requests[0].messages
            ))
            session_id = next(
                line.removeprefix("session: ") for line in output
                if line.startswith("session: ")
            )
            await application.registry.start_all()
            try:
                tasks = await application.kernel.list_session_tasks(session_id)
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0].goal, "继续")
                self.assertEqual(tasks[0].state, TaskState.SUCCEEDED)
            finally:
                await application.registry.stop_all()

    async def test_continue_after_completed_task_is_a_contextual_follow_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = RecordingChatModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            output: list[str] = []
            await _chat(
                root, input_fn=ScriptedInput([
                    "先查清字段来源", "继续查", "/exit",
                ]),
                output_fn=output.append, application_factory=lambda: application,
            )
            self.assertEqual(len(model.requests), 2)
            follow_up = model.requests[-1]
            self.assertEqual(follow_up.messages[-1].text, "继续查")
            session_context = [
                message for message in follow_up.messages
                if message.message_id.startswith("session-context-")
            ]
            self.assertEqual(len(session_context), 1)
            self.assertIn("先查清字段来源", session_context[0].text)
            session_id = next(
                line.removeprefix("session: ") for line in output
                if line.startswith("session: ")
            )
            await application.registry.start_all()
            try:
                tasks = await application.kernel.list_session_tasks(session_id)
                self.assertEqual(
                    [task.goal for task in tasks],
                    ["先查清字段来源", "继续查"],
                )
                self.assertEqual(
                    [task.state for task in tasks],
                    [TaskState.SUCCEEDED, TaskState.SUCCEEDED],
                )
            finally:
                await application.registry.stop_all()

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
            self.assertIn("continuation: NONE", output)
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
