from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.adapters.fixture import EchoToolProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    ApprovalDecision,
    ApprovalNotPending,
    ApprovalPayloadMismatch,
    ApprovalRequired,
    CoreToolPolicy,
    IdempotencyConflict,
    InvalidToolArguments,
    ToolNotFound,
    TaskState,
    ToolCommitState,
    ToolExecutionInProgress,
    ToolExecutionRecord,
)
from tsm_agt.ports import (
    AdapterDescriptor,
    RuntimeStorePort,
    ToolCall,
    ToolIdempotency,
    ToolInvocationContext,
    ToolResult,
    ToolRisk,
    ToolSpec,
    RuntimeEvent,
    RuntimeUnitOfWork,
)


class FailingToolProvider(EchoToolProvider):
    descriptor = EchoToolProvider.descriptor.__class__(
        adapter_id="fixture.failing-tool-provider",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({"fixture.echo"}),
    )

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        raise RuntimeError("fixture exploded")


class SlowToolProvider(FailingToolProvider):
    descriptor = EchoToolProvider.descriptor.__class__(
        adapter_id="fixture.slow-tool-provider",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({"fixture.echo"}),
    )

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        await asyncio.sleep(0.05)
        return ToolResult(call.call_id, True, data={"text": "too late"})


class RiskyToolProvider(EchoToolProvider):
    descriptor = AdapterDescriptor(
        adapter_id="fixture.risky-tool-provider",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({"fixture.write"}),
    )
    _spec = ToolSpec(
        name="fixture.write",
        description="Fixture write action that must be denied until approval exists.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R1,
        is_read_only=False,
        idempotency=ToolIdempotency.IDEMPOTENT,
    )

    def __init__(self) -> None:
        super().__init__()
        self.invocation_count = 0

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        self.invocation_count += 1
        return ToolResult(call.call_id, True, data={"unexpected": True})


class CountingToolProvider(EchoToolProvider):
    descriptor = AdapterDescriptor(
        adapter_id="fixture.counting-tool-provider",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({"fixture.echo"}),
    )

    def __init__(self) -> None:
        super().__init__()
        self.invocation_count = 0
        self.idempotency_keys: list[str | None] = []

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        self.invocation_count += 1
        self.idempotency_keys.append(context.idempotency_key)
        return await super().invoke(call, context)


class BlockingKeyedToolProvider(CountingToolProvider):
    descriptor = AdapterDescriptor(
        adapter_id="fixture.blocking-keyed-tool-provider",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({"fixture.echo"}),
    )
    _spec = ToolSpec(
        name="fixture.echo",
        description="Keyed fixture used to verify crash recovery.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R0,
        is_read_only=True,
        idempotency=ToolIdempotency.KEYED,
    )

    def __init__(self, block: bool = True) -> None:
        super().__init__()
        self.block = block
        self.entered = asyncio.Event()

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        self.invocation_count += 1
        self.idempotency_keys.append(context.idempotency_key)
        self.entered.set()
        if self.block:
            await asyncio.Future()
        return ToolResult(call.call_id, True, data={"text": call.arguments["text"]})


class BlockingNonIdempotentProvider(RiskyToolProvider):
    descriptor = AdapterDescriptor(
        adapter_id="fixture.blocking-non-idempotent-provider",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({"fixture.write"}),
    )
    _spec = ToolSpec(
        name="fixture.write",
        description="Non-idempotent fixture side effect.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R1,
        idempotency=ToolIdempotency.NON_IDEMPOTENT,
    )

    def __init__(self, block: bool = True) -> None:
        super().__init__()
        self.block = block
        self.entered = asyncio.Event()

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        self.invocation_count += 1
        self.entered.set()
        if self.block:
            await asyncio.Future()
        return ToolResult(call.call_id, True, data={"side_effect": True})


class SlowNonIdempotentProvider(BlockingNonIdempotentProvider):
    descriptor = AdapterDescriptor(
        adapter_id="fixture.slow-non-idempotent-provider",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({"fixture.write"}),
    )

    def __init__(self) -> None:
        super().__init__(block=False)

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        self.invocation_count += 1
        await asyncio.sleep(0.05)
        return ToolResult(call.call_id, True, data={"too_late": True})


class KernelToolTest(unittest.IsolatedAsyncioTestCase):
    async def _create_executing_task(self, provider=None):
        providers = None if provider is None else (provider,)
        application = compose_fixture_application(tool_adapters=providers)
        await application.registry.start_all()
        temp_dir = tempfile.TemporaryDirectory()
        task = await application.kernel.create_task(
            "invoke a tool", Path(temp_dir.name), task_id="task-1"
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

    async def test_lists_and_invokes_tool_with_trace_events(self) -> None:
        application, temp_dir, task = await self._create_executing_task()
        try:
            tools = await application.kernel.list_tools()
            result = await application.kernel.invoke_tool(
                task.task_id,
                "turn-1",
                ToolCall("call-1", "fixture.echo", {"text": "hello"}),
            )

            self.assertEqual([tool.name for tool in tools], ["fixture.echo"])
            self.assertTrue(result.ok)
            self.assertEqual(result.data, {"text": "hello"})
            store = application.registry.require(RuntimeStorePort)
            events = await store.read_events(task.task_id)
            self.assertEqual(
                [event.event_type for event in events[-4:]],
                [
                    "policy.evaluated", "tool.prepared",
                    "tool.started", "tool.completed",
                ],
            )
            self.assertEqual(events[-2].payload["call"]["call_id"], "call-1")
            self.assertEqual(
                events[-4].payload["decision"]["action"], "allow"
            )
            self.assertEqual(
                events[-4].payload["decision"]["decision_id"],
                events[-2].payload["policy_decision_id"],
            )
            self.assertEqual(
                events[-2].payload["invocation_id"],
                events[-1].payload["invocation_id"],
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_r1_tool_waits_for_approval_before_adapter_invocation(self) -> None:
        provider = RiskyToolProvider()
        application, temp_dir, task = await self._create_executing_task(provider)
        try:
            with self.assertRaises(ApprovalRequired) as raised:
                await application.kernel.invoke_tool(
                    task.task_id,
                    "turn-1",
                    ToolCall("call-1", "fixture.write", {"text": "change"}),
                )
            self.assertEqual(provider.invocation_count, 0)
            request = raised.exception.request
            suspended = await application.kernel.get_task(task.task_id)
            self.assertEqual(suspended.state, TaskState.AWAITING_APPROVAL)
            self.assertEqual(suspended.pending_approval, request)

            store = application.registry.require(RuntimeStorePort)
            events = await store.read_events(task.task_id)
            self.assertEqual(
                [event.event_type for event in events[-3:]],
                ["policy.evaluated", "approval.requested", "task.state_changed"],
            )
            decision = events[-3].payload["decision"]
            self.assertEqual(decision["action"], "require_approval")
            self.assertEqual(decision["effective_risk"], "R1")
            self.assertEqual(request.payload_hash, decision["payload_hash"])
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_policy_hash_changes_when_arguments_change(self) -> None:
        policy = CoreToolPolicy()
        first = policy.payload_hash(
            RiskyToolProvider._spec,
            ToolCall("call-1", "fixture.write", {"text": "one"}),
        )
        second = policy.payload_hash(
            RiskyToolProvider._spec,
            ToolCall("call-2", "fixture.write", {"text": "two"}),
        )
        self.assertNotEqual(first, second)

    async def test_approved_request_executes_saved_call_once(self) -> None:
        provider = RiskyToolProvider()
        application, temp_dir, task = await self._create_executing_task(provider)
        try:
            with self.assertRaises(ApprovalRequired) as raised:
                await application.kernel.invoke_tool(
                    task.task_id,
                    "turn-1",
                    ToolCall("call-1", "fixture.write", {"text": "change"}),
                )
            request = raised.exception.request
            result = await application.kernel.resolve_approval(
                task.task_id,
                request.request_id,
                request.payload_hash,
                ApprovalDecision.APPROVE,
                "user reviewed the exact payload",
            )

            self.assertTrue(result.ok)
            self.assertEqual(provider.invocation_count, 1)
            self.assertEqual(
                (await application.kernel.get_task(task.task_id)).state,
                TaskState.EXECUTING,
            )
            store = application.registry.require(RuntimeStorePort)
            events = await store.read_events(task.task_id)
            self.assertEqual(
                [event.event_type for event in events[-5:]],
                [
                    "approval.resolved", "task.state_changed",
                    "tool.prepared", "tool.started", "tool.completed",
                ],
            )
            self.assertEqual(events[-2].payload["approval_request_id"], request.request_id)
            self.assertEqual(events[-2].payload["payload_hash"], request.payload_hash)
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_denied_request_never_invokes_adapter(self) -> None:
        provider = RiskyToolProvider()
        application, temp_dir, task = await self._create_executing_task(provider)
        try:
            with self.assertRaises(ApprovalRequired) as raised:
                await application.kernel.invoke_tool(
                    task.task_id, "turn-1",
                    ToolCall("call-1", "fixture.write", {"text": "change"}),
                )
            request = raised.exception.request
            result = await application.kernel.resolve_approval(
                task.task_id, request.request_id, request.payload_hash,
                ApprovalDecision.DENY, "do not change the workspace",
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "PERMISSION_DENIED")
            self.assertEqual(provider.invocation_count, 0)
            with self.assertRaises(ApprovalNotPending):
                await application.kernel.resolve_approval(
                    task.task_id, request.request_id, request.payload_hash,
                    ApprovalDecision.APPROVE, "stale retry",
                )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_wrong_payload_hash_cannot_resolve_approval(self) -> None:
        provider = RiskyToolProvider()
        application, temp_dir, task = await self._create_executing_task(provider)
        try:
            with self.assertRaises(ApprovalRequired) as raised:
                await application.kernel.invoke_tool(
                    task.task_id, "turn-1",
                    ToolCall("call-1", "fixture.write", {"text": "one"}),
                )
            request = raised.exception.request
            changed_hash = CoreToolPolicy().payload_hash(
                RiskyToolProvider._spec,
                ToolCall("call-1", "fixture.write", {"text": "two"}),
            )
            with self.assertRaises(ApprovalPayloadMismatch):
                await application.kernel.resolve_approval(
                    task.task_id, request.request_id, changed_hash,
                    ApprovalDecision.APPROVE, "attempt changed payload",
                )
            self.assertEqual(provider.invocation_count, 0)
            self.assertEqual(
                (await application.kernel.get_task(task.task_id)).state,
                TaskState.AWAITING_APPROVAL,
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_generic_transition_cannot_bypass_pending_approval(self) -> None:
        provider = RiskyToolProvider()
        application, temp_dir, task = await self._create_executing_task(provider)
        try:
            with self.assertRaises(ApprovalRequired):
                await application.kernel.invoke_tool(
                    task.task_id, "turn-1",
                    ToolCall("call-1", "fixture.write", {"text": "change"}),
                )
            with self.assertRaisesRegex(
                Exception, "only be resumed through resolve_approval"
            ):
                await application.kernel.transition_task(
                    task.task_id, TaskState.EXECUTING, "attempt bypass"
                )
            self.assertEqual(provider.invocation_count, 0)
            self.assertEqual(
                (await application.kernel.get_task(task.task_id)).state,
                TaskState.AWAITING_APPROVAL,
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_pending_approval_survives_sqlite_restart(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        database = Path(temp_dir.name) / "runtime.db"
        first_provider = RiskyToolProvider()
        first = compose_fixture_application(
            tool_adapters=(first_provider,),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await first.registry.start_all()
        try:
            task = await first.kernel.create_task(
                "persist approval", Path(temp_dir.name), task_id="task-sqlite"
            )
            for state in (
                TaskState.INTAKE,
                TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS,
                TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await first.kernel.transition_task(
                    task.task_id, state, f"move to {state.value}"
                )
            with self.assertRaises(ApprovalRequired) as raised:
                await first.kernel.invoke_tool(
                    task.task_id, "turn-1",
                    ToolCall("call-1", "fixture.write", {"text": "persisted"}),
                )
            request = raised.exception.request
        finally:
            await first.registry.stop_all()

        second_provider = RiskyToolProvider()
        second = compose_fixture_application(
            tool_adapters=(second_provider,),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            restored = await second.kernel.get_task("task-sqlite")
            self.assertEqual(restored.state, TaskState.AWAITING_APPROVAL)
            self.assertEqual(restored.pending_approval, request)
            result = await second.kernel.resolve_approval(
                restored.task_id, request.request_id, request.payload_hash,
                ApprovalDecision.APPROVE, "approved after restart",
            )
            self.assertTrue(result.ok)
            self.assertEqual(second_provider.invocation_count, 1)
        finally:
            await second.registry.stop_all()
            temp_dir.cleanup()

    async def test_invalid_arguments_fail_before_invocation_event(self) -> None:
        application, temp_dir, task = await self._create_executing_task()
        try:
            store = application.registry.require(RuntimeStorePort)
            before = await store.read_events(task.task_id)
            with self.assertRaisesRegex(InvalidToolArguments, "missing required"):
                await application.kernel.invoke_tool(
                    task.task_id,
                    "turn-1",
                    ToolCall("call-1", "fixture.echo", {}),
                )
            self.assertEqual(await store.read_events(task.task_id), before)
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_unknown_tool_fails_before_invocation_event(self) -> None:
        application, temp_dir, task = await self._create_executing_task()
        try:
            with self.assertRaisesRegex(ToolNotFound, "missing.tool"):
                await application.kernel.invoke_tool(
                    task.task_id,
                    "turn-1",
                    ToolCall("call-1", "missing.tool", {}),
                )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_adapter_exception_becomes_failed_tool_result_and_event(self) -> None:
        application, temp_dir, task = await self._create_executing_task(
            FailingToolProvider()
        )
        try:
            result = await application.kernel.invoke_tool(
                task.task_id,
                "turn-1",
                ToolCall("call-1", "fixture.echo", {"text": "hello"}),
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "TOOL_FAILED")
            store = application.registry.require(RuntimeStorePort)
            self.assertEqual(
                (await store.read_events(task.task_id))[-1].event_type, "tool.failed"
            )
            self.assertEqual(
                (await application.kernel.get_task(task.task_id)).state,
                TaskState.EXECUTING,
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_timeout_becomes_structured_retryable_failure(self) -> None:
        application, temp_dir, task = await self._create_executing_task(
            SlowToolProvider()
        )
        try:
            result = await application.kernel.invoke_tool(
                task.task_id,
                "turn-1",
                ToolCall("call-1", "fixture.echo", {"text": "hello"}),
                timeout_seconds=0.001,
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "TIMEOUT")
            self.assertTrue(result.retryable)
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_completed_tool_call_reuses_result_without_adapter_invocation(self) -> None:
        provider = CountingToolProvider()
        application, temp_dir, task = await self._create_executing_task(provider)
        try:
            call = ToolCall("stable-call", "fixture.echo", {"text": "hello"})
            first = await application.kernel.invoke_tool(task.task_id, "turn-1", call)
            second = await application.kernel.invoke_tool(task.task_id, "turn-1", call)

            self.assertEqual(second, first)
            self.assertEqual(provider.invocation_count, 1)
            events = await application.registry.require(RuntimeStorePort).read_events(
                task.task_id
            )
            self.assertEqual(events[-1].event_type, "tool.result_reused")
            execution = (await application.kernel.get_task(task.task_id)).tool_executions[
                "turn-1:stable-call"
            ]
            self.assertEqual(execution.state, ToolCommitState.COMMITTED)
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_same_call_id_with_changed_arguments_is_rejected(self) -> None:
        provider = CountingToolProvider()
        application, temp_dir, task = await self._create_executing_task(provider)
        try:
            await application.kernel.invoke_tool(
                task.task_id, "turn-1",
                ToolCall("stable-call", "fixture.echo", {"text": "one"}),
            )
            with self.assertRaises(IdempotencyConflict):
                await application.kernel.invoke_tool(
                    task.task_id, "turn-1",
                    ToolCall("stable-call", "fixture.echo", {"text": "two"}),
                )
            self.assertEqual(provider.invocation_count, 1)
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_running_tool_reentry_is_blocked(self) -> None:
        provider = BlockingKeyedToolProvider()
        application, temp_dir, task = await self._create_executing_task(provider)
        call = ToolCall("call-running", "fixture.echo", {"text": "hello"})
        running = asyncio.create_task(
            application.kernel.invoke_tool(task.task_id, "turn-1", call)
        )
        try:
            await provider.entered.wait()
            with self.assertRaises(ToolExecutionInProgress):
                await application.kernel.invoke_tool(task.task_id, "turn-1", call)
        finally:
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_keyed_running_execution_recovers_after_sqlite_restart(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        database = Path(temp_dir.name) / "runtime.db"
        first_provider = BlockingKeyedToolProvider()
        first = compose_fixture_application(
            tool_adapters=(first_provider,),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await first.registry.start_all()
        task = await first.kernel.create_task(
            "recover keyed tool", Path(temp_dir.name), task_id="task-keyed"
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await first.kernel.transition_task(
                task.task_id, state, f"move to {state.value}"
            )
        call = ToolCall("call-keyed", "fixture.echo", {"text": "resume"})
        running = asyncio.create_task(
            first.kernel.invoke_tool(task.task_id, "turn-1", call)
        )
        await first_provider.entered.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        await first.registry.stop_all()

        second_provider = BlockingKeyedToolProvider(block=False)
        second = compose_fixture_application(
            tool_adapters=(second_provider,),
            store_adapter=SQLiteRuntimeStore(database),
        )
        await second.registry.start_all()
        try:
            result = await second.kernel.recover_tool_execution(
                task.task_id, "turn-1", call.call_id
            )
            self.assertTrue(result.ok)
            self.assertEqual(second_provider.invocation_count, 1)
            self.assertIsNotNone(second_provider.idempotency_keys[0])
            restored = await second.kernel.get_task(task.task_id)
            self.assertEqual(
                restored.tool_executions["turn-1:call-keyed"].state,
                ToolCommitState.COMMITTED,
            )
        finally:
            await second.registry.stop_all()
            temp_dir.cleanup()

    async def test_non_idempotent_running_execution_becomes_unknown_outcome(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        database = Path(temp_dir.name) / "runtime.db"
        first_provider = BlockingNonIdempotentProvider()
        first = compose_fixture_application(
            tool_adapters=(first_provider,), store_adapter=SQLiteRuntimeStore(database)
        )
        await first.registry.start_all()
        task = await first.kernel.create_task(
            "recover unsafe tool", Path(temp_dir.name), task_id="task-unsafe"
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await first.kernel.transition_task(
                task.task_id, state, f"move to {state.value}"
            )
        call = ToolCall("call-unsafe", "fixture.write", {"text": "once"})
        with self.assertRaises(ApprovalRequired) as raised:
            await first.kernel.invoke_tool(task.task_id, "turn-1", call)
        request = raised.exception.request
        running = asyncio.create_task(
            first.kernel.resolve_approval(
                task.task_id, request.request_id, request.payload_hash,
                ApprovalDecision.APPROVE, "approve once",
            )
        )
        await first_provider.entered.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        await first.registry.stop_all()

        second_provider = BlockingNonIdempotentProvider(block=False)
        second = compose_fixture_application(
            tool_adapters=(second_provider,), store_adapter=SQLiteRuntimeStore(database)
        )
        await second.registry.start_all()
        try:
            result = await second.kernel.recover_tool_execution(
                task.task_id, "turn-1", call.call_id
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "UNKNOWN_OUTCOME")
            self.assertFalse(result.retryable)
            self.assertEqual(second_provider.invocation_count, 0)
            execution = (await second.kernel.get_task(task.task_id)).tool_executions[
                "turn-1:call-unsafe"
            ]
            self.assertEqual(execution.state, ToolCommitState.UNKNOWN_OUTCOME)
            reused = await second.kernel.recover_tool_execution(
                task.task_id, "turn-1", call.call_id
            )
            self.assertEqual(reused.error_code, "UNKNOWN_OUTCOME")
            self.assertEqual(second_provider.invocation_count, 0)
        finally:
            await second.registry.stop_all()
            temp_dir.cleanup()

    async def test_non_idempotent_prepared_execution_can_start_safely(self) -> None:
        provider = BlockingNonIdempotentProvider(block=False)
        application, temp_dir, task = await self._create_executing_task(provider)
        try:
            call = ToolCall("call-prepared", "fixture.write", {"text": "once"})
            payload_hash = CoreToolPolicy().payload_hash(provider._spec, call)
            execution = ToolExecutionRecord.start(
                task_id=task.task_id,
                turn_id="turn-1",
                invocation_id="inv-prepared",
                call=call,
                payload_hash=payload_hash,
                policy_decision_id="policy-approved",
                effective_risk=ToolRisk.R1,
                approval_request_id="approval-reviewed",
                idempotency=ToolIdempotency.NON_IDEMPOTENT,
            )
            stored = application.registry.require(RuntimeStorePort)
            current = await stored.load_task(task.task_id)
            assert current is not None
            prepared_task = task.with_tool_execution(execution)
            await stored.commit(
                RuntimeUnitOfWork(
                    task.task_id, current.version, prepared_task.to_data(),
                    (RuntimeEvent(
                        "evt-prepared-test", task.task_id,
                        current.last_event_sequence + 1, "tool.prepared",
                        {"execution_id": execution.execution_id},
                    ),),
                )
            )

            result = await application.kernel.recover_tool_execution(
                task.task_id, "turn-1", call.call_id
            )
            self.assertTrue(result.ok)
            self.assertEqual(provider.invocation_count, 1)
            restored = await application.kernel.get_task(task.task_id)
            self.assertEqual(
                restored.tool_executions[execution.execution_id].state,
                ToolCommitState.COMMITTED,
            )
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()

    async def test_non_idempotent_timeout_is_unknown_outcome_not_retryable(self) -> None:
        provider = SlowNonIdempotentProvider()
        application, temp_dir, task = await self._create_executing_task(provider)
        try:
            call = ToolCall("call-timeout", "fixture.write", {"text": "once"})
            with self.assertRaises(ApprovalRequired) as raised:
                await application.kernel.invoke_tool(task.task_id, "turn-1", call)
            request = raised.exception.request
            result = await application.kernel.resolve_approval(
                task.task_id, request.request_id, request.payload_hash,
                ApprovalDecision.APPROVE, "approve once", timeout_seconds=0.001,
            )

            self.assertEqual(result.error_code, "UNKNOWN_OUTCOME")
            self.assertFalse(result.retryable)
            execution = (await application.kernel.get_task(task.task_id)).tool_executions[
                "turn-1:call-timeout"
            ]
            self.assertEqual(execution.state, ToolCommitState.UNKNOWN_OUTCOME)
        finally:
            temp_dir.cleanup()
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
