from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider, EchoToolProvider
from tsm_agt.adapters.builtin.core_tools import CoreReadOnlyToolProvider
from tsm_agt.adapters.fixture.runtime_store import InMemoryRuntimeStore
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.tool_execution_configuration import load_max_parallel_tool_calls
from tsm_agt.core import AgentTurnSuspended, TaskState
from tsm_agt.core.tool_concurrency import supports_parallel_execution
from tsm_agt.ports import (
    FinishReason, Message, MessageRole, ModelResponse, ProviderCapabilities,
    RuntimeStorePort, TextBlock, ToolCall, ToolCallBlock, ToolEffect,
    ToolResult, ToolResultAuthority, ToolResultBlock, ToolRisk, ToolProtocol,
)


class BatchModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=32768)

    def __init__(self, calls):
        super().__init__()
        self.calls = calls
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return ModelResponse(Message(
                "batch", MessageRole.ASSISTANT,
                tuple(ToolCallBlock(call) for call in self.calls),
            ), FinishReason.TOOL_CALL)
        return ModelResponse(Message(
            "final", MessageRole.ASSISTANT, (TextBlock("done"),),
        ), FinishReason.STOP)


class ObservedTools(EchoToolProvider):
    def __init__(self):
        super().__init__()
        self.active = self.max_active = 0
        self.timeline = []
        self.finished_second = asyncio.Event()
        self.hold_first = False
        self.release = None
        self.started = asyncio.Event()
        self.cancelled = set()
        self.counts = {}
        self.fail = set()

    async def list_tools(self):
        return (
            replace(self._spec, effect=ToolEffect.OBSERVE,
                    result_authority=ToolResultAuthority.WORKSPACE_FACT),
            replace(self._spec, name="fixture.serial", is_concurrency_safe=False),
            replace(self._spec, name="fixture.write", risk=ToolRisk.R1,
                    is_read_only=False, effect=ToolEffect.MUTATE),
        )

    async def invoke(self, call, context):
        self.counts[call.call_id] = self.counts.get(call.call_id, 0) + 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.timeline.append(("start", call.call_id))
        self.started.set()
        try:
            if self.hold_first and call.call_id == "a":
                await asyncio.wait_for(self.finished_second.wait(), 2)
            elif self.release is not None and call.call_id == "a":
                await self.release.wait()
            else:
                await asyncio.sleep(0.01)
            if call.call_id == "b":
                self.finished_second.set()
            if call.call_id in self.fail:
                raise RuntimeError("independent tool failure")
            return ToolResult(call.call_id, True, {"text": call.arguments["text"]})
        except asyncio.CancelledError:
            self.cancelled.add(call.call_id)
            raise
        finally:
            self.active -= 1
            self.timeline.append(("end", call.call_id))


class YieldingStore(InMemoryRuntimeStore):
    """Expose concurrent read-modify-write races hidden by sync fixture IO."""

    def __init__(self):
        super().__init__()
        self.checkpoints = []

    async def load_task(self, task_id):
        result = await super().load_task(task_id)
        await asyncio.sleep(0)
        return result

    async def commit(self, unit):
        await asyncio.sleep(0)
        result = await super().commit(unit)
        checkpoint = unit.next_state.get("active_agent_checkpoint")
        if checkpoint:
            self.checkpoints.append(checkpoint)
        return result


def calls(*names):
    return tuple(ToolCall(name, "fixture.echo", {"text": name}) for name in names)


def result_ids(messages):
    return [
        block.result.call_id for message in messages for block in message.content
        if isinstance(block, ToolResultBlock)
    ]


class ToolConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    async def application(self, root, batch, tools=None, limit=8, store=None):
        model = BatchModel(batch)
        tools = tools or ObservedTools()
        app = compose_fixture_application(
            model_adapter=model, tool_adapters=(tools,), store_adapter=store,
            max_parallel_tool_calls=limit,
        )
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await app.kernel.create_task("read independent inputs", root)
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await app.kernel.transition_task(task.task_id, state, state.value)
        return app, task, model, tools

    async def test_independent_io_overlaps_but_results_and_checkpoints_are_ordered(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = ObservedTools()
            tools.hold_first = True
            store = YieldingStore()
            app, task, model, tools = await self.application(
                Path(directory), calls("a", "b"), tools, store=store,
            )
            await app.kernel.run_agent_turn(task.task_id, "read both")
            self.assertEqual(tools.max_active, 2)
            self.assertEqual(tools.timeline, [
                ("start", "a"), ("start", "b"), ("end", "b"), ("end", "a"),
            ])
            self.assertEqual(result_ids(model.requests[-1].messages), ["a", "b"])
            self.assertTrue(store.checkpoints)
            for checkpoint in store.checkpoints:
                ids = result_ids(tuple(Message.from_data(m) for m in checkpoint["messages"]))
                self.assertEqual(ids, sorted(ids))
            executions = (await app.kernel.get_task(task.task_id)).tool_executions
            self.assertEqual(len(executions), 2)
            self.assertTrue(all(item.result.ok for item in executions.values()))

    async def test_limit_and_serial_barrier(self):
        with tempfile.TemporaryDirectory() as directory:
            batch = (*calls("a", "b"), ToolCall("barrier", "fixture.serial", {"text": "barrier"}), *calls("c", "d", "e"))
            app, task, model, tools = await self.application(Path(directory), batch, limit=2)
            await app.kernel.run_agent_turn(task.task_id, "read inputs")
            self.assertEqual(tools.max_active, 2)
            trace = tools.timeline
            self.assertLess(trace.index(("end", "a")), trace.index(("start", "barrier")))
            self.assertLess(trace.index(("end", "b")), trace.index(("start", "barrier")))
            self.assertLess(trace.index(("end", "barrier")), trace.index(("start", "c")))
            self.assertEqual(result_ids(model.requests[-1].messages), [c.call_id for c in batch])

    async def test_builtin_file_reads_overlap_in_worker_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a.txt", "b.txt"):
                (root / name).write_text(name, encoding="utf-8")
            tools = CoreReadOnlyToolProvider()
            read = tools._read_file
            rendezvous = threading.Barrier(2, timeout=2)

            def simultaneous_read(call, context):
                rendezvous.wait()
                return read(call, context)

            tools._read_file = simultaneous_read
            batch = tuple(ToolCall(name, "core.read_file", {"path": name}) for name in ("a.txt", "b.txt"))
            app, task, model, _ = await self.application(root, batch, tools)
            await app.kernel.run_agent_turn(task.task_id, "read both files")
            results = [b.result for m in model.requests[-1].messages for b in m.content if isinstance(b, ToolResultBlock)]
            self.assertEqual([r.call_id for r in results], ["a.txt", "b.txt"])
            self.assertTrue(all(r.ok for r in results))

    async def test_limit_one_disables_parallel_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            app, task, _, tools = await self.application(Path(directory), calls("a", "b"), limit=1)
            await app.kernel.run_agent_turn(task.task_id, "read inputs")
            self.assertEqual(tools.max_active, 1)

    async def test_snapshot_reports_runtime_concurrency_separately_from_model(self):
        with tempfile.TemporaryDirectory() as directory:
            app, task, model, _ = await self.application(Path(directory), calls("a", "b"), limit=2)
            snapshot = await app.kernel.get_effective_configuration(task.task_id)
            self.assertEqual(snapshot.execution["max_parallel_tool_calls"], 2)
            self.assertTrue(snapshot.execution["parallel_tools"])
            self.assertFalse(model.capabilities.parallel_tools)

    async def test_one_failure_does_not_drop_sibling_results(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = ObservedTools()
            tools.fail.add("b")
            app, task, model, _ = await self.application(Path(directory), calls("a", "b", "c"), tools)
            await app.kernel.run_agent_turn(task.task_id, "read inputs")
            results = [b.result for m in model.requests[-1].messages for b in m.content if isinstance(b, ToolResultBlock)]
            self.assertEqual([r.call_id for r in results], ["a", "b", "c"])
            self.assertEqual([r.ok for r in results], [True, False, True])

    async def test_timeout_does_not_cancel_successful_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = ObservedTools()
            tools.release = asyncio.Event()
            app, task, model, _ = await self.application(Path(directory), calls("a", "b"), tools)
            await app.kernel.run_agent_turn(task.task_id, "read inputs", tool_timeout_seconds=0.04)
            results = [b.result for m in model.requests[-1].messages for b in m.content if isinstance(b, ToolResultBlock)]
            self.assertEqual([(r.call_id, r.ok, r.error_code) for r in results], [("a", False, "TIMEOUT"), ("b", True, None)])
            self.assertEqual(tools.active, 0)

    async def test_write_approval_is_a_barrier_for_later_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            batch = (*calls("a", "b"), ToolCall("write", "fixture.write", {"text": "write"}), *calls("c", "d"))
            app, task, _, tools = await self.application(Path(directory), batch)
            result = await app.kernel.run_agent_turn(task.task_id, "read inputs then write")
            self.assertIsInstance(result, AgentTurnSuspended)
            self.assertEqual(tools.counts, {"a": 1, "b": 1})
            current = await app.kernel.get_task(task.task_id)
            self.assertEqual([c["call_id"] for c in current.active_agent_checkpoint["pending_tool_calls"]], ["write", "c", "d"])

    async def test_authoritative_spec_can_withdraw_parallel_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            app, task, _, tools = await self.application(Path(directory), calls("a", "b", "c"))
            find = app.kernel._find_tool

            async def changed_spec(name):
                provider, spec = await find(name)
                # The initial visible tool set allowed concurrency; the live
                # registry withdraws that permission before actual invocation.
                return provider, (
                    replace(spec, is_concurrency_safe=False) if tools.active else spec
                )

            app.kernel._find_tool = changed_spec
            await app.kernel.run_agent_turn(task.task_id, "read inputs")
            self.assertEqual(tools.max_active, 1)
            self.assertEqual(list(tools.counts), ["a", "b", "c"])

    async def test_cancel_settles_workers_and_resume_reuses_completed_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = ObservedTools()
            tools.release = asyncio.Event()
            app, task, model, _ = await self.application(
                root, calls("a", "b"), tools, store=SQLiteRuntimeStore(root / "runtime.db"),
            )
            turn = asyncio.create_task(app.kernel.run_agent_turn(task.task_id, "read both"))
            await asyncio.wait_for(tools.finished_second.wait(), 2)
            # Wait for b's result to be committed to the execution ledger.
            for _ in range(100):
                current = await app.kernel.get_task(task.task_id)
                if any(e.call.call_id == "b" and e.result for e in current.tool_executions.values()):
                    break
                await asyncio.sleep(0.001)
            else:
                self.fail("completed sibling was not committed")
            turn.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await turn
            self.assertEqual(tools.active, 0)
            self.assertEqual(tools.cancelled, {"a"})
            tools.release.set()
            await app.kernel.resume_checkpointed_agent_turn(task.task_id)
            self.assertEqual(tools.counts, {"a": 2, "b": 1})
            self.assertEqual(result_ids(model.requests[-1].messages), ["a", "b"])
            events = await app.registry.require(RuntimeStorePort).read_events(task.task_id)
            audited = [e.payload["tool_call_id"] for e in events if e.event_type == "conversation.tool_result_recorded"]
            self.assertEqual(audited, ["a", "b"])


class ToolConcurrencyConfigurationTest(unittest.TestCase):
    def test_yaml_limit_reaches_both_application_compositions(self):
        from tests.test_model_profiles import _configuration
        from tsm_agt.bootstrap import (
            compose_openai_compatible_readonly_application_from_config,
            compose_openai_compatible_engineering_application_from_config,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = root / "config.yaml"
            configuration.write_text(
                _configuration() + "\nexecution:\n  max_parallel_tool_calls: 2\n",
                encoding="utf-8",
            )
            for compose in (
                compose_openai_compatible_readonly_application_from_config,
                compose_openai_compatible_engineering_application_from_config,
            ):
                with self.subTest(compose=compose.__name__):
                    app = compose(configuration, root / "runtime.db")
                    self.assertEqual(app.kernel.dependencies.max_parallel_tool_calls, 2)

    def test_read_only_flag_does_not_override_effect_protocol_or_state_safety(self):
        spec = EchoToolProvider._spec
        self.assertTrue(supports_parallel_execution(spec))
        for changes in (
            {"is_concurrency_safe": False}, {"is_read_only": False},
            {"is_internal_state": True}, {"effect": ToolEffect.MUTATE},
            {"protocol": ToolProtocol.WAIT_USER},
            {"result_authority": ToolResultAuthority.PROCESS_FACT},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(supports_parallel_execution(replace(spec, **changes)))

    def test_execution_limit_is_validated_separately_from_model_capabilities(self):
        self.assertEqual(load_max_parallel_tool_calls({}), 8)
        self.assertEqual(load_max_parallel_tool_calls({"execution": {"max_parallel_tool_calls": 1}}), 1)
        for limit in (0, -1, True, "4", 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                load_max_parallel_tool_calls({"execution": {"max_parallel_tool_calls": limit}})
