from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import AgentTurnResult, TaskState, ToolCommitState, ToolExecutionRecord
from tsm_agt.core.execution_facts import (
    EffectStatus,
    project_execution_fact,
    project_tool_execution_fact,
)
from tsm_agt.core import OrchestrationMode
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
    RuntimeStorePort,
    TextBlock,
    ToolCall,
    ToolCallBlock,
    ToolEffect,
    ToolIdempotency,
    ToolInvocationContext,
    ToolResult,
    ToolResultAuthority,
    ToolRisk,
    ToolSpec,
)


def _record(
    *,
    effect: ToolEffect,
    authority: ToolResultAuthority,
    result: ToolResult,
) -> ToolExecutionRecord:
    return ToolExecutionRecord.start(
        task_id="task-1",
        turn_id="turn-1",
        invocation_id="invocation-1",
        call=ToolCall(result.call_id, "test.effect", {}),
        payload_hash="payload-hash",
        policy_decision_id="policy-1",
        effective_risk=ToolRisk.R0,
        approval_request_id=None,
        idempotency=ToolIdempotency.IDEMPOTENT,
        effect=effect,
        result_authority=authority,
    ).finish(ToolCommitState.COMMITTED, result)


class ToolExecutionFactProjectionTest(unittest.TestCase):
    def test_workspace_read_projects_an_observation_without_delivery_fact(self) -> None:
        record = _record(
            effect=ToolEffect.OBSERVE,
            authority=ToolResultAuthority.WORKSPACE_FACT,
            result=ToolResult("call-1", True, data={"path": "src/main.py"}),
        )

        execution_fact = project_tool_execution_fact(record)

        self.assertIsNotNone(execution_fact)
        assert execution_fact is not None
        self.assertIs(execution_fact.effect, ToolEffect.OBSERVE)
        self.assertIs(execution_fact.effect_status, EffectStatus.SUCCEEDED)
        self.assertIsNone(project_execution_fact(record))

    def test_mutation_preview_does_not_project_a_delivered_effect(self) -> None:
        record = _record(
            effect=ToolEffect.MUTATE,
            authority=ToolResultAuthority.MUTATION_FACT,
            result=ToolResult("call-1", True, data={"patch": "preview"}),
        )

        execution_fact = project_tool_execution_fact(
            record,
            known_mutation_ids=frozenset(),
        )
        effect_fact = project_execution_fact(
            record,
            known_mutation_ids=frozenset(),
        )

        self.assertIsNotNone(execution_fact)
        self.assertIsNotNone(effect_fact)
        assert execution_fact is not None
        assert effect_fact is not None
        self.assertIs(execution_fact.effect_status, EffectStatus.UNKNOWN)
        self.assertEqual(execution_fact.failure_code, "MUTATION_NOT_RECORDED")
        self.assertIs(effect_fact.effect_status, EffectStatus.UNKNOWN)

    def test_recorded_mutation_projects_a_delivered_effect(self) -> None:
        record = _record(
            effect=ToolEffect.MUTATE,
            authority=ToolResultAuthority.MUTATION_FACT,
            result=ToolResult("call-1", True, data={"mutation_id": "mutation-1"}),
        )

        execution_fact = project_tool_execution_fact(
            record,
            known_mutation_ids=frozenset({"mutation-1"}),
        )

        self.assertIsNotNone(execution_fact)
        assert execution_fact is not None
        self.assertIs(execution_fact.effect_status, EffectStatus.SUCCEEDED)

    def test_failed_read_projects_a_failed_observation(self) -> None:
        record = _record(
            effect=ToolEffect.OBSERVE,
            authority=ToolResultAuthority.WORKSPACE_FACT,
            result=ToolResult(
                "call-1",
                False,
                error_code="NOT_FOUND",
                message="target does not exist",
            ),
        )

        execution_fact = project_tool_execution_fact(record)

        self.assertIsNotNone(execution_fact)
        assert execution_fact is not None
        self.assertIs(execution_fact.effect_status, EffectStatus.FAILED)
        self.assertEqual(execution_fact.failure_code, "NOT_FOUND")


class _WorkspaceReadProvider:
    descriptor = AdapterDescriptor(
        adapter_id="test.execution-effect-shadow",
        adapter_version="1",
        port_name="ToolProviderPort",
        port_version="1",
        capabilities=frozenset({"test.workspace-read"}),
    )
    _spec = ToolSpec(
        name="test.workspace_read",
        description="Return a deterministic workspace observation.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=ToolRisk.R0,
        is_read_only=True,
        is_concurrency_safe=True,
        idempotency=ToolIdempotency.IDEMPOTENT,
        effect=ToolEffect.OBSERVE,
        result_authority=ToolResultAuthority.WORKSPACE_FACT,
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
        self,
        call: ToolCall,
        context: ToolInvocationContext,
    ) -> ToolResult:
        del context
        return ToolResult(call.call_id, True, data={"path": "src/main.py"})


class _ToolThenTextModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if any(
            block.__class__.__name__ == "ToolResultBlock"
            for message in request.messages
            for block in message.content
        ):
            return ModelResponse(
                Message("final", MessageRole.ASSISTANT, (TextBlock("done"),)),
                FinishReason.STOP,
                ModelUsage(1, 1),
            )
        return ModelResponse(
            Message(
                "tool-call",
                MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall("read-1", "test.workspace_read", {})),),
            ),
            FinishReason.TOOL_CALL,
            ModelUsage(1, 1),
        )


class ExecutionEffectShadowEventTest(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_retains_committed_observation_without_completion_shadow(self) -> None:
        application = compose_fixture_application(
            model_adapter=_ToolThenTextModel(),
            tool_adapters=(_WorkspaceReadProvider(),),
        )
        await application.registry.start_all()
        directory = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(application.registry.stop_all)
        self.addCleanup(directory.cleanup)
        task = await application.kernel.create_task(
            "inspect workspace",
            Path(directory.name),
            task_id="execution-effect-shadow",
        )
        for state in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id,
                state,
                state.value,
            )

        result = await application.kernel.run_agent_turn(task.task_id, "inspect")

        self.assertIsInstance(result, AgentTurnResult)
        events = await application.registry.require(RuntimeStorePort).read_events(
            task.task_id
        )
        # Conversation turns retain the actual committed tool record. Retired
        # completion/shadow evaluators must not be required to execute a read.
        current = await application.kernel.get_task(task.task_id)
        execution = next(iter(current.tool_executions.values()))
        fact = project_tool_execution_fact(execution)
        self.assertIsNotNone(fact)
        self.assertIs(fact.effect, ToolEffect.OBSERVE)
        self.assertIs(fact.effect_status, EffectStatus.SUCCEEDED)
        self.assertTrue(any(e.event_type == "tool.completed" for e in events))
        self.assertFalse(any(e.event_type == "completion.shadow_validated" for e in events))



if __name__ == "__main__":
    unittest.main()
