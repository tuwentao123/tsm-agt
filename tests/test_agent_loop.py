from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider, ToolCallingModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AgentLoopLimitExceeded,
    AgentClarificationSuspended,
    AgentContinuationSuspended,
    AgentTurnResult,
    AgentTurnSuspended,
    ApprovalDecision,
    ApprovalNotPending,
    ModelInvocationFailed,
    ProviderCapabilityMismatch,
    RuntimeInputIntent,
    TaskState,
    ToolCommitState,
)
from tsm_agt.ports import (
    AdapterDescriptor,
    FinishReason,
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
    ToolEffect,
    ToolIdempotency,
    ToolInvocationContext,
    ToolResult,
    ToolResultBlock,
    ToolRisk,
    ToolSpec,
)


class UnknownToolThenFinishModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        result = next(
            (
                block.result
                for message in request.messages
                for block in message.content
                if isinstance(block, ToolResultBlock)
            ),
            None,
        )
        if result is None:
            return ModelResponse(
                Message(
                    "assistant-tool",
                    MessageRole.ASSISTANT,
                    (ToolCallBlock(ToolCall("call-missing", "missing.tool")),),
                ),
                FinishReason.TOOL_CALL,
            )
        return ModelResponse(
            Message(
                "assistant-final",
                MessageRole.ASSISTANT,
                (TextBlock(f"Handled {result.error_code}"),),
            )
        )


class DuplicateToolCallModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        call = ToolCall("same-call", "fixture.echo", {"text": "hello"})
        return ModelResponse(
            Message(
                "assistant-invalid",
                MessageRole.ASSISTANT,
                (ToolCallBlock(call), ToolCallBlock(call)),
            ),
            FinishReason.TOOL_CALL,
        )


class AlwaysCallToolModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        call_number = sum(
            isinstance(block, ToolResultBlock)
            for message in request.messages
            for block in message.content
        )
        return ModelResponse(
            Message(
                f"assistant-{call_number}",
                MessageRole.ASSISTANT,
                (
                    ToolCallBlock(
                        ToolCall(
                            f"call-{call_number}",
                            "fixture.echo",
                            {"text": "again"},
                        )
                    ),
                ),
            ),
            FinishReason.TOOL_CALL,
            ModelUsage(1, 1),
        )


class WrapUpAwareModel(AlwaysCallToolModel):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.allow_tool_calls:
            return ModelResponse(
                Message(
                    "assistant-wrap-up", MessageRole.ASSISTANT,
                    (TextBlock("Concluding from collected evidence."),),
                ),
                FinishReason.STOP, ModelUsage(1, 1),
            )
        return await super().complete(request)


class WrapUpProtocolViolationThenAnswerModel(AlwaysCallToolModel):
    def __init__(self) -> None:
        super().__init__()
        self.wrap_up_attempts = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.allow_tool_calls:
            self.wrap_up_attempts += 1
            if self.wrap_up_attempts == 1:
                raise RecoverableToolProtocolError(
                    "tool_call_emitted_while_disabled"
                )
            return ModelResponse(
                Message(
                    "assistant-corrected-wrap-up", MessageRole.ASSISTANT,
                    (TextBlock("Direct final answer from collected evidence."),),
                ),
                FinishReason.STOP, ModelUsage(1, 1),
            )
        return await super().complete(request)


class TwoToolCallsModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            Message(
                "assistant-two-tools",
                MessageRole.ASSISTANT,
                (
                    ToolCallBlock(
                        ToolCall("call-1", "fixture.echo", {"text": "one"})
                    ),
                    ToolCallBlock(
                        ToolCall("call-2", "fixture.echo", {"text": "two"})
                    ),
                ),
            ),
            FinishReason.TOOL_CALL,
        )


class TwoUnsafeCallsThenFinishModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        result = next((
            block.result
            for message in reversed(request.messages)
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ), None)
        if result is not None:
            return ModelResponse(
                Message(
                    "assistant-after-reconciliation", MessageRole.ASSISTANT,
                    (TextBlock("Replanned after explicit reconciliation."),),
                ),
                FinishReason.STOP, ModelUsage(1, 1),
            )
        return ModelResponse(
            Message(
                "assistant-two-unsafe", MessageRole.ASSISTANT,
                (
                    ToolCallBlock(ToolCall(
                        "unsafe-1", "fixture.write", {"text": "one"}
                    )),
                    ToolCallBlock(ToolCall(
                        "unsafe-2", "fixture.write", {"text": "two"}
                    )),
                ),
            ),
            FinishReason.TOOL_CALL, ModelUsage(1, 1),
        )


class CorrectableProtocolModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    def __init__(self, *, always_invalid: bool = False) -> None:
        super().__init__()
        self.calls = 0
        self.always_invalid = always_invalid

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls == 1 or self.always_invalid:
            raise RecoverableToolProtocolError(
                "missing_evidence_question_envelope"
            )
        return ModelResponse(Message(
            "assistant-corrected", MessageRole.ASSISTANT,
            (TextBlock("Corrected after one protocol retry."),),
        ))


class RiskyToolCallingModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        result = next(
            (
                block.result
                for message in reversed(request.messages)
                for block in message.content
                if isinstance(block, ToolResultBlock)
            ),
            None,
        )
        if result is None:
            return ModelResponse(
                Message(
                    "assistant-risky-tool", MessageRole.ASSISTANT,
                    (ToolCallBlock(ToolCall(
                        "call-write", "fixture.write", {"text": "change"}
                    )),),
                ),
                FinishReason.TOOL_CALL,
                ModelUsage(2, 1),
            )
        outcome = "approved" if result.ok else result.error_code
        return ModelResponse(
            Message(
                "assistant-risky-final", MessageRole.ASSISTANT,
                (TextBlock(f"Tool outcome: {outcome}"),),
            ),
            FinishReason.STOP,
            ModelUsage(3, 2),
        )


class OutcomeRiskyToolCallingModel(RiskyToolCallingModel):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        if response.finish_reason is not FinishReason.TOOL_CALL:
            return response
        call = next(
            block.call for block in response.message.content
            if isinstance(block, ToolCallBlock)
        )
        return ModelResponse(Message(
            response.message.message_id, response.message.role,
            (ToolCallBlock(ToolCall(
                call.call_id, call.name, call.arguments,
                outcome_ref="workspace-change",
            )),),
        ), response.finish_reason, response.usage)


class RiskyToolProvider:
    descriptor = AdapterDescriptor(
        adapter_id="fixture.agent-risky-tool",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({"fixture.write"}),
    )
    _spec = ToolSpec(
        name="fixture.write",
        description="Change fixture data.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R1,
        idempotency=ToolIdempotency.IDEMPOTENT,
        effect=ToolEffect.MUTATE,
    )

    def __init__(self) -> None:
        self.invocation_count = 0

    async def start(self, context) -> None:
        pass

    async def health(self):
        raise NotImplementedError

    async def stop(self, deadline) -> None:
        pass

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return (self._spec,)

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        self.invocation_count += 1
        return ToolResult(call.call_id, True, data={"changed": call.arguments["text"]})


class ApprovalSteeringClassifier:
    """Fixture semantic router; it proposes intent but grants no authority."""

    descriptor = AdapterDescriptor(
        "fixture.approval-steering-classifier", "1.0",
        "RuntimeInputClassifierPort", "1.0",
    )

    async def start(self, context) -> None:
        pass

    async def health(self):
        from tsm_agt.ports import HealthState, HealthStatus
        return HealthStatus(HealthState.HEALTHY)

    async def stop(self, deadline) -> None:
        pass

    async def classify_runtime_input(self, text, context):
        return {"intent": "STEER", "confidence": 0.99}


class SlowUnsafeToolProvider(RiskyToolProvider):
    descriptor = AdapterDescriptor(
        adapter_id="fixture.agent-slow-unsafe-tool",
        adapter_version="0.1.0", port_name="ToolProviderPort",
        port_version="1.0", capabilities=frozenset({"fixture.write"}),
    )
    _spec = ToolSpec(
        name="fixture.write", description="Non-idempotent side effect.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False,
        },
        risk=ToolRisk.R1, idempotency=ToolIdempotency.NON_IDEMPOTENT,
        effect=ToolEffect.MUTATE,
    )

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        self.invocation_count += 1
        await asyncio.sleep(1)
        return ToolResult(call.call_id, True, data={"changed": True})


class AgentLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_direction_supersedes_pending_approval_without_approving_it(self):
        model = RiskyToolCallingModel()
        provider = RiskyToolProvider()
        application = compose_fixture_application(
            model_adapter=model, tool_adapters=(provider,),
        )
        await application.registry.start_all()
        temp_dir = tempfile.TemporaryDirectory()
        try:
            task = await application.kernel.create_task(
                "perform guarded effect", Path(temp_dir.name)
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await application.kernel.transition_task(
                    task.task_id, state, state.value
                )
            approval = await application.kernel.run_agent_turn(
                task.task_id, "perform the original work"
            )
            self.assertIsInstance(approval, AgentTurnSuspended)
            assert isinstance(approval, AgentTurnSuspended)
            self.assertEqual(provider.invocation_count, 0)

            route = await application.kernel.route_runtime_input(
                task.task_id, "change the remaining direction", "new-direction",
                explicit_intent=RuntimeInputIntent.REPLACE,
            )
            self.assertTrue(route.applied)
            current = await application.kernel.get_task(task.task_id)
            self.assertEqual(current.state, TaskState.EXECUTING)
            self.assertIsNone(current.pending_approval)
            self.assertEqual(provider.invocation_count, 0)
            with self.assertRaises(ApprovalNotPending):
                await application.kernel.resolve_agent_approval(
                    approval.approval_request_id, ApprovalDecision.APPROVE,
                    "stale approval must not execute",
                )

            completed = await application.kernel.resume_checkpointed_agent_turn(
                task.task_id
            )
            self.assertIsInstance(completed, AgentTurnResult)
            self.assertEqual(provider.invocation_count, 0)
            self.assertIn(
                "ACTION_SUPERSEDED", completed.assistant_message.text
            )
            events = await application.kernel.dependencies.store.read_events(
                task.task_id
            )
            resolution = next(
                event for event in events
                if event.event_type == "approval.resolved"
            )
            self.assertEqual(resolution.payload["decision"], "superseded")
            queued = next(
                event for event in events
                if event.event_type == "steering.queued"
            )
            self.assertEqual(queued.payload["text"], "change the remaining direction")
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_unknown_outcome_stops_batch_until_explicit_reconciliation(self):
        model = TwoUnsafeCallsThenFinishModel()
        provider = SlowUnsafeToolProvider()
        application = compose_fixture_application(
            model_adapter=model, tool_adapters=(provider,),
        )
        await application.registry.start_all()
        temp_dir = tempfile.TemporaryDirectory()
        try:
            task = await application.kernel.create_task(
                "perform guarded effects", Path(temp_dir.name)
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await application.kernel.transition_task(
                    task.task_id, state, state.value
                )
            approval = await application.kernel.run_agent_turn(
                task.task_id, "run both", tool_timeout_seconds=0.001,
            )
            self.assertIsInstance(approval, AgentTurnSuspended)
            assert isinstance(approval, AgentTurnSuspended)
            stopped = await application.kernel.resume_agent_turn(
                task.task_id, approval.approval_request_id,
                approval.payload_hash, ApprovalDecision.APPROVE,
                "approved exact action",
            )
            self.assertIsInstance(stopped, AgentClarificationSuspended)
            assert isinstance(stopped, AgentClarificationSuspended)
            self.assertEqual(stopped.kind, "OUTCOME_RECONCILIATION")
            self.assertEqual(provider.invocation_count, 1)
            self.assertEqual(model.calls, 1)
            waiting = await application.kernel.get_task(task.task_id)
            self.assertEqual(waiting.state, TaskState.AWAITING_USER)
            self.assertEqual(
                waiting.tool_executions[
                    f"{stopped.turn_id}:unsafe-1"
                ].state,
                ToolCommitState.UNKNOWN_OUTCOME,
            )
            self.assertEqual(
                (waiting.active_agent_checkpoint or {})[
                    "pending_tool_calls"
                ], [],
            )
            # CONFIRM_NOT_APPLIED turns the unknown outcome into an explicit
            # failure. Prose alone cannot close a failed Tool batch, so the turn
            # is suspended for continuation instead of reported as finished.
            blocked = await application.kernel.resolve_agent_clarification(
                stopped.request_id, stopped.resume_token,
                selected_choice="CONFIRM_NOT_APPLIED",
            )
            self.assertIsInstance(blocked, AgentContinuationSuspended)
            assert isinstance(blocked, AgentContinuationSuspended)
            blocker = json.loads(blocked.assistant_message.text)
            self.assertEqual(blocker["boundary"], "unresolved_tool_batch")
            self.assertEqual(
                [item["error_code"] for item in blocker["failures"]],
                ["NOT_APPLIED"],
            )
            self.assertEqual(provider.invocation_count, 1)
            self.assertEqual(model.calls, 2)
            reconciled = (
                await application.kernel.get_task(task.task_id)
            ).tool_executions[f"{stopped.turn_id}:unsafe-1"]
            self.assertEqual(reconciled.state, ToolCommitState.UNKNOWN_OUTCOME)
            self.assertEqual(reconciled.reconciled_outcome, "NOT_APPLIED")
            events = await application.registry.require(
                RuntimeStorePort
            ).read_events(task.task_id)
            event_types = [item.event_type for item in events]
            self.assertIn("outcome_reconciliation.requested", event_types)
            self.assertIn("tool.outcome_reconciled", event_types)
            self.assertEqual(event_types.count("tool.started"), 1)
            candidates = await application.kernel.list_session_resume_candidates(
                waiting.session_id, Path(temp_dir.name)
            )
            self.assertFalse(any(
                item.reason_code == "unknown_side_effect_outcome"
                for item in candidates
            ))
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_unknown_outcome_reconciliation_survives_sqlite_restart(self):
        temp_dir = tempfile.TemporaryDirectory()
        database = Path(temp_dir.name) / "runtime.db"
        first_model = TwoUnsafeCallsThenFinishModel()
        first_provider = SlowUnsafeToolProvider()
        first = compose_fixture_application(
            model_adapter=first_model, tool_adapters=(first_provider,),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await first.registry.start_all()
        task = await first.kernel.create_task(
            "restart guarded effect", Path(temp_dir.name)
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await first.kernel.transition_task(
                task.task_id, state, state.value
            )
        approval = await first.kernel.run_agent_turn(
            task.task_id, "run both", tool_timeout_seconds=0.001,
        )
        assert isinstance(approval, AgentTurnSuspended)
        stopped = await first.kernel.resume_agent_turn(
            task.task_id, approval.approval_request_id, approval.payload_hash,
            ApprovalDecision.APPROVE, "approved exact action",
        )
        assert isinstance(stopped, AgentClarificationSuspended)
        await first.registry.stop_all()

        second_model = TwoUnsafeCallsThenFinishModel()
        second_provider = SlowUnsafeToolProvider()
        second = compose_fixture_application(
            model_adapter=second_model, tool_adapters=(second_provider,),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            restored = await second.kernel.get_task(task.task_id)
            self.assertEqual(restored.state, TaskState.AWAITING_USER)
            self.assertEqual(
                restored.pending_clarification.kind.value,
                "OUTCOME_RECONCILIATION",
            )
            blocked = await second.kernel.resolve_agent_clarification(
                stopped.request_id, stopped.resume_token,
                selected_choice="CONFIRM_NOT_APPLIED",
            )
            self.assertIsInstance(blocked, AgentContinuationSuspended)
            self.assertEqual(second_provider.invocation_count, 0)
            self.assertEqual(second_model.calls, 1)
        finally:
            await second.registry.stop_all()
            temp_dir.cleanup()

    async def _create_executing_task(self, model):
        application = compose_fixture_application(model_adapter=model)
        await application.registry.start_all()
        temp_dir = tempfile.TemporaryDirectory()
        task = await application.kernel.create_task(
            "run agent loop", Path(temp_dir.name), task_id="task-agent"
        )
        for state in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, state, f"move to {state.value}"
            )
        return application, temp_dir, task

    async def _create_risky_agent(self, *, store=None, model=None):
        provider = RiskyToolProvider()
        application = compose_fixture_application(
            model_adapter=model or RiskyToolCallingModel(),
            tool_adapters=(provider,),
            store_adapter=store,
        )
        await application.registry.start_all()
        temp_dir = tempfile.TemporaryDirectory()
        task = await application.kernel.create_task(
            "run risky agent", Path(temp_dir.name), task_id="task-risky-agent"
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, state, f"move to {state.value}"
            )
        return application, temp_dir, task, provider

    async def _install_mutation_outcome(self, application, task) -> None:
        from tsm_agt.core import TaskSpecProposal, TaskSpecSnapshot
        current = await application.kernel.get_task_spec(task.task_id)
        proposal = TaskSpecProposal.from_data({
            "schema_version": 1,
            "goal": task.goal,
            "scope": ["fixture"],
            "constraints": [],
            "outcomes": [{
                "outcome_id": "workspace-change",
                "description": "Commit the requested change",
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
        await application.kernel._append_events(task.task_id, ((
            "task_spec.revised", {"snapshot": spec.to_data()},
        ),))

    async def test_agent_approval_resumes_same_turn_and_finishes(self) -> None:
        application, temp_dir, task, provider = await self._create_risky_agent()
        try:
            suspended = await application.kernel.run_agent_turn(task.task_id, "change it")
            self.assertIsInstance(suspended, AgentTurnSuspended)
            assert isinstance(suspended, AgentTurnSuspended)
            self.assertEqual(provider.invocation_count, 0)
            with self.assertRaisesRegex(ApprovalNotPending, "resume_agent_turn"):
                await application.kernel.resolve_approval(
                    task.task_id, suspended.approval_request_id, suspended.payload_hash,
                    ApprovalDecision.APPROVE, "wrong API",
                )

            completed = await application.kernel.resume_agent_turn(
                task.task_id, suspended.approval_request_id, suspended.payload_hash,
                ApprovalDecision.APPROVE, "approved exact change",
            )
            self.assertIsInstance(completed, AgentTurnResult)
            assert isinstance(completed, AgentTurnResult)
            self.assertEqual(completed.turn_id, suspended.turn_id)
            self.assertEqual(completed.assistant_message.text, "Tool outcome: approved")
            self.assertEqual(completed.model_calls, 2)
            self.assertEqual(completed.tool_calls, 1)
            self.assertEqual(provider.invocation_count, 1)
            live = await application.kernel.get_task(task.task_id)
            self.assertEqual(
                (live.active_agent_checkpoint or {}).get(
                    "pending_user_action", {}
                ), {},
            )
            store = application.registry.require(RuntimeStorePort)
            event_types = [event.event_type for event in await store.read_events(task.task_id)]
            self.assertIn("checkpoint.saved", event_types)
            self.assertIn("turn.resumed", event_types)
            self.assertEqual(event_types[-2:], ["llm.completed", "turn.completed"])
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_agent_rejection_returns_failure_to_model_and_finishes(self) -> None:
        application, temp_dir, task, provider = await self._create_risky_agent()
        try:
            suspended = await application.kernel.run_agent_turn(task.task_id, "change it")
            assert isinstance(suspended, AgentTurnSuspended)
            completed = await application.kernel.resume_agent_turn(
                task.task_id, suspended.approval_request_id, suspended.payload_hash,
                ApprovalDecision.DENY, "keep workspace unchanged",
            )
            assert isinstance(completed, AgentTurnResult)
            self.assertEqual(completed.assistant_message.text, "Tool outcome: PERMISSION_DENIED")
            self.assertEqual(provider.invocation_count, 0)
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_agent_approval_checkpoint_resumes_after_sqlite_restart(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        database = Path(temp_dir.name) / "runtime.db"
        first, workspace, task, first_provider = await self._create_risky_agent(
            store=SQLiteRuntimeStore(database)
        )
        try:
            suspended = await first.kernel.run_agent_turn(task.task_id, "change it")
            assert isinstance(suspended, AgentTurnSuspended)
            self.assertEqual(first_provider.invocation_count, 0)
        finally:
            await first.registry.stop_all()

        second_provider = RiskyToolProvider()
        second = compose_fixture_application(
            model_adapter=RiskyToolCallingModel(),
            tool_adapters=(second_provider,),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            completed = await second.kernel.resume_agent_turn(
                task.task_id, suspended.approval_request_id, suspended.payload_hash,
                ApprovalDecision.APPROVE, "approved after restart",
            )
            assert isinstance(completed, AgentTurnResult)
            self.assertEqual(completed.assistant_message.text, "Tool outcome: approved")
            self.assertEqual(second_provider.invocation_count, 1)
        finally:
            await second.registry.stop_all()
            workspace.cleanup()
            temp_dir.cleanup()

    async def test_outcome_ref_survives_approval_sqlite_restart(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        database = Path(temp_dir.name) / "runtime.db"
        first, workspace, task, first_provider = await self._create_risky_agent(
            store=SQLiteRuntimeStore(database),
            model=OutcomeRiskyToolCallingModel(),
        )
        await self._install_mutation_outcome(first, task)
        try:
            suspended = await first.kernel.run_agent_turn(
                task.task_id, "change it"
            )
            assert isinstance(suspended, AgentTurnSuspended)
        finally:
            await first.registry.stop_all()

        second_provider = RiskyToolProvider()
        second = compose_fixture_application(
            model_adapter=OutcomeRiskyToolCallingModel(),
            tool_adapters=(second_provider,),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            completed = await second.kernel.resume_agent_turn(
                task.task_id, suspended.approval_request_id,
                suspended.payload_hash, ApprovalDecision.APPROVE,
                "approved after restart",
            )
            self.assertIsInstance(completed, AgentTurnResult)
            snapshot = await second.kernel.get_task(task.task_id)
            execution = next(iter(snapshot.tool_executions.values()))
            self.assertEqual(second_provider.invocation_count, 1)
            self.assertIsNone(execution.call.outcome_ref)
            events = await second.kernel.dependencies.store.read_events(task.task_id)
            self.assertFalse(any(
                item.event_type == "task_outcome.binding_decided" for item in events
            ))
            self.assertTrue(all(
                any(item.event_type == event_type for item in events)
                for event_type in (
                    "tool.requested", "policy.evaluated", "approval.requested",
                    "approval.resolved", "tool.started", "tool.completed",
                )
            ))
        finally:
            await second.registry.stop_all()
            workspace.cleanup()
            temp_dir.cleanup()

    async def test_tool_call_result_and_final_answer_form_one_turn(self) -> None:
        application, temp_dir, task = await self._create_executing_task(
            ToolCallingModelProvider()
        )
        try:
            result = await application.kernel.run_agent_turn(
                task.task_id, "inspect this"
            )

            self.assertEqual(result.assistant_message.text, "Tool observed: inspect this")
            self.assertEqual(result.model_calls, 2)
            self.assertEqual(result.tool_calls, 1)
            self.assertEqual(result.usage, ModelUsage(3, 3))
            self.assertEqual(result.messages[0].role, MessageRole.USER)
            self.assertFalse(any(
                message.message_id.startswith("working-memory-context-")
                for message in result.messages
            ))
            self.assertEqual(result.messages[-1].role, MessageRole.ASSISTANT)
            tool_message = next(
                message for message in result.messages
                if message.role is MessageRole.TOOL
            )
            self.assertEqual(tool_message.content[0].result.call_id, f"call-{result.turn_id}")

            store = application.registry.require(RuntimeStorePort)
            events = await store.read_events(task.task_id)
            event_types = [event.event_type for event in events]
            for expected in (
                "turn.started", "tool.requested", "policy.evaluated",
                "tool.prepared", "tool.started", "tool.completed",
                "turn.completed",
            ):
                self.assertIn(expected, event_types)
            self.assertLess(
                event_types.index("tool.requested"), event_types.index("tool.completed")
            )
            model_events = [
                event for event in events if event.event_type == "llm.completed"
            ]
            self.assertEqual(len(model_events), 2)
            self.assertNotEqual(
                model_events[0].payload["effective_prompt_hash"],
                model_events[1].payload["effective_prompt_hash"],
            )
            configuration = await application.kernel.get_effective_configuration(
                task.task_id
            )
            self.assertTrue(all(
                event.payload["prompt_manifest_hash"]
                == configuration.prompt_manifest_hash
                for event in model_events
            ))
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_provider_without_tool_capability_fails_before_turn_event(self) -> None:
        application, temp_dir, task = await self._create_executing_task(
            EchoModelProvider()
        )
        try:
            store = application.registry.require(RuntimeStorePort)
            before = await store.read_events(task.task_id)
            with self.assertRaisesRegex(ProviderCapabilityMismatch, "does not support tools"):
                await application.kernel.run_agent_turn(task.task_id, "inspect")
            self.assertEqual(await store.read_events(task.task_id), before)
            self.assertEqual(
                (await application.kernel.get_task(task.task_id)).state, TaskState.EXECUTING
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_unknown_tool_rejects_entire_batch_before_execution(self) -> None:
        application, temp_dir, task = await self._create_executing_task(
            UnknownToolThenFinishModel()
        )
        try:
            with self.assertRaisesRegex(
                ModelInvocationFailed, "invalid_tool_batch"
            ):
                await application.kernel.run_agent_turn(task.task_id, "hello")
            store = application.registry.require(RuntimeStorePort)
            self.assertNotIn(
                "tool.started",
                [event.event_type for event in await store.read_events(task.task_id)],
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_duplicate_tool_call_ids_fail_closed(self) -> None:
        application, temp_dir, task = await self._create_executing_task(
            DuplicateToolCallModel()
        )
        try:
            with self.assertRaisesRegex(ModelInvocationFailed, "duplicate"):
                await application.kernel.run_agent_turn(task.task_id, "hello")
            self.assertEqual(
                (await application.kernel.get_task(task.task_id)).state, TaskState.FAILED
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_recoverable_tool_protocol_error_retries_exactly_once(self) -> None:
        model = CorrectableProtocolModel()
        application, temp_dir, task = await self._create_executing_task(model)
        try:
            result = await application.kernel.run_agent_turn(
                task.task_id, "inspect", max_model_calls=4
            )
            self.assertEqual(
                result.assistant_message.text,
                "Corrected after one protocol retry.",
            )
            self.assertEqual(result.model_calls, 2)
            self.assertEqual(model.calls, 2)
            events = await application.registry.require(
                RuntimeStorePort
            ).read_events(task.task_id)
            retries = [
                event for event in events
                if event.event_type == "llm.protocol_retry_requested"
            ]
            self.assertEqual(len(retries), 1)
            self.assertEqual(
                retries[0].payload["reason_code"],
                "missing_evidence_question_envelope",
            )
            self.assertFalse(any(
                event.event_type == "tool.started" for event in events
            ))
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_tool_protocol_retry_fails_closed_after_one_correction(self) -> None:
        model = CorrectableProtocolModel(always_invalid=True)
        application, temp_dir, task = await self._create_executing_task(model)
        try:
            with self.assertRaisesRegex(
                ModelInvocationFailed, "after one correction"
            ):
                await application.kernel.run_agent_turn(
                    task.task_id, "inspect", max_model_calls=4
                )
            self.assertEqual(model.calls, 2)
            events = await application.registry.require(
                RuntimeStorePort
            ).read_events(task.task_id)
            self.assertEqual(sum(
                event.event_type == "llm.protocol_retry_requested"
                for event in events
            ), 1)
            self.assertFalse(any(
                event.event_type == "tool.started" for event in events
            ))
            self.assertEqual(
                (await application.kernel.get_task(task.task_id)).state,
                TaskState.INTERRUPTED,
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_model_call_limit_stops_before_unconsumable_tool_execution(self) -> None:
        application, temp_dir, task = await self._create_executing_task(
            AlwaysCallToolModel()
        )
        try:
            with self.assertRaises(AgentLoopLimitExceeded) as raised:
                await application.kernel.run_agent_turn(
                    task.task_id, "loop", max_model_calls=2
                )
            self.assertEqual(raised.exception.limit, "max_model_calls")
            store = application.registry.require(RuntimeStorePort)
            event_types = [
                event.event_type for event in await store.read_events(task.task_id)
            ]
            self.assertEqual(event_types.count("tool.started"), 1)
            self.assertEqual(event_types[-3:], [
                "agent.limit_exceeded",
                "turn.failed",
                "task.state_changed",
            ])
            self.assertEqual(
                (await application.kernel.get_task(task.task_id)).state, TaskState.FAILED
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_last_budget_window_forces_final_answer_without_more_tools(self) -> None:
        application, temp_dir, task = await self._create_executing_task(
            WrapUpAwareModel()
        )
        progress = []
        try:
            result = await application.kernel.run_agent_turn(
                task.task_id, "investigate then conclude", max_model_calls=3,
                on_progress=progress.append,
            )
            self.assertEqual(
                result.assistant_message.text, "Concluding from collected evidence."
            )
            self.assertEqual(result.model_calls, 2)
            self.assertEqual(result.tool_calls, 1)
            self.assertTrue(any(
                item.kind.value == "wrap_up" and item.model_call == 2
                for item in progress
            ))
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_wrap_up_tool_markup_gets_one_correction_not_false_success(self) -> None:
        model = WrapUpProtocolViolationThenAnswerModel()
        application, temp_dir, task = await self._create_executing_task(model)
        try:
            result = await application.kernel.run_agent_turn(
                task.task_id, "investigate then conclude", max_model_calls=5
            )
            self.assertEqual(
                result.assistant_message.text,
                "Direct final answer from collected evidence.",
            )
            self.assertEqual(model.wrap_up_attempts, 2)
            events = await application.registry.require(
                RuntimeStorePort
            ).read_events(task.task_id)
            retries = [
                event for event in events
                if event.event_type == "llm.protocol_retry_requested"
            ]
            self.assertEqual(len(retries), 1)
            self.assertEqual(
                retries[0].payload["reason_code"],
                "tool_call_emitted_while_disabled",
            )
            self.assertFalse(any(
                "<tool_use" in message.text for message in result.messages
            ))
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_tool_call_limit_rejects_entire_batch_before_execution(self) -> None:
        application, temp_dir, task = await self._create_executing_task(
            TwoToolCallsModel()
        )
        try:
            with self.assertRaises(AgentLoopLimitExceeded) as raised:
                await application.kernel.run_agent_turn(
                    task.task_id, "too many", max_tool_calls=1
                )
            self.assertEqual(raised.exception.limit, "max_tool_calls")
            store = application.registry.require(RuntimeStorePort)
            event_types = [
                event.event_type for event in await store.read_events(task.task_id)
            ]
            self.assertNotIn("tool.requested", event_types)
            self.assertNotIn("tool.started", event_types)
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
