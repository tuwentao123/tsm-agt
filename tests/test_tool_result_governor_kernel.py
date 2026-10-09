import asyncio
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoToolProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskState, ToolCommitState
from tsm_agt.ports import (
    AdapterDescriptor,
    RuntimeStorePort,
    ToolCall,
    ToolIdempotency,
    ToolInvocationContext,
    ToolResult,
    ToolRisk,
    ToolSpec,
)


class SensitiveToolProvider(EchoToolProvider):
    descriptor = AdapterDescriptor(
        adapter_id="fixture.sensitive-tool",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({"fixture.governed"}),
    )
    _spec = ToolSpec(
        name="fixture.governed",
        description="Return sensitive content for result-governor integration tests.",
        parameters={"type": "object", "additionalProperties": False},
        risk=ToolRisk.R0,
        is_read_only=True,
        idempotency=ToolIdempotency.IDEMPOTENT,
        max_result_tokens=16,
    )

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={"token": "ledger-secret", "payload": "x" * 400},
            message="Bearer ledger-secret",
            meta={"Authorization": "Bearer ledger-secret"},
        )


class NonIdempotentFailingProvider(SensitiveToolProvider):
    descriptor = AdapterDescriptor(
        adapter_id="fixture.non-idempotent-failing-tool",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({"fixture.unknown"}),
    )
    _spec = ToolSpec(
        name="fixture.unknown",
        description="Time out after a non-idempotent call has started.",
        parameters={"type": "object", "additionalProperties": False},
        risk=ToolRisk.R0,
        is_read_only=True,
        idempotency=ToolIdempotency.NON_IDEMPOTENT,
        max_result_tokens=16,
    )

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        await asyncio.sleep(0.05)
        return ToolResult(call_id=call.call_id, ok=True, data={"too_late": True})


class ToolResultGovernorKernelTest(unittest.IsolatedAsyncioTestCase):
    async def _create_executing_task(self, provider: EchoToolProvider):
        application = compose_fixture_application(tool_adapters=(provider,))
        await application.registry.start_all()
        temporary_directory = tempfile.TemporaryDirectory()
        task = await application.kernel.create_task(
            "govern tool result", Path(temporary_directory.name), task_id="task-governor"
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
        return application, temporary_directory, task

    async def test_governed_result_reaches_return_event_and_execution_ledger(self) -> None:
        application, temporary_directory, task = await self._create_executing_task(
            SensitiveToolProvider()
        )
        try:
            result = await application.kernel.invoke_tool(
                task.task_id,
                "turn-1",
                ToolCall("call-1", "fixture.governed"),
            )
            events = await application.registry.require(RuntimeStorePort).read_events(
                task.task_id
            )
            execution = (await application.kernel.get_task(task.task_id)).tool_executions[
                "turn-1:call-1"
            ]

            self.assertTrue(result.meta["result_governance"]["redacted"])
            self.assertFalse(result.meta["result_governance"]["provider_truncated"])
            self.assertFalse(result.truncated)
            self.assertIsInstance(result.data, dict)
            self.assertEqual(result.data["token"], "[REDACTED]")
            self.assertEqual(
                result.data["payload"], "x" * 400,
                "the governor must not compact a payload; the tool owns its size",
            )
            self.assertEqual(events[-1].payload["result"], result.to_data())
            self.assertEqual(execution.result, result)
            self.assertEqual(execution.state, ToolCommitState.COMMITTED)
            for persisted in (result.to_data(), events[-1].payload, execution.to_data()):
                self.assertNotIn("ledger-secret", json.dumps(persisted))
            self.assertTrue(result.meta["result_governance"]["redacted"])
        finally:
            temporary_directory.cleanup()
            await application.registry.stop_all()

    async def test_non_idempotent_timeout_becomes_governed_unknown_outcome(self) -> None:
        application, temporary_directory, task = await self._create_executing_task(
            NonIdempotentFailingProvider()
        )
        try:
            result = await application.kernel.invoke_tool(
                task.task_id,
                "turn-1",
                ToolCall("call-1", "fixture.unknown"),
                timeout_seconds=0.001,
            )
            execution = (await application.kernel.get_task(task.task_id)).tool_executions[
                "turn-1:call-1"
            ]

            self.assertEqual(result.error_code, "UNKNOWN_OUTCOME")
            self.assertEqual(execution.state, ToolCommitState.UNKNOWN_OUTCOME)
            self.assertEqual(execution.result, result)
            # Governed, but nothing to redact and nothing to compact: the
            # synthesized failure payload carries no secret and no size limit.
            self.assertIn("result_governance", result.meta)
            self.assertFalse(result.meta["result_governance"]["redacted"])
            self.assertFalse(result.truncated)
        finally:
            temporary_directory.cleanup()
            await application.registry.stop_all()
