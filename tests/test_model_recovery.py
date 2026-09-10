from __future__ import annotations

import unittest
from collections.abc import AsyncIterator

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.resilient_model import ResilientModelProvider
from tsm_agt.adapters.rule_based_model_recovery import (
    RuleBasedModelRecoveryPolicy,
)
from tsm_agt.ports import (
    AdapterContext, Message, MessageRole, ModelAttemptFailed,
    ModelAttemptFailure, ModelFailureCategory, ModelRecoveryExhausted,
    ModelRequest, ModelResponse, ModelRetrySafety, ModelStreamCompleted,
    ModelTextDelta, TextBlock,
)


class AttemptModel(EchoModelProvider):
    def __init__(self, attempts):
        super().__init__()
        self.attempts = list(attempts)
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        value = self.attempts.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class AttemptStreamingModel(AttemptModel):
    async def stream_complete(
        self, request: ModelRequest,
    ) -> AsyncIterator[ModelTextDelta | ModelStreamCompleted]:
        self.calls += 1
        value = self.attempts.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, list):
            for event in value:
                yield event
            return
        yield ModelStreamCompleted(value)


def response(text: str) -> ModelResponse:
    return ModelResponse(Message(
        "assistant", MessageRole.ASSISTANT, (TextBlock(text),)
    ))


def failure(category, safety, code="fixture_failure"):
    return ModelAttemptFailed(ModelAttemptFailure(
        category, safety, code, code
    ))


class ModelRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def _coordinator(self, model, attempts=3):
        policy = RuleBasedModelRecoveryPolicy(0)
        await policy.start(AdapterContext({}, lambda *_: None))
        coordinator = ResilientModelProvider(
            model, policy, max_provider_attempts=attempts
        )
        await coordinator.start(AdapterContext({}, lambda *_: None))
        return coordinator

    async def test_same_request_and_resample_use_one_bounded_flow(self):
        for safety in (
            ModelRetrySafety.SAFE_SAME_REQUEST,
            ModelRetrySafety.SAFE_RESAMPLE,
        ):
            with self.subTest(safety=safety):
                physical = AttemptModel([
                    failure(ModelFailureCategory.INVALID_RESPONSE, safety),
                    response("recovered"),
                ])
                model = await self._coordinator(physical)
                updates = []
                result = await model.complete(ModelRequest(
                    "turn", (), on_transport_progress=updates.append
                ))
                self.assertEqual(result.message.text, "recovered")
                self.assertEqual(physical.calls, 2)
                self.assertEqual(
                    [item.kind for item in updates],
                    [
                        "attempt_started", "attempt_failed",
                        "recovery_decided", "attempt_started",
                        "attempt_completed",
                    ],
                )

    async def test_failure_limit_preserves_structured_terminal_decision(self):
        physical = AttemptModel([
            failure(ModelFailureCategory.TRANSIENT_PROVIDER,
                    ModelRetrySafety.SAFE_SAME_REQUEST),
            failure(ModelFailureCategory.INVALID_RESPONSE,
                    ModelRetrySafety.SAFE_RESAMPLE),
        ])
        model = await self._coordinator(physical, attempts=2)
        with self.assertRaises(ModelRecoveryExhausted) as caught:
            await model.complete(ModelRequest("turn", ()))
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(
            caught.exception.decision.action.value, "PRESERVE_AND_INTERRUPT"
        )

    async def test_stream_falls_back_before_any_visible_output(self):
        physical = AttemptStreamingModel([
            failure(ModelFailureCategory.INVALID_RESPONSE,
                    ModelRetrySafety.SAFE_FALLBACK_TRANSPORT),
            response("fallback answer"),
        ])
        model = await self._coordinator(physical)
        events = [event async for event in model.stream_complete(
            ModelRequest("turn", ())
        )]
        self.assertEqual(events[-1].response.message.text, "fallback answer")
        self.assertEqual(physical.calls, 2)

    async def test_visible_partial_output_is_never_replayed(self):
        physical = AttemptStreamingModel([[
            ModelTextDelta("visible"),
            failure(ModelFailureCategory.TRANSIENT_PROVIDER,
                    ModelRetrySafety.SAFE_SAME_REQUEST),
        ]])
        # Raise the failure from inside the stream after the visible event.
        async def stream_with_failure(request):
            physical.calls += 1
            yield ModelTextDelta("visible")
            raise failure(
                ModelFailureCategory.TRANSIENT_PROVIDER,
                ModelRetrySafety.SAFE_SAME_REQUEST,
            )
        physical.stream_complete = stream_with_failure
        model = await self._coordinator(physical)
        emitted = []
        with self.assertRaises(ModelRecoveryExhausted) as caught:
            async for event in model.stream_complete(ModelRequest("turn", ())):
                emitted.append(event)
        self.assertEqual([event.text for event in emitted], ["visible"])
        self.assertEqual(physical.calls, 1)
        self.assertTrue(caught.exception.failure.visible_output_emitted)


if __name__ == "__main__":
    unittest.main()
