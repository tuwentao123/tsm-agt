from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
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
    AgentTurnSuspended, ApprovalDecision, Kernel, ProjectTrustLevel,
    TaskState, TaskSpecProposal, TaskSpecSnapshot, ToolCommitState,
    ToolExecutionRecord,
)
from tsm_agt.core.kernel import (
    _continuation_made_no_progress, _post_mutation_verification,
    _post_mutation_verification_description,
    _post_mutation_verification_observed,
)
from tsm_agt.ports import (
    CompletionGap, CompletionReadinessAction, CompletionReadinessDecision,
    CompletionReadinessProbe,
    CompletionReadinessState, EvidenceQuestion, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ModelStreamCompleted, ModelTextDelta, ModelUsage,
    ProviderCapabilities, RuntimeStorePort, TextBlock, ToolCall, ToolCallBlock,
    ToolEffect, ToolIdempotency, ToolInvocationContext, ToolResult,
    ToolResultBlock, ToolRisk,
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
                            "argv": [
                                sys.executable, "-m", "unittest",
                                "test_changed",
                            ],
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
    async def _require_command_verification(self, app, task) -> None:
        current = await app.kernel.get_task_spec(task.task_id)
        proposal = TaskSpecProposal.from_data({
            "schema_version": 1, "goal": task.goal,
            "scope": ["."], "constraints": [],
            "outcomes": [{
                "outcome_id": "verification",
                "description": "Run required behavioral verification",
                "kind": "COMMAND_RESULT",
                "required_effects": ["execute"], "required": True,
            }],
            "continuation_policy": {"mode": "NONE"},
        })
        spec = TaskSpecSnapshot.from_proposal(
            task.task_id, current.revision + 1, proposal,
            current.acceptance_criteria,
        )
        await app.kernel._append_events(task.task_id, ((
            "task_spec.revised", {"snapshot": spec.to_data()},
        ),))

    async def test_post_mutation_correction_can_request_approved_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "changed.py"
            target.write_text("value = 1\n", encoding="utf-8")
            (root / "test_changed.py").write_text(
                "import unittest\n"
                "import changed\n\n"
                "class ChangedTest(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual(changed.value, 2)\n",
                encoding="utf-8",
            )
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
                await self._require_command_verification(app, task)
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
                    "approve the exact local unit test",
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
                self.assertGreaterEqual(sum(
                    event.event_type == "completion.continuation_requested"
                    for event in events
                ), 1)
                command_results = [
                    event.payload["result"]
                    for event in events if event.event_type == "tool.completed"
                    and event.payload.get("result", {}).get("call_id")
                    == "verify-change"
                ]
                self.assertEqual(command_results[-1]["data"]["exit_code"], 0)
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
                await self._require_command_verification(app, task)
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

    async def test_mutation_only_delivery_does_not_invent_command_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "delivery.txt"
            target.write_text("before\n", encoding="utf-8")
            app = compose_fixture_application(
                tool_adapters=(CoreProcessToolProvider(),),
                completion_readiness_policy_adapter=(
                    RuleBasedCompletionReadinessPolicy()
                ),
            )
            await app.registry.start_all()
            try:
                task = await executing_task(app, root, "mutation-only")
                current = await app.kernel.get_task_spec(task.task_id)
                proposal = TaskSpecProposal.from_data({
                    "schema_version": 1, "goal": task.goal,
                    "scope": ["delivery.txt"], "constraints": [],
                    "outcomes": [{
                        "outcome_id": "delivery",
                        "description": "Deliver one workspace file",
                        "kind": "WORKSPACE_DELIVERY",
                        "required_effects": ["mutate"],
                        "required": True,
                    }],
                    "continuation_policy": {"mode": "NONE"},
                })
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, current.revision + 1, proposal,
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                await app.kernel.write_workspace_text(
                    task.task_id, "write-delivery", target.name, "after\n",
                    hashlib.sha256(b"before\n").hexdigest(),
                )
                gaps = await app.kernel._completion_readiness_gaps(
                    task.task_id, await app.kernel.list_tools()
                )
                self.assertFalse(any(
                    gap.kind == "POST_MUTATION_VERIFICATION" for gap in gaps
                ))
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
                    event.event_type == "completion.automatic_resume_started"
                    for event in events
                ), 1)
                self.assertGreaterEqual(sum(
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


class _VerificationFacts:
    """Minimal duck-typed task for the post-mutation verification contract.

    The contract only reads ``mutation_journal`` and ``tool_executions``, so a
    stand-in keeps these cases about the predicate and its wording instead of
    about building a whole TaskSnapshot.
    """

    def __init__(self, mutation_at, executions):
        self.mutation_journal = (_Mutation(mutation_at),)
        self.tool_executions = {
            item.invocation_id: item for item in executions
        }


class _Mutation:
    def __init__(self, created_at):
        self.created_at = created_at


def _execution(argv, exit_code, *, stderr="", at=None, name="core.run_command"):
    moment = at or datetime(2026, 1, 2, tzinfo=timezone.utc)
    return ToolExecutionRecord(
        execution_id="turn-verify:" + argv[-1],
        turn_id="turn-verify",
        invocation_id="inv-" + argv[-1],
        call=ToolCall("call-" + argv[-1], name, {"argv": list(argv)}),
        payload_hash="payload",
        policy_decision_id="policy",
        effective_risk=ToolRisk.R0,
        approval_request_id=None,
        idempotency=ToolIdempotency.IDEMPOTENT,
        idempotency_key=None,
        state=ToolCommitState.COMMITTED,
        result=ToolResult(
            "call-" + argv[-1], True,
            data={
                "mode": "foreground", "status": "exited",
                "exit_code": exit_code,
                "stdout": {"text": "", "total_bytes": 0, "truncated": False},
                "stderr": {
                    "text": stderr, "total_bytes": len(stderr),
                    "truncated": False,
                },
            },
        ),
        started_at=moment,
        updated_at=moment,
    )


class PostMutationVerificationContractTest(unittest.TestCase):
    """The requirement must say what counts, and why the last try did not."""

    MUTATION_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_failed_recognized_attempt_is_reported_with_its_reason(self):
        task = _VerificationFacts(self.MUTATION_AT, [_execution(
            ["python3", "-m", "pytest", "tests"], 1,
            stderr="/usr/bin/python3: No module named pytest\n",
        )])
        outcome = _post_mutation_verification(task)
        self.assertFalse(outcome.passed)
        self.assertEqual(
            outcome.latest_argv, ("python3", "-m", "pytest", "tests")
        )
        self.assertEqual(outcome.latest_exit_code, 1)
        observed = _post_mutation_verification_observed(
            outcome, ".venv/bin/python"
        )
        self.assertIn("python3 -m pytest tests", observed)
        self.assertIn("exited 1", observed)
        self.assertIn("No module named pytest", observed)
        self.assertIn(".venv/bin/python", observed)

    def test_static_syntax_checks_never_satisfy_verification(self):
        """P043 keeps compileall out of behavioral evidence; so does this."""
        task = _VerificationFacts(self.MUTATION_AT, [_execution(
            ["python3", "-m", "compileall", "-f", "src"], 0,
        )])
        self.assertFalse(_post_mutation_verification(task).passed)
        description = _post_mutation_verification_description(
            _post_mutation_verification(task)
        )
        self.assertIn("python -m pytest", description)
        self.assertIn("compileall", description)

    def test_successful_recognized_command_satisfies_verification(self):
        task = _VerificationFacts(self.MUTATION_AT, [_execution(
            [".venv/bin/python", "-m", "pytest", "tests"], 0,
        )])
        self.assertTrue(_post_mutation_verification(task).passed)

    def test_attempt_before_the_latest_mutation_does_not_count(self):
        task = _VerificationFacts(
            datetime(2026, 2, 1, tzinfo=timezone.utc),
            [_execution(
                [".venv/bin/python", "-m", "pytest"], 0,
                at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )],
        )
        self.assertFalse(_post_mutation_verification(task).passed)

    def test_correction_message_carries_the_gap_and_its_observation(self):
        gap = CompletionGap(
            "post-mutation-verification", "POST_MUTATION_VERIFICATION",
            "Only a recognized test or build command counts.",
            "MISSING", required_effects=(ToolEffect.EXECUTE,),
            candidate_tools=("core.run_command",),
            observed="Last recognized attempt: `python3 -m pytest` exited 1.",
        )
        message = Kernel._completion_correction_message(
            CompletionReadinessDecision(
                CompletionReadinessAction.CONTINUE,
                "required_capability_is_available",
                CompletionReadinessState(), (gap,),
            )
        )
        self.assertIn("Only a recognized test or build command counts.",
                      message.text)
        self.assertIn("exited 1", message.text)


class ContinuationStallTest(unittest.IsolatedAsyncioTestCase):
    """An unchanged requirement must not be retried forever."""

    def test_no_progress_requires_identical_gaps_and_outcomes(self):
        previous = {
            "gap_ids": ["post-mutation-verification"],
            "remaining_outcome_ids": ["implementation"],
        }
        self.assertTrue(_continuation_made_no_progress(
            previous, ("post-mutation-verification",), ("implementation",),
        ))
        # Finishing anything resets the stall: real progress happened.
        self.assertFalse(_continuation_made_no_progress(
            previous, ("post-mutation-verification",), (),
        ))
        self.assertFalse(_continuation_made_no_progress(
            previous, ("a-different-gap",), ("implementation",),
        ))
        self.assertFalse(_continuation_made_no_progress(
            {}, ("post-mutation-verification",), ("implementation",),
        ))
        self.assertFalse(_continuation_made_no_progress(
            previous, (), (),
        ))

    def test_stalled_counter_survives_state_serialization(self):
        state = CompletionReadinessState(stalled_continuations=2)
        restored = CompletionReadinessState.from_data(state.to_data())
        self.assertEqual(restored.stalled_continuations, 2)

    async def test_repeated_stall_reports_blocked_instead_of_continuing(self):
        policy = RuleBasedCompletionReadinessPolicy()
        await policy.start(None)  # type: ignore[arg-type]
        gap = CompletionGap(
            "post-mutation-verification", "POST_MUTATION_VERIFICATION",
            "Run a recognized test.", "MISSING",
            required_effects=(ToolEffect.EXECUTE,),
        )
        probe = CompletionReadinessProbe(
            goal="verify the change", gaps=(gap,),
            remaining_model_calls=6, remaining_tool_calls=6,
            available_read_tools=("core.read_file",),
            available_effects=frozenset({ToolEffect.EXECUTE}),
        )

        # First stall: still allowed to continue the work once.
        first = await policy.evaluate(
            probe, CompletionReadinessState(stalled_continuations=1)
        )
        self.assertIs(first.action, CompletionReadinessAction.CONTINUE)

        # At the limit the same requirement is reported as a blocker, and the
        # stall count keeps travelling with the state.
        blocked = await policy.evaluate(
            probe, CompletionReadinessState(stalled_continuations=2)
        )
        self.assertIs(blocked.action, CompletionReadinessAction.REPORT_BLOCKED)
        self.assertEqual(
            blocked.reason, "required_gaps_unchanged_across_continuations"
        )
        self.assertEqual(blocked.state.stalled_continuations, 2)

    async def test_continue_keeps_the_stall_count(self):
        policy = RuleBasedCompletionReadinessPolicy()
        await policy.start(None)  # type: ignore[arg-type]
        gap = CompletionGap(
            "post-mutation-verification", "POST_MUTATION_VERIFICATION",
            "Run a recognized test.", "MISSING",
            required_effects=(ToolEffect.EXECUTE,),
        )
        decision = await policy.evaluate(
            CompletionReadinessProbe(
                goal="verify", gaps=(gap,), remaining_model_calls=6,
                remaining_tool_calls=6, available_read_tools=(),
                available_effects=frozenset({ToolEffect.EXECUTE}),
            ),
            CompletionReadinessState(stalled_continuations=1),
        )
        self.assertIs(decision.action, CompletionReadinessAction.CONTINUE)
        self.assertEqual(decision.state.stalled_continuations, 1)
