from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AgentTurnCheckpoint, AgentTurnResult, TaskOutcomeStatus, TaskSpecProposal,
    TaskSpecSnapshot, TaskState,
)
from tsm_agt.core.configuration import canonical_hash
from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, FinishReason, HealthState, HealthStatus,
    Message, MessageRole, ModelRequest, ModelResponse, ModelUsage,
    OutcomeBindingMode, ProviderCapabilities, TextBlock, ToolCall,
    ToolCallBlock, ToolEffect, ToolIdempotency, ToolInvocationContext,
    ToolResult, ToolResultBlock, ToolRisk, ToolSpec,
)


def proposal(goal: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "goal": goal,
        "scope": ["."],
        "constraints": [],
        "acceptance_criteria": [{
            "criterion_id": "workspace-integrity",
            "description": "Committed workspace effects remain intact",
            "verification_kind": "workspace_integrity",
        }],
        "outcomes": [{
            "outcome_id": "inspect",
            "description": "Collect inspection evidence",
            "kind": "EVIDENCE",
            "required_effects": ["observe"],
            "required": True,
        }],
        "continuation_policy": {"mode": "NONE"},
    }


class EffectToolProvider:
    descriptor = AdapterDescriptor(
        "fixture.outcome-effects", "1.0", "ToolProviderPort", "1.0"
    )

    def __init__(self, effect: ToolEffect, *, delay: float = 0.0):
        self.effect = effect
        self.delay = delay
        self.name = f"fixture.{effect.value}"

    async def start(self, context: AdapterContext) -> None:
        del context

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY)

    async def stop(self, deadline: datetime) -> None:
        del deadline

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return (ToolSpec(
            self.name, f"Produce one {self.effect.value} result",
            {"type": "object", "properties": {}, "additionalProperties": False},
            ToolRisk.R0,
            is_read_only=self.effect is ToolEffect.OBSERVE,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=self.effect,
        ),)

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext,
    ) -> ToolResult:
        del context
        if self.delay:
            await asyncio.sleep(self.delay)
        return ToolResult(call.call_id, True, {"effect": self.effect.value})


class BoundReadModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result for message in request.messages for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        if not results:
            return ModelResponse(
                Message("model-read", MessageRole.ASSISTANT, (ToolCallBlock(
                    ToolCall("model-read", "core.list_files", {"path": "."},
                             outcome_ref="inspect"),
                ),)),
                FinishReason.TOOL_CALL, ModelUsage(1, 1),
            )
        return ModelResponse(
            Message("model-final", MessageRole.ASSISTANT, (
                TextBlock("The workspace was inspected."),
            )),
            FinishReason.STOP, ModelUsage(1, 1),
        )


class TaskOutcomeLoopTest(unittest.IsolatedAsyncioTestCase):
    async def _prepared_app(self, root: Path, *, model=None, tools=None):
        app = compose_fixture_application(
            model_adapter=model or EchoModelProvider(),
            tool_adapters=tools or (CoreReadOnlyToolProvider(),),
        )
        await app.registry.start_all()
        task = await app.kernel.create_task("inspect workspace", root)
        current = await app.kernel.get_task_spec(task.task_id)
        spec = TaskSpecSnapshot.from_proposal(
            task.task_id, current.revision + 1,
            TaskSpecProposal.from_data(proposal(task.goal)),
            current.acceptance_criteria,
        )
        await app.kernel._append_events(task.task_id, ((
            "task_spec.revised", {"snapshot": spec.to_data()},
        ),))
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await app.kernel.transition_task(
                task.task_id, state, state.value
            )
        return app, task

    async def test_runtime_discards_model_outcome_binding_and_executes_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            app, task = await self._prepared_app(Path(directory))
            try:
                result = await app.kernel.invoke_tool(
                    task.task_id, "turn-bound", ToolCall(
                        "read", "core.list_files", {"path": "."},
                        outcome_ref="inspect",
                    ),
                )
                self.assertTrue(result.ok)
                snapshot = await app.kernel.get_task(task.task_id)
                execution = next(iter(snapshot.tool_executions.values()))
                self.assertIsNone(execution.call.outcome_ref)
                self.assertEqual(
                    execution.call.outcome_binding_mode,
                    OutcomeBindingMode.FULFILLMENT,
                )
                self.assertEqual(
                    (await app.kernel.get_task_spec(task.task_id)).outcomes[0].status,
                    TaskOutcomeStatus.PENDING,
                )
                events = await app.kernel.dependencies.store.read_events(task.task_id)
                self.assertFalse(any(
                    event.event_type == "task_outcome.binding_decided"
                    for event in events
                ))
            finally:
                await app.registry.stop_all()

    async def test_agent_loop_records_unbound_execution_for_legacy_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            app, task = await self._prepared_app(
                Path(directory), model=BoundReadModel(),
            )
            try:
                result = await app.kernel.run_agent_turn(task.task_id, task.goal)
                self.assertIsInstance(result, AgentTurnResult)
                snapshot = await app.kernel.get_task(task.task_id)
                self.assertEqual(len(snapshot.tool_executions), 1)
                execution = next(iter(snapshot.tool_executions.values()))
                self.assertEqual(execution.call.name, "core.list_files")
                self.assertIsNone(execution.call.outcome_ref)
            finally:
                await app.registry.stop_all()

    async def test_timeout_persists_tool_execution_across_sqlite_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            slow = EffectToolProvider(ToolEffect.OBSERVE, delay=0.05)
            first = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(slow,),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await first.registry.start_all()
            task = await first.kernel.create_task("observe slowly", root)
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await first.kernel.transition_task(
                    task.task_id, state, state.value
                )
            result = await first.kernel.invoke_tool(
                task.task_id, "turn-timeout", ToolCall(
                    "slow-call", slow.name, {}, outcome_ref="inspect",
                ), timeout_seconds=0.001,
            )
            self.assertEqual(result.error_code, "TIMEOUT")
            await first.registry.stop_all()

            second = compose_fixture_application(
                model_adapter=EchoModelProvider(),
                tool_adapters=(EffectToolProvider(ToolEffect.OBSERVE),),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await second.registry.start_all()
            try:
                restored = await second.kernel.get_task(task.task_id)
                execution = next(iter(restored.tool_executions.values()))
                self.assertFalse(execution.result.ok)
                self.assertEqual(execution.result.error_code, "TIMEOUT")
                self.assertIsNone(execution.call.outcome_ref)
            finally:
                await second.registry.stop_all()

    async def test_legacy_event_log_outcome_projection_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            app, task = await self._prepared_app(Path(directory))
            try:
                await app.kernel._append_events(task.task_id, ((
                    "task_outcome.state_changed", {
                        "outcome_id": "inspect",
                        "status": TaskOutcomeStatus.DELIVERED.value,
                        "fulfillment_ref": "legacy:inspect:observe",
                        "reason": "legacy_replay",
                    },
                ),))
                replayed = await app.kernel.get_task_spec(task.task_id)
                self.assertEqual(
                    replayed.outcomes[0].status, TaskOutcomeStatus.DELIVERED
                )
                self.assertEqual(
                    replayed.outcomes[0].fulfillment_refs,
                    ("legacy:inspect:observe",),
                )
            finally:
                await app.registry.stop_all()

    def test_legacy_tool_call_binding_payload_remains_decodable(self):
        legacy = {
            "call_id": "legacy-read",
            "name": "core.list_files",
            "arguments": {"path": "."},
            "outcome_ref": "inspect",
            "outcome_binding_mode": "SUPPORTING",
        }
        restored = ToolCall.from_data(legacy)
        self.assertEqual(restored.outcome_ref, "inspect")
        self.assertEqual(restored.outcome_binding_mode, OutcomeBindingMode.SUPPORTING)

    def test_legacy_checkpoint_binding_fields_remain_integrity_checked(self):
        checkpoint = AgentTurnCheckpoint(
            "task-legacy", "turn-legacy", 1, (), (), (),
            0, 0, 0, 0, 2, 2, 100, 1.0,
        )
        legacy = checkpoint._content_data()
        legacy["execution_focus"] = {
            "selected_outcome_ids": ["inspect"],
            "selection_revision": 1,
            "selection_reason": "legacy",
            "source_input_id": "input-1",
        }
        legacy["active_outcome_ids"] = ["inspect"]
        legacy["checkpoint_hash"] = canonical_hash(legacy)
        restored = AgentTurnCheckpoint.from_data(legacy)
        self.assertTrue(restored.legacy_execution_focus)
        self.assertTrue(restored.legacy_active_outcome_ids)
        self.assertEqual(restored.to_data(), legacy)


if __name__ == "__main__":
    unittest.main()
