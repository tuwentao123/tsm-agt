from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ApprovalDecision, ModelInvocationFailed, TaskState
from tsm_agt.ports import (
    AdapterDescriptor, CompletionReadinessMode, FinishReason, Message,
    MessageRole, ModelRequest,
    ModelResponse, ModelUsage, ProviderCapabilities, RuntimeStorePort, TextBlock,
    ToolCall,
    ToolCallBlock, ToolIdempotency, ToolResult, ToolResultBlock, ToolRisk, ToolSpec,
    RuntimeEvent, RuntimeUnitOfWork,
)
from tsm_agt.ports import (
    HealthState, HealthStatus, ModelAttemptFailure, ModelFailureCategory,
    ModelRecoveryAction, ModelRecoveryDecision, ModelRecoveryExhausted,
    ModelRetrySafety,
)
from tsm_agt.sdk import EngineeringAgentClient
from tsm_agt.sdk.runtime import _is_resumable_stop


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
                    task.task_id, "arbitrary payload 42",
                    command_id="sdk-input-1",
                )
                replay = await client.route_input(
                    task.task_id, "arbitrary payload 42",
                    command_id="sdk-input-1",
                )
                self.assertEqual(first.result["intent"], "STEER")
                self.assertTrue(first.result["applied"])
                self.assertEqual(
                    first.result["reason_code"], "follow_up_mode_default"
                )
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

    async def test_cancel_command_transitions_task_to_cancelled(self) -> None:
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
                    "cancel me", command_id="sdk-cancel-create"
                )
                await asyncio.wait_for(model.entered.wait(), 5)
                result = await client.cancel(
                    accepted.task_id,
                    command_id="sdk-cancel-command",
                    reason="cancel runtime execution",
                )
                replay = await client.cancel(
                    accepted.task_id,
                    command_id="sdk-cancel-command",
                    reason="cancel runtime execution",
                )
                self.assertTrue(replay.replayed)
                self.assertEqual(result.result["state"], TaskState.CANCELLED.value)
                final = await client.get_task_result(accepted.task_id)
                self.assertEqual(final.state, TaskState.CANCELLED.value)

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

    async def test_resume_continuation_is_wired_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with fixture_client(root) as client:
                accepted = await client.submit_task(
                    "hello resume", command_id="sdk-resume-create"
                )
                final = await client.wait_task(accepted.task_id, timeout=5)
                self.assertEqual(final.state, TaskState.SUCCEEDED.value)
                # A Task that is not awaiting a continuation simply returns its
                # current result, so the explicit channel is always safe.
                first = await client.resume_continuation(
                    final.task_id, command_id="sdk-resume-1", text="继续"
                )
                self.assertIn("task_id", first.result)
                again = await client.resume_continuation(
                    final.task_id, command_id="sdk-resume-1", text="继续"
                )
                self.assertEqual(again.result, first.result)

    async def test_non_legacy_mode_reports_verification_without_finalizing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                completion_readiness_mode=CompletionReadinessMode.OBSERVE_ONLY,
            )
            client = EngineeringAgentClient(
                root, application_factory=lambda: application, poll_interval=0.001
            )
            async with client:
                accepted = await client.submit_task(
                    "observe completion", command_id="sdk-observe-create"
                )
                final = await client.wait_task(accepted.task_id, timeout=5)
                self.assertEqual(final.state, TaskState.EXECUTING.value)
                self.assertEqual(final.status, "running")
                self.assertIsNotNone(final.verification)
                self.assertTrue(final.verification["passed"])
                self.assertIsNotNone(final.completion_diagnostics)
                events = await application.registry.require(RuntimeStorePort).read_events(
                    final.task_id
                )
                self.assertFalse(any(
                    event.event_type == "task.state_changed"
                    and event.payload.get("next_state") in {
                        TaskState.VERIFYING.value, TaskState.FINALIZING.value,
                        TaskState.SUCCEEDED.value, TaskState.FAILED.value,
                    }
                    for event in events
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
    async def test_v3_projection_fields_survive_sqlite_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first = fixture_client(root, SQLiteRuntimeStore(database))
            async with first:
                created = await first.create_task(
                    "recover V3 result", command_id="sdk-v3-create"
                )
                stored = await first._store().load_task(created.task_id)
                assert stored is not None
                snapshot = await first.application.kernel.get_task(created.task_id)
                await first._store().commit(RuntimeUnitOfWork(
                    created.task_id,
                    stored.version,
                    snapshot.to_data(),
                    (RuntimeEvent(
                        "evt-v3-conclusion", created.task_id,
                        stored.last_event_sequence + 1,
                        "conclusion.recorded", {
                            "latest_answer_event_ref": "evt-v3-answer",
                            "conclusion_claims": [{
                                "claim_id": "claim-v3", "kind": "checked",
                                "summary": "restart-safe structured result",
                            }],
                            "conclusion_validation": {
                                "status": "valid",
                                "claims": [{
                                    "claim_id": "claim-v3", "status": "valid",
                                }],
                            },
                        },
                    ),),
                ))

            restarted = fixture_client(root, SQLiteRuntimeStore(database))
            async with restarted:
                result = await restarted.get_task_result(created.task_id)

        self.assertEqual(result.latest_answer_event_ref, "evt-v3-answer")
        self.assertEqual(result.conclusion_claims[0]["claim_id"], "claim-v3")
        self.assertEqual(result.conclusion_validation["status"], "valid")
        self.assertEqual(
            result.to_data()["latest_answer_event_ref"], "evt-v3-answer"
        )

    async def test_classified_resumable_stop_is_not_overwritten_with_failed(
        self,
    ) -> None:
        # INV-14: a Runtime stop that was classified as resumable keeps its
        # checkpoint and its resumable state. This is the case that produced
        # "task-ce19b33b..." -- one exhausted protocol correction was turned into
        # a product failure by the SDK's catch-all.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(tool_adapters=())
            client = EngineeringAgentClient(
                root, application_factory=lambda: application, poll_interval=0.001
            )
            async with client:
                task = await application.kernel.create_task(
                    "resumable stop", root, task_id="sdk-resumable",
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await application.kernel.transition_task(
                        task.task_id, state, state.value,
                    )
                # The runtime always interrupts through INTERRUPTING.
                await application.kernel.transition_task(
                    task.task_id, TaskState.INTERRUPTING, "recoverable stop",
                )
                await application.kernel.transition_task(
                    task.task_id, TaskState.INTERRUPTED, "recoverable stop",
                )

                error = ModelInvocationFailed(
                    "turn-x", "model did not satisfy the tool-call protocol",
                    failure_kind="tool_protocol",
                    failure_category=ModelFailureCategory.CORRECTABLE_PROTOCOL,
                    retry_safety=ModelRetrySafety.SAFE_WITH_CORRECTION,
                    recovery_action=ModelRecoveryAction.PRESERVE_AND_INTERRUPT,
                )
                self.assertTrue(_is_resumable_stop(error))

                async def _raise(*_args, **_kwargs):
                    raise error

                client.run_task = _raise  # type: ignore[assignment]
                result = await client._run_submitted_task(task.task_id, "goal")
                self.assertEqual(result.state, TaskState.INTERRUPTED.value)

    def test_unclassified_error_still_converges_to_failed(self) -> None:
        # The complement: an error nobody classified is a defect and must stay
        # visible as a failure rather than masquerade as a resumable stop.
        self.assertFalse(_is_resumable_stop(RuntimeError("boom")))

    def test_tool_batch_correction_names_the_rejected_call(self) -> None:
        # The retry must aim at the actual defect. Before this, a missing-argument
        # failure fell into the generic "invalid envelope" text and told the model
        # to fix its evidence_question -- so the correction round repeated the same
        # mistake and was wasted ("task-ce19b33b...").
        #
        # Asserted by CALLING the producer, not by reading the Kernel class's
        # source. A source assertion tests the shape of the code rather than its
        # behaviour: it passes while the call site is mis-wired and fails on a
        # correct refactor (Hermes AGENTS.md:487-502) -- which is exactly what
        # happened when this phase moved out of Kernel.
        from tsm_agt.core.kernel import Kernel

        retry = Kernel._protocol_correction_text(
            "invalid_tool_batch", "core.read_file: path is required",
        )
        self.assertIn("core.read_file: path is required", retry)
        self.assertIn("Re-issue that call with every required argument", retry)
        self.assertIn("Do not repeat the rejected call unchanged", retry)

    def test_unclassified_protocol_reason_falls_back_to_envelope_retry(self) -> None:
        """An unknown code must still produce one actionable retry, not silence."""

        from tsm_agt.core.kernel import Kernel

        retry = Kernel._protocol_correction_text("something_new", "detail")
        self.assertIn("invalid tool-call envelope", retry)
        self.assertIn("evidence_question", retry)

    def test_disabled_tools_correction_forbids_more_tool_calls(self) -> None:
        """The finalization correction must stop tool use, not invite a retry."""

        from tsm_agt.core.kernel import Kernel

        retry = Kernel._protocol_correction_text(
            "tool_call_emitted_while_disabled", "",
        )
        self.assertIn("Do not emit XML", retry)
        self.assertIn("Answer the user's request directly", retry)


if __name__ == "__main__":
    unittest.main()


class _TransportFailingPlanner:
    """A planner whose provider connection failed on every attempt."""

    descriptor = AdapterDescriptor(
        "fixture.transport-failing-planner", "1.0", "TaskSpecPlannerPort", "1.0"
    )

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def start(self, context) -> None:
        pass

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY, "ready")

    async def stop(self, deadline) -> None:
        pass

    async def propose_task_spec(self, goal, context):
        raise self._error


def _transport_exhaustion() -> ModelRecoveryExhausted:
    return ModelRecoveryExhausted(
        ModelAttemptFailure(
            ModelFailureCategory.TRANSIENT_PROVIDER,
            ModelRetrySafety.SAFE_SAME_REQUEST,
            "connection_failed",
            "provider connection failed: SSL: UNEXPECTED_EOF_WHILE_READING",
        ),
        ModelRecoveryDecision(ModelRecoveryAction.FAIL_TERMINAL, "exhausted"),
        3,
    )


class PlannerTransportFailureTest(unittest.IsolatedAsyncioTestCase):
    """A provider outage must not become a product failure.

    The planner runs before any work is committed, so a gateway that dropped the
    connection leaves nothing to report as failed: the Task stops at the
    recoverable interruption and can be retried
    (``task-e547187aaaac4f4b83ae9ff1d2c767b5``).
    """

    async def _run(self, error: Exception):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                task_spec_planner_adapter=_TransportFailingPlanner(error),
                tool_adapters=(),
            )
            client = EngineeringAgentClient(
                root, application_factory=lambda: application,
                poll_interval=0.001,
            )
            async with client:
                accepted = await client.submit_task(
                    "查下今天的nba新闻", command_id="planner-failure",
                )
                final = await client.wait_task(accepted.task_id, timeout=5)
                events = await application.kernel.dependencies.store.read_events(
                    accepted.task_id
                )
                return final, events, application

    async def test_transport_exhaustion_interrupts_instead_of_failing(self) -> None:
        final, events, application = await self._run(_transport_exhaustion())
        self.assertEqual(final.state, TaskState.INTERRUPTED.value)
        planning = [
            event for event in events
            if event.event_type == "task_spec.planning_failed"
        ]
        self.assertEqual(len(planning), 1)
        self.assertTrue(planning[0].payload["recoverable"])
        interrupted = await application.kernel.get_task(final.task_id)
        self.assertIsNotNone(interrupted.active_agent_checkpoint)

    async def test_a_planner_bug_still_converges_to_failed(self) -> None:
        final, events, _ = await self._run(ValueError("malformed proposal"))
        self.assertEqual(final.state, TaskState.FAILED.value)
        planning = [
            event for event in events
            if event.event_type == "task_spec.planning_failed"
        ]
        self.assertFalse(planning[0].payload["recoverable"])
