from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.adapters.fixture import EchoModelProvider, EchoToolProvider
from tsm_agt.adapters.local_process import LocalProcessExecutor
from tsm_agt.adapters.local_sandbox import LocalWorkspaceSandbox
from tsm_agt.adapters.posix_path import PosixWorkspacePath
from tsm_agt.adapters.rule_based_completion_readiness import (
    RuleBasedCompletionReadinessPolicy,
)
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AgentContinuationSuspended, AgentTurnCheckpoint, AgentTurnResult,
    AgentTurnSuspended, ApprovalDecision, ProjectTrustLevel, TaskState,
)
from tsm_agt.ports import (
    CompletionGap, CompletionReadinessAction, CompletionReadinessProbe,
    CompletionReadinessState, EvidenceQuestion, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ModelStreamCompleted, ModelTextDelta, ModelUsage,
    ProviderCapabilities, RuntimeStorePort, TextBlock, ToolCall, ToolCallBlock,
    ToolEffect, ToolInvocationContext, ToolResult, ToolResultBlock,
)


class RecoverableReadTool(EchoToolProvider):
    """Read-only fixture that fails a configurable number of attempts."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext,
    ) -> ToolResult:
        self.attempts += 1
        if self.attempts <= self.failures:
            return ToolResult(
                call.call_id, False, error_code="PATH_CONTEXT_REQUIRED",
                message="A more precise path is required.", retryable=True,
                meta={"recoverable_input": True},
            )
        return await super().invoke(call, context)


class BlockedReadTool(EchoToolProvider):
    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext,
    ) -> ToolResult:
        return ToolResult(
            call.call_id, False, error_code="PERMISSION_DENIED",
            message="The required source is not authorized.",
        )


class ReadinessSequenceModel(EchoModelProvider):
    """Proposes an early final, then follows Runtime completion feedback."""

    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    def __init__(self) -> None:
        super().__init__()
        self.tool_calls = 0
        self.requests: list[ModelRequest] = []

    def _response(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        results = [
            block.result
            for message in request.messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        correction = next((
            message.text for message in reversed(request.messages)
            if message.role is MessageRole.USER
            and '"boundary":"completion_readiness"' in message.text
        ), "")
        if '"action":"REPORT_BLOCKED"' in correction:
            return ModelResponse(
                Message(
                    "blocked-final", MessageRole.ASSISTANT,
                    (TextBlock("Blocked: permission denied; source remains unverified."),),
                ),
                FinishReason.STOP, ModelUsage(1, 1),
            )
        if '"action":"REPORT_INCOMPLETE_RECOVERABLE"' in correction:
            return ModelResponse(
                Message(
                    "incomplete-final", MessageRole.ASSISTANT,
                    (TextBlock("Incomplete: required source remains unverified."),),
                ),
                FinishReason.STOP, ModelUsage(1, 1),
            )
        if results and results[-1].ok:
            return ModelResponse(
                Message(
                    "complete-final", MessageRole.ASSISTANT,
                    (TextBlock("Required evidence collected."),),
                ),
                FinishReason.STOP, ModelUsage(1, 1),
            )
        if not results or (
            '"action":"CONTINUE"' in correction
            and self.tool_calls < 2
        ):
            self.tool_calls += 1
            call = ToolCall(
                f"read-{self.tool_calls}", "fixture.echo",
                {"text": "evidence"},
                EvidenceQuestion(
                    "Q-required", "What fact is required for the answer?",
                    expected_scope=".",
                ),
            )
            return ModelResponse(
                Message(
                    f"tool-{self.tool_calls}", MessageRole.ASSISTANT,
                    (ToolCallBlock(call),),
                ),
                FinishReason.TOOL_CALL, ModelUsage(1, 1),
            )
        return ModelResponse(
            Message(
                f"premature-{len(results)}", MessageRole.ASSISTANT,
                (TextBlock("I can continue later if needed."),),
            ),
            FinishReason.STOP, ModelUsage(1, 1),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return self._response(request)


class StreamingReadinessModel(ReadinessSequenceModel):
    capabilities = ProviderCapabilities(
        tools=True, stream_cancel=True, context_window=8192
    )

    async def stream_complete(self, request: ModelRequest):
        response = self._response(request)
        if response.message.text:
            yield ModelTextDelta(response.message.text)
        yield ModelStreamCompleted(response)


class PostMutationVerificationModel(EchoModelProvider):
    """Propose an early final, then execute the capability named by the gap."""

    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result
            for message in request.messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        correction = next((
            message.text for message in reversed(request.messages)
            if message.role is MessageRole.USER
            and '"boundary":"completion_readiness"' in message.text
        ), "")
        if results:
            return ModelResponse(
                Message(
                    "verified-final", MessageRole.ASSISTANT,
                    (TextBlock("Change verified successfully."),),
                ),
                FinishReason.STOP, ModelUsage(1, 1),
            )
        if '"action":"CONTINUE"' in correction:
            return ModelResponse(
                Message(
                    "verification-tool", MessageRole.ASSISTANT,
                    (ToolCallBlock(ToolCall(
                        "verify-change", "core.run_command", {
                            "argv": [sys.executable, "-m", "compileall", "changed.py"],
                            "mode": "foreground", "timeout_seconds": 30,
                        },
                    )),),
                ),
                FinishReason.TOOL_CALL, ModelUsage(1, 1),
            )
        return ModelResponse(
            Message(
                "early-final", MessageRole.ASSISTANT,
                (TextBlock("Change written; verification can be run later."),),
            ),
            FinishReason.STOP, ModelUsage(1, 1),
        )


async def executing_task(application, root: Path, task_id: str):
    task = await application.kernel.create_task(
        "collect the required fact", root, task_id=task_id
    )
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await application.kernel.transition_task(
            task.task_id, state, state.value
        )
    return task


class CompletionReadinessTest(unittest.IsolatedAsyncioTestCase):
    async def test_post_mutation_correction_can_request_approved_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "changed.py"
            target.write_text("value = 1\n", encoding="utf-8")
            workspace_path = PosixWorkspacePath()
            app = compose_fixture_application(
                model_adapter=PostMutationVerificationModel(),
                tool_adapters=(CoreProcessToolProvider(),),
                process_adapter=LocalProcessExecutor(),
                sandbox_adapter=LocalWorkspaceSandbox(workspace_path),
                workspace_path_adapter=workspace_path,
                completion_readiness_policy_adapter=(
                    RuleBasedCompletionReadinessPolicy()
                ),
            )
            await app.registry.start_all()
            try:
                await app.kernel.set_project_trust(
                    root, ProjectTrustLevel.TRUSTED_BUILD
                )
                task = await executing_task(app, root, "ready-verify-e2e")
                await app.kernel.write_workspace_text(
                    task.task_id, "change-file", target.name, "value = 2\n",
                    hashlib.sha256(b"value = 1\n").hexdigest(),
                )
                suspended = await app.kernel.run_agent_turn(
                    task.task_id, "finish and verify the change",
                    max_model_calls=5, max_tool_calls=2,
                )
                self.assertIsInstance(suspended, AgentTurnSuspended)
                assert isinstance(suspended, AgentTurnSuspended)
                completed = await app.kernel.resolve_agent_approval(
                    suspended.approval_request_id, ApprovalDecision.APPROVE,
                    "approve the exact local compile check",
                )
                self.assertIsInstance(completed, AgentTurnResult)
                assert isinstance(completed, AgentTurnResult)
                self.assertEqual(
                    completed.assistant_message.text,
                    "Change verified successfully.",
                )
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                self.assertEqual(sum(
                    event.event_type == "completion.continuation_requested"
                    for event in events
                ), 1)
                self.assertFalse(any(
                    event.event_type == "completion.blocker_disclosure_requested"
                    for event in events
                ))
            finally:
                await app.registry.stop_all()

    async def test_execute_gap_continues_when_execute_capability_is_visible(self) -> None:
        policy = RuleBasedCompletionReadinessPolicy()
        await policy.start(None)  # type: ignore[arg-type]
        try:
            decision = await policy.evaluate(
                CompletionReadinessProbe(
                    goal="verify change",
                    gaps=(CompletionGap(
                        "verification", "POST_MUTATION_VERIFICATION",
                        "latest mutation needs verification", "MISSING",
                        required_effects=(ToolEffect.EXECUTE,),
                        candidate_tools=("core.run_command",),
                    ),),
                    remaining_model_calls=2, remaining_tool_calls=2,
                    available_read_tools=(),
                    available_effects=frozenset({ToolEffect.EXECUTE}),
                    available_tools=("core.run_command",),
                ),
                CompletionReadinessState(),
            )
            self.assertEqual(decision.action, CompletionReadinessAction.CONTINUE)
            self.assertEqual(decision.gaps[0].candidate_tools, ("core.run_command",))
        finally:
            await policy.stop(None)  # type: ignore[arg-type]

    async def test_execute_gap_reports_blocked_without_execute_capability(self) -> None:
        policy = RuleBasedCompletionReadinessPolicy()
        await policy.start(None)  # type: ignore[arg-type]
        try:
            decision = await policy.evaluate(
                CompletionReadinessProbe(
                    goal="verify change",
                    gaps=(CompletionGap(
                        "verification", "POST_MUTATION_VERIFICATION",
                        "latest mutation needs verification", "MISSING",
                        required_effects=(ToolEffect.EXECUTE,),
                    ),),
                    remaining_model_calls=2, remaining_tool_calls=2,
                    available_read_tools=("core.read_file",),
                    available_effects=frozenset({ToolEffect.OBSERVE}),
                ),
                CompletionReadinessState(),
            )
            self.assertEqual(
                decision.action, CompletionReadinessAction.REPORT_BLOCKED,
            )
        finally:
            await policy.stop(None)  # type: ignore[arg-type]

    async def test_kernel_exposes_post_mutation_execute_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "changed.py"
            target.write_text("before\n", encoding="utf-8")
            app = compose_fixture_application(
                tool_adapters=(CoreProcessToolProvider(),),
                completion_readiness_policy_adapter=(
                    RuleBasedCompletionReadinessPolicy()
                ),
            )
            await app.registry.start_all()
            try:
                task = await executing_task(app, root, "ready-mutation")
                await app.kernel.write_workspace_text(
                    task.task_id, "change-file", target.name, "after\n",
                    hashlib.sha256(b"before\n").hexdigest(),
                )
                tools = await app.kernel.list_tools()
                gaps = await app.kernel._completion_readiness_gaps(
                    task.task_id, tools
                )
                gap = next(
                    item for item in gaps
                    if item.kind == "POST_MUTATION_VERIFICATION"
                )
                self.assertEqual(gap.required_effects, (ToolEffect.EXECUTE,))
                self.assertEqual(gap.candidate_tools, ("core.run_command",))

                checkpoint = AgentTurnCheckpoint(
                    task.task_id, "turn-mutation", 1, (), (), (),
                    0, 0, 0, 0, 3, 3, 512, 10.0,
                )
                decision = await app.kernel._evaluate_completion_readiness(
                    task.task_id, checkpoint.turn_id, checkpoint, tools,
                    forced_wrap_up=False,
                )
                self.assertEqual(
                    decision.action, CompletionReadinessAction.CONTINUE
                )
                correction = app.kernel._completion_correction_message(decision)
                self.assertNotIn("read-only", correction.text)
                self.assertIn("normal argument validation", correction.text.lower())
                self.assertIn("core.run_command", correction.text)
            finally:
                await app.registry.stop_all()

    async def test_forced_wrap_up_reports_gap_instead_of_opening_tools(self) -> None:
        policy = RuleBasedCompletionReadinessPolicy()
        await policy.start(None)  # type: ignore[arg-type]
        try:
            decision = await policy.evaluate(
                CompletionReadinessProbe(
                    goal="inspect",
                    gaps=(CompletionGap(
                        "gap", "EVIDENCE_QUESTION", "required fact",
                        "OPEN", recoverable=True, expected_scope=".",
                    ),),
                    remaining_model_calls=2, remaining_tool_calls=2,
                    available_read_tools=("fixture.echo",),
                    forced_wrap_up=True,
                ),
                CompletionReadinessState(),
            )
            self.assertEqual(
                decision.action,
                CompletionReadinessAction.REPORT_INCOMPLETE_RECOVERABLE,
            )
            self.assertEqual(decision.state.continue_attempts, 0)
        finally:
            await policy.stop(None)  # type: ignore[arg-type]

    async def test_no_gap_finishes_without_extra_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = EchoModelProvider()
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=(),
                completion_readiness_policy_adapter=(
                    RuleBasedCompletionReadinessPolicy()
                ),
            )
            await app.registry.start_all()
            try:
                task = await executing_task(app, Path(directory), "ready-no-gap")
                result = await app.kernel.run_agent_turn(task.task_id, "answer")
                self.assertEqual(result.model_calls, 1)
                self.assertEqual(result.assistant_message.text, "answer")
            finally:
                await app.registry.stop_all()

    async def test_recoverable_open_question_continues_and_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = ReadinessSequenceModel()
            tool = RecoverableReadTool(failures=1)
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=(tool,),
                require_evidence_questions=True,
                completion_readiness_policy_adapter=(
                    RuleBasedCompletionReadinessPolicy()
                ),
            )
            await app.registry.start_all()
            try:
                task = await executing_task(app, Path(directory), "ready-continue")
                result = await app.kernel.run_agent_turn(
                    task.task_id, "inspect", max_model_calls=8, max_tool_calls=4
                )
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(result.assistant_message.text, "Required evidence collected.")
                self.assertEqual(tool.attempts, 2)
                questions = await app.kernel.get_evidence_questions(task.task_id)
                self.assertEqual(questions.records[0].status.value, "RESOLVED")
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                self.assertEqual(sum(
                    event.event_type == "completion.continuation_requested"
                    for event in events
                ), 1)
            finally:
                await app.registry.stop_all()

    async def test_blocked_question_requests_one_truthful_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = ReadinessSequenceModel()
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=(BlockedReadTool(),),
                require_evidence_questions=True,
                completion_readiness_policy_adapter=(
                    RuleBasedCompletionReadinessPolicy()
                ),
            )
            await app.registry.start_all()
            try:
                task = await executing_task(app, Path(directory), "ready-blocked")
                result = await app.kernel.run_agent_turn(
                    task.task_id, "inspect", max_model_calls=8, max_tool_calls=4
                )
                self.assertIn("permission denied", result.assistant_message.text)
                self.assertFalse(model.requests[-1].allow_tool_calls)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                self.assertEqual(sum(
                    event.event_type == "completion.blocker_disclosure_requested"
                    for event in events
                ), 1)
            finally:
                await app.registry.stop_all()

    async def test_continue_is_bounded_then_reports_persistent_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = ReadinessSequenceModel()
            tool = RecoverableReadTool(failures=99)
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=(tool,),
                require_evidence_questions=True,
                completion_readiness_policy_adapter=(
                    RuleBasedCompletionReadinessPolicy(max_continue_attempts=1)
                ),
            )
            await app.registry.start_all()
            try:
                task = await executing_task(app, Path(directory), "ready-bounded")
                result = await app.kernel.run_agent_turn(
                    task.task_id, "inspect", max_model_calls=10, max_tool_calls=5
                )
                self.assertIn("remains unverified", result.assistant_message.text)
                self.assertIsInstance(result, AgentContinuationSuspended)
                self.assertEqual(tool.attempts, 2)
                suspended = await app.kernel.get_task(task.task_id)
                self.assertEqual(suspended.state, TaskState.AWAITING_USER)
                self.assertIsNotNone(suspended.active_agent_checkpoint)
                candidates = await app.kernel.list_session_resume_candidates(
                    suspended.session_id
                )
                self.assertEqual(candidates[0].task_id, task.task_id)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                self.assertEqual(sum(
                    event.event_type == "completion.continuation_requested"
                    for event in events
                ), 1)
                self.assertEqual(sum(
                    event.event_type == "completion.incomplete_recoverable_requested"
                    for event in events
                ), 1)
                resumed_result = await app.kernel.resume_agent_continuation(
                    task.task_id, "continue the remaining work",
                    input_id="input-recoverable-resume",
                )
                self.assertIsInstance(
                    resumed_result, AgentContinuationSuspended
                )
                resumed_task = await app.kernel.get_task(task.task_id)
                resumed_checkpoint = AgentTurnCheckpoint.from_data(
                    resumed_task.active_agent_checkpoint or {}
                )
                self.assertGreater(resumed_checkpoint.max_model_calls, 10)
                resumed_events = await app.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                self.assertTrue(any(
                    event.event_type == "continuation.resolved"
                    and event.payload.get("capacity_replenished") is True
                    for event in resumed_events
                ))
            finally:
                await app.registry.stop_all()

    async def test_rejected_premature_stream_text_is_not_shown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = StreamingReadinessModel()
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=(RecoverableReadTool(1),),
                require_evidence_questions=True,
                completion_readiness_policy_adapter=(
                    RuleBasedCompletionReadinessPolicy()
                ),
            )
            await app.registry.start_all()
            try:
                task = await executing_task(app, Path(directory), "ready-stream")
                output: list[str] = []
                result = await app.kernel.run_agent_turn(
                    task.task_id, "inspect", max_model_calls=8, max_tool_calls=4,
                    on_text_delta=output.append,
                )
                self.assertEqual(output, ["Required evidence collected."])
                self.assertNotIn("continue later", "".join(output))
                self.assertEqual(result.assistant_message.text, "Required evidence collected.")
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
