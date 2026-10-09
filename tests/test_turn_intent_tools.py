from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from tsm_agt.adapters.builtin import CoreTurnIntentToolProvider
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskState
from tsm_agt.core.single_loop.types import (
    IntentAttempt,
    IntentAttemptStatus,
    IntentRequestValidationError,
    IntentSource,
    ToolIntentRequest,
    TurnIntent,
    TurnIntentKind,
)
from tsm_agt.ports import (
    AdapterContext,
    FinishReason,
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
    ToolInvocationContext,
)


class _TurnIntentControl:
    def __init__(self) -> None:
        self.intents: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []

    async def declare(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.intents.append(dict(intent))
        return dict(intent)

    async def record_attempt(
        self,
        call_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        attempt_id = f"intent-attempt:turn-1:{call_id}"
        try:
            request = ToolIntentRequest.from_tool_arguments(arguments)
            attempt = IntentAttempt(
                schema_version=1,
                attempt_id=attempt_id,
                turn_id="turn-1",
                tool_call_id=call_id,
                requested_intent=ToolIntentRequest.requested_intent(arguments),
                normalized_intent=request.intent,
                status=(
                    IntentAttemptStatus.NORMALIZED
                    if request.normalized_effect_aliases
                    else IntentAttemptStatus.ACCEPTED
                ),
                implied_effects=request.implied_effects,
                continuation_task_id=request.continuation_task_id,
                legacy_input=request.legacy_input,
                normalized_effect_aliases=request.normalized_effect_aliases,
            )
        except IntentRequestValidationError as error:
            known_intent = ToolIntentRequest.known_intent(arguments)
            attempt = IntentAttempt(
                schema_version=1,
                attempt_id=attempt_id,
                turn_id="turn-1",
                tool_call_id=call_id,
                requested_intent=ToolIntentRequest.requested_intent(arguments),
                normalized_intent=known_intent,
                status=IntentAttemptStatus.REJECTED,
                implied_effects=ToolIntentRequest.implied_effects_for(known_intent),
                legacy_input="expected_effects" in arguments,
                validation_error=error.detail,
            )
        data = attempt.to_data()
        self.attempts.append(data)
        return data


class _IntentDeclarationModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        has_result = any(
            block.__class__.__name__ == "ToolResultBlock"
            for message in request.messages
            for block in message.content
        )
        if has_result:
            return ModelResponse(
                Message("final", MessageRole.ASSISTANT, (TextBlock("done"),)),
                FinishReason.STOP,
                ModelUsage(input_tokens=1, output_tokens=1),
            )
        return ModelResponse(
            Message(
                "intent",
                MessageRole.ASSISTANT,
                (
                    ToolCallBlock(ToolCall(
                        "declare-intent",
                        "core.turn_intent",
                        {
                            "intent": "INVESTIGATE",
                            "dependencies": [{
                                "kind": "WORKSPACE_SOURCE",
                                "reference": "src/tsm_agt",
                            }],
                            "summary": "inspect the implementation",
                        },
                    )),
                ),
            ),
            FinishReason.TOOL_CALL,
            ModelUsage(input_tokens=1, output_tokens=1),
        )


class TurnIntentToolProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = CoreTurnIntentToolProvider()
        await self.provider.start(AdapterContext(config={}, emit_event=lambda *_: None))
        self.control = _TurnIntentControl()
        self.context = ToolInvocationContext(
            invocation_id="inv-1",
            task_id="task-1",
            turn_id="turn-1",
            workspace=Path("."),
            deadline=datetime.now(),
            turn_intent_control=self.control,
        )

    async def asyncTearDown(self) -> None:
        await self.provider.stop(datetime.now())

    async def _call(self, arguments: dict[str, Any]):
        return await self.provider.invoke(
            ToolCall("call-1", "core.turn_intent", arguments),
            self.context,
        )

    async def test_direct_response_returns_an_accepted_attempt(self) -> None:
        result = await self._call({
            "intent": "DIRECT_RESPONSE",
            "summary": "answer from the supplied information",
        })

        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], IntentAttemptStatus.ACCEPTED.value)
        self.assertEqual(result.data["normalized_intent"], "DIRECT_RESPONSE")
        self.assertEqual(result.data["implied_effects"], [])
        self.assertEqual(len(self.control.attempts), 1)
        self.assertEqual(self.control.intents, [])

    async def test_runtime_inspection_derives_observe_without_model_effects(self) -> None:
        result = await self._call({
            "intent": "INSPECT_RUNTIME",
            "dependencies": [{
                "kind": "RUNTIME_TASK",
                "reference": "task-1",
            }],
        })

        self.assertTrue(result.ok)
        self.assertEqual(result.data["implied_effects"], ["observe"])
        self.assertFalse(result.data["legacy_input"])

    async def test_modify_derives_mutate_without_expected_effects(self) -> None:
        result = await self._call({"intent": "MODIFY"})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["implied_effects"], ["mutate"])

    async def test_unknown_field_returns_structured_recovery_data(self) -> None:
        result = await self._call({
            "intent": "MODIFY",
            "expected_effects": ["mutate"],
            "unexpected": True,
        })

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")
        self.assertEqual(result.data["field"], "unexpected")
        self.assertTrue(result.data["retry_safe"])
        self.assertFalse(result.data["side_effects_started"])
        self.assertEqual(
            result.data["obligation_status"],
            "recoverable_intent_attempt_recorded",
        )
        self.assertEqual(
            self.control.attempts[-1]["status"],
            IntentAttemptStatus.REJECTED.value,
        )

    async def test_spec_is_internal_r0_state(self) -> None:
        (spec,) = await self.provider.list_tools()

        self.assertTrue(spec.is_internal_state)
        self.assertEqual(spec.effect, ToolEffect.INTERNAL)
        self.assertEqual(spec.risk.value, "R0")
        self.assertFalse(spec.requires_evidence_question)
        self.assertNotIn("expected_effects", spec.parameters["properties"])


class TurnIntentKernelPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_is_persisted_and_downgrade_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application()
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("intent", Path(directory), "task-1")
                investigate = TurnIntent(
                    1,
                    "turn-1",
                    TurnIntentKind.INVESTIGATE,
                    (ToolEffect.OBSERVE,),
                    summary="inspect first",
                )
                modify = TurnIntent(
                    1,
                    "turn-1",
                    TurnIntentKind.MODIFY,
                    (ToolEffect.MUTATE,),
                    summary="apply a change",
                )
                await app.kernel.declare_turn_intent(task.task_id, "turn-1", investigate)
                updated = await app.kernel.declare_turn_intent(
                    task.task_id,
                    "turn-1",
                    modify,
                )
                with self.assertRaisesRegex(ValueError, "cannot downgrade"):
                    await app.kernel.declare_turn_intent(
                        task.task_id,
                        "turn-1",
                        TurnIntent(1, "turn-1", TurnIntentKind.DIRECT_RESPONSE),
                    )
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
            finally:
                await app.registry.stop_all()

        intent_events = [
            event for event in events
            if event.event_type.startswith("turn.intent_")
        ]
        self.assertEqual(
            [event.event_type for event in intent_events],
            ["turn.intent_declared", "turn.intent_updated"],
        )
        self.assertEqual(intent_events[-1].payload, updated)
        self.assertEqual(TurnIntent.from_data(intent_events[-1].payload), modify)

    async def test_single_loop_persists_runtime_derived_intent_and_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(model_adapter=_IntentDeclarationModel())
            await app.registry.start_all()
            try:
                self.assertIn(
                    "core.turn_intent",
                    {tool.name for tool in await app.kernel.list_tools()},
                )
                task = await app.kernel.create_task("intent", Path(directory), "task-1")
                for state in (
                    TaskState.INTAKE,
                    TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS,
                    TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id,
                        state,
                        state.value,
                    )
                turn = await app.kernel.run_agent_turn(task.task_id, "declare")
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
            finally:
                await app.registry.stop_all()

        event_types = [event.event_type for event in events]
        self.assertIn("tool.intent_attempted", event_types)
        self.assertIn("tool.intent_accepted", event_types)
        declared = next(
            event for event in events if event.event_type == "turn.intent_declared"
        )
        self.assertEqual(
            declared.payload,
            {
                "schema_version": 1,
                "turn_id": turn.turn_id,
                "intent": "INVESTIGATE",
                "expected_effects": ["observe"],
                "dependencies": [{
                    "kind": "WORKSPACE_SOURCE",
                    "reference": "src/tsm_agt",
                }],
                "summary": "inspect the implementation",
                "source": IntentSource.RUNTIME_INFERRED.value,
            },
        )
        attempt = next(
            event for event in events if event.event_type == "tool.intent_accepted"
        )
        self.assertEqual(attempt.payload["turn_id"], turn.turn_id)
        self.assertEqual(attempt.payload["implied_effects"], ["observe"])
        self.assertNotIn("expected_effects", attempt.payload)


class _InvalidIntentRecoveryModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    def __init__(self) -> None:
        super().__init__()
        self.saw_structured_error = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        tool_results = [
            block.result
            for message in request.messages
            for block in message.content
            if block.__class__.__name__ == "ToolResultBlock"
        ]
        if tool_results:
            result = tool_results[-1]
            self.saw_structured_error = (
                not result.ok
                and result.error_code == "INVALID_PARAM"
                and isinstance(result.data, dict)
                and result.data.get("field") == "unexpected"
                and result.data.get("retry_safe") is True
            )
            return ModelResponse(
                Message("recovered", MessageRole.ASSISTANT, (TextBlock("done"),)),
                FinishReason.STOP,
                ModelUsage(input_tokens=1, output_tokens=1),
            )
        return ModelResponse(
            Message(
                "invalid-intent",
                MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "invalid-intent-call",
                    "core.turn_intent",
                    {"intent": "MODIFY", "unexpected": True},
                )),),
            ),
            FinishReason.TOOL_CALL,
            ModelUsage(input_tokens=1, output_tokens=1),
        )


class TurnIntentRecoveryIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def _executing_task(self, app, directory: str, task_id: str):
        task = await app.kernel.create_task("intent", Path(directory), task_id)
        for state in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await app.kernel.transition_task(task.task_id, state, state.value)
        return task

    async def test_legacy_verify_alias_is_normalized_before_derived_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application()
            await app.registry.start_all()
            try:
                task = await self._executing_task(app, directory, "normalized-intent")
                result = await app.kernel.invoke_tool(
                    task.task_id,
                    "turn-1",
                    ToolCall(
                        "intent-call",
                        "core.turn_intent",
                        {"intent": "VERIFY", "expected_effects": ["verify"]},
                    ),
                )
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
            finally:
                await app.registry.stop_all()

        self.assertTrue(result.ok)
        normalized = next(
            event for event in events if event.event_type == "tool.intent_normalized"
        )
        self.assertEqual(
            normalized.payload["normalized_effect_aliases"],
            [{"received": "verify", "canonical": "execute"}],
        )
        self.assertEqual(normalized.payload["implied_effects"], ["execute"])
        declared = next(
            event for event in events if event.event_type == "turn.intent_declared"
        )
        self.assertEqual(declared.payload["expected_effects"], ["execute"])
        self.assertEqual(declared.payload["source"], IntentSource.RUNTIME_INFERRED.value)

    async def test_invalid_intent_attempt_is_persisted_and_returned_to_model(self) -> None:
        model = _InvalidIntentRecoveryModel()
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(model_adapter=model)
            await app.registry.start_all()
            try:
                task = await self._executing_task(app, directory, "rejected-intent")
                turn = await app.kernel.run_agent_turn(task.task_id, "modify")
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
            finally:
                await app.registry.stop_all()

        self.assertEqual(turn.assistant_message.text, "done")
        self.assertTrue(model.saw_structured_error)
        rejected = next(
            event for event in events if event.event_type == "tool.intent_rejected"
        )
        self.assertEqual(rejected.payload["requested_intent"], "MODIFY")
        self.assertEqual(rejected.payload["implied_effects"], ["mutate"])
        self.assertEqual(rejected.payload["validation_error"]["field"], "unexpected")


if __name__ == "__main__":
    unittest.main()
