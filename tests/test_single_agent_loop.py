from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AgentTurnResult,
    AgentTurnIncomplete,
    AgentTurnSuspended,
    AgentClarificationSuspended,
    ApprovalDecision,
    TaskState,
)
from tsm_agt.core.single_loop import OrchestrationMode
from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    FinishReason,
    HealthState,
    HealthStatus,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    RecoverableToolProtocolError,
    RuntimeStorePort,
    TextBlock,
    ToolCall,
    ToolCallBlock,
    ToolIdempotency,
    ToolEffect,
    ToolInvocationContext,
    ToolResult,
    ToolRisk,
    ToolSpec,
)
from tsm_agt.ports.evidence import (
    ContractAuthority,
    EvidenceRequirement,
    GateDecision,
    GateResolution,
    TaskContract,
)


class _DirectModel(EchoModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.request: ModelRequest | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.request = request
        return ModelResponse(
            Message("single-direct", MessageRole.ASSISTANT, (TextBlock("direct"),)),
            FinishReason.STOP,
            ModelUsage(1, 1),
        )


class _FailOnceModel(EchoModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated interruption")
        return ModelResponse(
            Message("resumed-final", MessageRole.ASSISTANT, (TextBlock("resumed"),)),
            FinishReason.STOP,
            ModelUsage(1, 1),
        )


class _IntentThenTextModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if any(
            block.__class__.__name__ == "ToolResultBlock"
            for message in request.messages
            for block in message.content
        ):
            return ModelResponse(
                Message("intent-final", MessageRole.ASSISTANT, (TextBlock("done"),)),
                FinishReason.STOP,
                ModelUsage(1, 1),
            )
        return ModelResponse(
            Message(
                "intent-call",
                MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "intent-call-id",
                    "core.turn_intent",
                    {
                        "intent": "INVESTIGATE",
                        "dependencies": [],
                        "summary": "inspect before answering",
                    },
                )),),
            ),
            FinishReason.TOOL_CALL,
            ModelUsage(1, 1),
        )


class _ModifyThenTextModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if any(
            block.__class__.__name__ == "ToolResultBlock"
            for message in request.messages
            for block in message.content
        ):
            return ModelResponse(
                Message("modify-final", MessageRole.ASSISTANT, (TextBlock("done"),)),
                FinishReason.STOP,
                ModelUsage(1, 1),
            )
        return ModelResponse(
            Message(
                "modify-intent-call",
                MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "modify-intent-call-id",
                    "core.turn_intent",
                    {
                        "intent": "MODIFY",
                        "dependencies": [],
                        "summary": "change the workspace",
                    },
                )),),
            ),
            FinishReason.TOOL_CALL,
            ModelUsage(1, 1),
        )


class _ApprovalToolProvider:
    descriptor = AdapterDescriptor(
        adapter_id="test.approval-tool",
        adapter_version="1",
        port_name="ToolProviderPort",
        port_version="1",
        capabilities=frozenset({"test.approval"}),
    )
    _spec = ToolSpec(
        name="test.approval",
        description="Approval-gated test action.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=ToolRisk.R1,
        is_read_only=False,
        is_concurrency_safe=True,
        idempotency=ToolIdempotency.IDEMPOTENT,
    )

    async def start(self, context: AdapterContext) -> None:
        del context

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY, "ready")

    async def stop(self, deadline: datetime) -> None:
        del deadline

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return (self._spec,)

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        del context
        return ToolResult(call.call_id, True, data={"approved": call.name})


class _ApprovalThenTextModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if any(
            block.__class__.__name__ == "ToolResultBlock"
            for message in request.messages
            for block in message.content
        ):
            return ModelResponse(
                Message("approval-final", MessageRole.ASSISTANT, (TextBlock("approved"),)),
                FinishReason.STOP,
                ModelUsage(1, 1),
            )
        return ModelResponse(
            Message(
                "approval-call",
                MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall("approval-call-id", "test.approval", {})),),
            ),
            FinishReason.TOOL_CALL,
            ModelUsage(1, 1),
        )


class _ProtocolCorrectionModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            if self.source == "request":
                raise RecoverableToolProtocolError("provider_tool_protocol", "retry")
            if self.source == "response":
                return ModelResponse(
                    Message(
                        "protocol-text",
                        MessageRole.ASSISTANT,
                        (TextBlock("<tool_use>{}</tool_use>"),),
                    ),
                    FinishReason.STOP,
                    ModelUsage(1, 1),
                )
            return ModelResponse(
                Message(
                    "protocol-batch",
                    MessageRole.ASSISTANT,
                    (ToolCallBlock(ToolCall("rejected-call", "missing.tool", {})),),
                ),
                FinishReason.TOOL_CALL,
                ModelUsage(1, 1),
            )
        return ModelResponse(
            Message("protocol-final", MessageRole.ASSISTANT, (TextBlock("fixed"),)),
            FinishReason.STOP,
            ModelUsage(1, 1),
        )


class _PassingEvidenceGate:
    descriptor = AdapterDescriptor(
        "test.passing-evidence-gate", "1.0", "EvidenceGatePort", "1.0"
    )

    async def start(self, context: AdapterContext) -> None:
        del context

    async def stop(self, deadline: datetime) -> None:
        del deadline

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY, "ready")

    def evaluate(self, contract, facts, capabilities) -> GateDecision:
        del contract, facts, capabilities
        return GateDecision(GateResolution.PASS, reason_code="TEST_OVERRIDE")


class SingleAgentLoopTest(unittest.IsolatedAsyncioTestCase):
    async def _executing_task(
        self,
        application,
        directory: str,
        task_id: str,
        evidence_contract: TaskContract | None = None,
    ):
        task = await application.kernel.create_task(
            "answer",
            Path(directory),
            task_id,
            evidence_contract=evidence_contract,
        )
        for state in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(task.task_id, state, state.value)
        return task

    async def _run(
        self,
        model,
        *,
        tools=None,
        evidence_contract: TaskContract | None = None,
        evidence_gate_adapter=None,
    ):
        application = compose_fixture_application(
            model_adapter=model,
            tool_adapters=tools,
            evidence_gate_adapter=evidence_gate_adapter,
            orchestration_mode=OrchestrationMode.SINGLE_LOOP,
        )
        await application.registry.start_all()
        directory = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(application.registry.stop_all)
        self.addCleanup(directory.cleanup)
        task = await self._executing_task(
            application,
            directory.name,
            "single-task",
            evidence_contract,
        )
        return application, task

    async def test_direct_response_uses_single_loop_without_legacy_planner_or_resolver(self) -> None:
        application, task = await self._run(_DirectModel(), tools=())
        application.kernel.plan_task_spec = AsyncMock(side_effect=AssertionError("planner"))
        application.kernel.resolve_session_input = AsyncMock(side_effect=AssertionError("resolver"))

        result = await application.kernel.run_agent_turn(task.task_id, "answer")

        self.assertIsInstance(result, AgentTurnResult)
        self.assertEqual(result.assistant_message.text, "direct")
        application.kernel.plan_task_spec.assert_not_awaited()
        application.kernel.resolve_session_input.assert_not_awaited()
        events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
        self.assertEqual(
            next(event for event in events if event.event_type == "turn.started").payload[
                "orchestration_mode"
            ],
            OrchestrationMode.SINGLE_LOOP.value,
        )
        self.assertEqual(
            next(event for event in events if event.event_type == "llm.completed").payload[
                "orchestration_mode"
            ],
            OrchestrationMode.SINGLE_LOOP.value,
        )

    async def test_checkpoint_resume_returns_to_the_single_loop(self) -> None:
        model = _FailOnceModel()
        application, task = await self._run(model, tools=())

        with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
            await application.kernel.run_agent_turn(task.task_id, "resume")
        await application.kernel.transition_task(
            task.task_id, TaskState.INTERRUPTING, "test interruption requested"
        )
        await application.kernel.transition_task(
            task.task_id, TaskState.INTERRUPTED, "test interruption settled"
        )

        result = await application.kernel.resume_checkpointed_agent_turn(task.task_id)

        self.assertIsInstance(result, AgentTurnResult)
        self.assertEqual(result.assistant_message.text, "resumed")
        events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
        resumed = next(event for event in events if event.event_type == "turn.resumed")
        self.assertEqual(
            resumed.payload["orchestration_mode"], OrchestrationMode.SINGLE_LOOP.value
        )

    async def test_shadow_gap_gets_one_diagnostic_feedback_without_gating_final(self) -> None:
        application, task = await self._run(_IntentThenTextModel())

        result = await application.kernel.run_agent_turn(task.task_id, "inspect")

        self.assertIsInstance(result, AgentTurnResult)
        assert isinstance(result, AgentTurnResult)
        self.assertEqual(result.assistant_message.text, "done")
        events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
        declared = next(event for event in events if event.event_type == "turn.intent_declared")
        self.assertEqual(declared.payload["turn_id"], result.turn_id)
        self.assertEqual(declared.payload["intent"], "INVESTIGATE")
        self.assertEqual(
            [
                event.payload["reason"]
                for event in events
                if event.event_type == "checkpoint.saved"
                and event.payload["reason"] == "single-loop-contract-feedback"
            ],
            ["single-loop-contract-feedback"],
        )
        shadow_events = [
            event for event in events
            if event.event_type == "completion.shadow_validated"
        ]
        self.assertEqual(len(shadow_events), 2)
        self.assertTrue(shadow_events[0].payload["gaps"])
        self.assertNotIn("turn.incomplete_boundary", [event.event_type for event in events])

    async def test_completion_gap_at_budget_records_non_success_boundary(self) -> None:
        application, task = await self._run(_ModifyThenTextModel())
        visible_tools = await application.kernel.list_tools()

        async def list_tools() -> tuple[ToolSpec, ...]:
            return visible_tools + (ToolSpec(
                name="fixture.repair-capability",
                description="Expose mutation repair capability for this loop test.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                risk=ToolRisk.R0,
                is_concurrency_safe=True,
                idempotency=ToolIdempotency.IDEMPOTENT,
                effect=ToolEffect.MUTATE,
            ),)

        application.kernel.list_tools = list_tools
        result = await application.kernel.run_agent_turn(
            task.task_id, "modify", max_model_calls=3
        )

        self.assertIsInstance(result, AgentTurnIncomplete)
        assert isinstance(result, AgentTurnIncomplete)
        self.assertEqual(result.reason, "completion_gaps_unrecoverable")
        self.assertEqual(result.gaps[0]["code"], "NET_MUTATION_MISSING")
        events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
        self.assertIn("turn.incomplete_boundary", [event.event_type for event in events])
        self.assertNotIn("llm.completed", [event.event_type for event in events])
        self.assertIn(
            "completion.shadow_validated",
            [event.event_type for event in events],
        )
        self.assertIn(
            "single-loop-completion-continuation",
            [event.payload.get("reason") for event in events if event.event_type == "checkpoint.saved"],
        )

        application, task = await self._run(
            _ApprovalThenTextModel(), tools=(_ApprovalToolProvider(),)
        )

        suspended = await application.kernel.run_agent_turn(task.task_id, "act")

        self.assertIsInstance(suspended, AgentTurnSuspended)
        assert isinstance(suspended, AgentTurnSuspended)
        resumed = await application.kernel.resolve_agent_approval(
            suspended.approval_request_id,
            ApprovalDecision.APPROVE,
            "approved for test",
        )

        self.assertIsInstance(resumed, AgentTurnResult)
        assert isinstance(resumed, AgentTurnResult)
        self.assertEqual(resumed.assistant_message.text, "approved")
        events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
        self.assertIn("approval.requested", [event.event_type for event in events])
        self.assertIn("approval.resolved", [event.event_type for event in events])
        self.assertEqual(
            next(event for event in events if event.event_type == "llm.completed").payload[
                "orchestration_mode"
            ],
            OrchestrationMode.SINGLE_LOOP.value,
        )
    async def test_user_contract_blocks_and_is_projected_to_the_model(self) -> None:
        model = _DirectModel()
        contract = TaskContract(
            objective="verify source provenance",
            authority=ContractAuthority.USER,
            evidence_requirements=(EvidenceRequirement("source", "source.artifact"),),
        )
        application, task = await self._run(
            model,
            tools=(),
            evidence_contract=contract,
        )

        result = await application.kernel.run_agent_turn(task.task_id, "answer")

        self.assertIsInstance(result, AgentClarificationSuspended)
        assert isinstance(result, AgentClarificationSuspended)
        self.assertEqual(result.kind, "CONTRACT_BLOCKED")
        self.assertTrue(any(
            message.message_id.startswith("task-contract-context-")
            for message in model.request.messages
        ))
        waiting = await application.kernel.get_task(task.task_id)
        self.assertEqual(waiting.state, TaskState.AWAITING_USER)
        self.assertEqual(
            waiting.pending_clarification.payload["missing_requirement_ids"], ["source"]
        )
        events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
        blocked = next(
            event for event in events if event.event_type == "clarification.requested"
        )
        self.assertEqual(blocked.payload["kind"], "CONTRACT_BLOCKED")
        self.assertNotIn("llm.completed", [event.event_type for event in events])

    async def test_composed_evidence_gate_can_override_completion(self) -> None:
        contract = TaskContract(
            objective="verify source provenance",
            authority=ContractAuthority.USER,
            evidence_requirements=(EvidenceRequirement("source", "source.artifact"),),
        )
        application, task = await self._run(
            _DirectModel(),
            tools=(),
            evidence_contract=contract,
            evidence_gate_adapter=_PassingEvidenceGate(),
        )

        result = await application.kernel.run_agent_turn(task.task_id, "answer")

        self.assertIsInstance(result, AgentTurnResult)
        self.assertEqual(
            application.kernel.dependencies.evidence_gate.descriptor.adapter_id,
            "test.passing-evidence-gate",
        )

    async def test_protocol_correction_recovers_every_recoverable_source(self) -> None:
        for source, reason_code in (
            ("request", "provider_tool_protocol"),
            ("response", "invalid_text_tool_protocol"),
            ("batch", "invalid_tool_batch"),
        ):
            with self.subTest(source=source):
                model = _ProtocolCorrectionModel(source)
                application, task = await self._run(model, tools=())

                result = await application.kernel.run_agent_turn(task.task_id, "answer")

                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(len(model.requests), 2)
                self.assertFalse(any(
                    isinstance(block, ToolCallBlock)
                    for message in model.requests[1].messages
                    for block in message.content
                ))
                events = await application.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                rejected = next(
                    event for event in events if event.event_type == "tool.protocol_rejected"
                )
                self.assertEqual(rejected.payload["reason_code"], reason_code)


if __name__ == "__main__":
    unittest.main()


class _StreamingModifyThenTextModel(_ModifyThenTextModel):
    capabilities = ProviderCapabilities(
        tools=True, stream_cancel=True, context_window=4096
    )

    async def stream_complete(self, request: ModelRequest):
        response = await self.complete(request)
        if response.message.text:
            from tsm_agt.ports import ModelStreamCompleted, ModelTextDelta

            yield ModelTextDelta(response.message.text)
            yield ModelStreamCompleted(response)
        else:
            from tsm_agt.ports import ModelStreamCompleted

            yield ModelStreamCompleted(response)


class SingleLoopStreamingBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_completion_never_flushes_candidate_text(self) -> None:
        application = compose_fixture_application(
            model_adapter=_StreamingModifyThenTextModel(),
            orchestration_mode=OrchestrationMode.SINGLE_LOOP,
        )
        await application.registry.start_all()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addAsyncCleanup(application.registry.stop_all)
        task = await application.kernel.create_task(
            "modify", Path(directory.name), task_id="streaming-incomplete"
        )
        for state in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, state, state.value
            )

        deltas: list[str] = []
        result = await application.kernel.run_agent_turn(
            task.task_id, "modify", max_model_calls=2, on_text_delta=deltas.append
        )

        self.assertIsInstance(result, AgentTurnIncomplete)
        self.assertEqual(deltas, [])
