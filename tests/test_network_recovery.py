from __future__ import annotations

import unittest
from types import SimpleNamespace
from urllib.error import URLError
from unittest import mock

import yaml

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.http_json import (
    ModelProviderTransportError, UrllibHttpJsonTransport,
)
from tsm_agt.adapters.resilient_model import ResilientModelProvider
from tsm_agt.adapters.rule_based_model_recovery import RuleBasedModelRecoveryPolicy
from tsm_agt.bootstrap.model_configuration import load_model_configuration
from tsm_agt.sdk.ui_events import (
    _TURN_PROGRESS_EVENT_TYPES, _turn_progress_item,
)
from tsm_agt.ports import (
    AdapterContext, Message, MessageRole, ModelAttemptFailed,
    ModelAttemptFailure, ModelFailureCategory, ModelRecoveryAction,
    ModelRecoveryDecision, ModelRecoveryExhausted, ModelRecoveryProbe,
    ModelRequest, ModelResponse, ModelRetrySafety, TextBlock,
)


def response(text: str) -> ModelResponse:
    return ModelResponse(Message(
        "assistant", MessageRole.ASSISTANT, (TextBlock(text),),
    ))


def offline_failure() -> ModelAttemptFailure:
    return ModelAttemptFailure(
        ModelFailureCategory.NETWORK_UNAVAILABLE,
        ModelRetrySafety.SAFE_SAME_REQUEST,
        "connection_failed",
        "provider connection failed: [SSL: UNEXPECTED_EOF_WHILE_READING]",
    )


def offline_attempt() -> ModelAttemptFailed:
    return ModelAttemptFailed(offline_failure())


class ScriptedPolicy:
    """Stand-in policy: the provider's waiting logic is what is under test."""

    def __init__(self, *decisions: ModelRecoveryDecision) -> None:
        self._decisions = list(decisions)

    async def start(self, context) -> None:
        return None

    async def stop(self, deadline) -> None:
        return None

    async def evaluate(self, probe: ModelRecoveryProbe) -> ModelRecoveryDecision:
        return (
            self._decisions.pop(0) if len(self._decisions) > 1
            else self._decisions[0]
        )


class OfflineModel(EchoModelProvider):
    def __init__(self, *results: object) -> None:
        super().__init__()
        self._results = list(results)
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        result = (
            self._results[0] if len(self._results) == 1
            else self._results.pop(0)
        )
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


def scripted_probe(*answers: bool):
    remaining = list(answers)

    async def probe() -> bool:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return probe


def wait_decision(delay_seconds: float) -> ModelRecoveryDecision:
    return ModelRecoveryDecision(
        ModelRecoveryAction.WAIT_FOR_NETWORK,
        "network_unavailable_wait_for_recovery", delay_seconds,
    )


class NetworkRecoveryPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.policy = RuleBasedModelRecoveryPolicy(1.0)
        await self.policy.start(AdapterContext({}, lambda *_: None))

    def probe(self, limit: float, waited: float, attempt: int = 2):
        return ModelRecoveryProbe(
            offline_failure(), attempt, 3, False, False,
            network_wait_limit_seconds=limit,
            network_waited_seconds=waited,
        )

    async def test_network_failure_waits_instead_of_spending_the_last_attempt(self):
        # The reported behaviour: the third failure stopped everything. A dead
        # network must park the round instead of closing the attempt budget.
        decision = await self.policy.evaluate(self.probe(600.0, 0.0, attempt=3))
        self.assertIs(decision.action, ModelRecoveryAction.WAIT_FOR_NETWORK)
        self.assertEqual(
            decision.reason_code, "network_unavailable_wait_for_recovery"
        )
        self.assertGreaterEqual(decision.delay_seconds, 1.0)

    async def test_spent_wait_budget_interrupts_and_keeps_progress(self):
        for limit, waited in ((0.0, 0.0), (600.0, 600.0), (600.0, 900.0)):
            with self.subTest(limit=limit, waited=waited):
                decision = await self.policy.evaluate(self.probe(limit, waited))
                self.assertIs(
                    decision.action, ModelRecoveryAction.PRESERVE_AND_INTERRUPT
                )
                self.assertEqual(decision.reason_code, "network_wait_limit_reached")

    async def test_other_categories_keep_the_attempt_budget(self):
        decision = await self.policy.evaluate(ModelRecoveryProbe(
            ModelAttemptFailure(
                ModelFailureCategory.TRANSIENT_PROVIDER,
                ModelRetrySafety.SAFE_SAME_REQUEST, "http_503", "busy",
            ),
            3, 3, False, False, network_wait_limit_seconds=600.0,
        ))
        self.assertIs(decision.action, ModelRecoveryAction.PRESERVE_AND_INTERRUPT)
        self.assertEqual(decision.reason_code, "provider_attempt_limit_reached")


class ConnectionFailureCategoryTest(unittest.TestCase):
    def test_connection_failure_is_reported_as_network_unavailable(self):
        with mock.patch(
            "tsm_agt.adapters.http_json.urlopen",
            side_effect=URLError("offline"),
        ):
            with self.assertRaises(ModelProviderTransportError) as raised:
                UrllibHttpJsonTransport._post_json_sync(
                    "https://example.test/v1", {}, {"model": "fixture"}, 1.0,
                )
        failure = raised.exception
        self.assertIs(
            failure.category, ModelFailureCategory.NETWORK_UNAVAILABLE
        )
        self.assertEqual(failure.reason_code, "connection_failed")
        self.assertIs(failure.retry_safety, ModelRetrySafety.SAFE_SAME_REQUEST)


class ProviderNetworkWaitTest(unittest.IsolatedAsyncioTestCase):
    async def _coordinator(self, model, policy, **kwargs):
        provider = ResilientModelProvider(
            model, policy, max_provider_attempts=3, **kwargs
        )
        await provider.start(AdapterContext({}, lambda *_: None))
        return provider

    async def test_waiting_resumes_the_same_request_after_recovery(self):
        physical = OfflineModel(offline_attempt(), response("recovered"))
        model = await self._coordinator(
            physical,
            ScriptedPolicy(wait_decision(0.01)),
            network_wait_limit_seconds=30.0,
            network_probe=scripted_probe(False, True),
        )
        updates = []
        result = await model.complete(ModelRequest(
            "turn", (), on_transport_progress=updates.append,
        ))

        self.assertEqual(result.message.text, "recovered")
        self.assertEqual(physical.calls, 2)
        kinds = [update.kind for update in updates]
        self.assertEqual(kinds.count("network_waiting"), 2)
        self.assertIn("network_recovered", kinds)
        # The resumed request starts a fresh sequence: the attempt that never
        # left this machine must not consume the retry budget.
        started = [update.attempt for update in updates
                   if update.kind == "attempt_started"]
        self.assertEqual(started, [1, 1])
        recovered = next(u for u in updates if u.kind == "network_recovered")
        self.assertGreater(recovered.waited_seconds, 0.0)
        failed = next(u for u in updates if u.kind == "attempt_failed")
        self.assertEqual(
            failed.category, ModelFailureCategory.NETWORK_UNAVAILABLE.value
        )

    async def test_exhausted_wait_budget_interrupts_instead_of_spinning(self):
        physical = OfflineModel(offline_attempt())
        model = await self._coordinator(
            physical,
            ScriptedPolicy(wait_decision(0.01)),
            network_wait_limit_seconds=0.05,
            network_probe=scripted_probe(False),
        )
        with self.assertRaises(ModelRecoveryExhausted) as raised:
            await model.complete(ModelRequest("turn", ()))
        self.assertEqual(
            raised.exception.decision.reason_code, "network_wait_limit_reached"
        )
        self.assertIs(
            raised.exception.decision.action,
            ModelRecoveryAction.PRESERVE_AND_INTERRUPT,
        )
        self.assertEqual(physical.calls, 1)

    async def test_flapping_network_stops_after_the_resume_limit(self):
        physical = OfflineModel(offline_attempt())
        model = await self._coordinator(
            physical,
            ScriptedPolicy(wait_decision(0.01)),
            network_wait_limit_seconds=30.0,
            network_probe=scripted_probe(True),
            max_network_resumes=1,
        )
        with self.assertRaises(ModelRecoveryExhausted) as raised:
            await model.complete(ModelRequest("turn", ()))
        self.assertEqual(
            raised.exception.decision.reason_code, "network_resume_limit_reached"
        )
        self.assertEqual(physical.calls, 2)

    async def test_waiting_is_off_without_a_probe_or_a_budget(self):
        for label, kwargs in (
            ("no budget", {"network_wait_limit_seconds": 0.0,
                           "network_probe": scripted_probe(True)}),
            ("no probe", {"network_wait_limit_seconds": 30.0,
                          "network_probe": None}),
        ):
            with self.subTest(label):
                physical = OfflineModel(offline_attempt())
                model = await self._coordinator(
                    physical, ScriptedPolicy(wait_decision(0.01)), **kwargs
                )
                with self.assertRaises(ModelRecoveryExhausted) as raised:
                    await model.complete(ModelRequest("turn", ()))
                self.assertEqual(
                    raised.exception.decision.reason_code,
                    "network_wait_limit_reached",
                )
                self.assertEqual(physical.calls, 1)


class NetworkWaitConfigurationTest(unittest.TestCase):

    def _configuration(self, options: str) -> str:
        return (
            "version: 1\n"
            "model:\n"
            "  default_profile: primary\n"
            "  providers:\n"
            "    provider:\n"
            "      protocol: openai_chat\n"
            "      base_url: http://127.0.0.1:8000/v1\n"
            "      auth:\n"
            "        api_key: local-secret\n"
            "  profiles:\n"
            "    primary:\n"
            "      provider: provider\n"
            "      model: local-model\n"
            "      options:\n"
            + options
        )

    def _load(self, options: str):
        document = yaml.safe_load(self._configuration(options))
        return load_model_configuration(None, document)

    def test_default_budget_allows_ten_minutes_of_waiting(self) -> None:
        configuration = self._load("        streaming: false\n")
        self.assertEqual(configuration.network_wait_seconds, 600.0)

    def test_configured_budget_is_loaded(self) -> None:
        configuration = self._load(
            "        streaming: false\n        network_wait_seconds: 30\n"
        )
        self.assertEqual(configuration.network_wait_seconds, 30.0)

    def test_out_of_range_budget_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._load(
                "        streaming: false\n        network_wait_seconds: 7200\n"
            )


class DurableNetworkEventTest(unittest.TestCase):
    """The waiting events must survive into the per-Turn Web view."""

    def _item(self, event_type: str, waited: float) -> dict:
        payload = {
            "turn_id": "turn-1",
            "provider_attempt": 2,
            "max_provider_attempts": 3,
            "waited_seconds": waited,
            "delay_seconds": 4.0,
            "category": "NETWORK_UNAVAILABLE",
            "diagnostic_code": "connection_failed",
            "diagnostic_detail": "provider connection failed: offline",
        }
        event = SimpleNamespace(
            payload=payload, event_type=event_type, sequence=7,
            turn_id="turn-1",
        )
        item = _turn_progress_item(event)
        self.assertIsNotNone(item)
        return item

    def test_waiting_and_recovery_reach_the_turn_view(self):
        waiting = self._item("model.network_waiting", 12.5)
        self.assertEqual(waiting["kind"], "model_transport")
        self.assertEqual(waiting["transport_event"], "network_waiting")
        self.assertEqual(waiting["waited_seconds"], 12.5)
        self.assertEqual(waiting["transport_attempt"], 2)

        recovered = self._item("model.network_recovered", 20.0)
        self.assertEqual(recovered["transport_event"], "network_recovered")
        self.assertEqual(recovered["waited_seconds"], 20.0)

    def test_both_event_types_are_read_into_the_turn_view(self):
        self.assertIn("model.network_waiting", _TURN_PROGRESS_EVENT_TYPES)
        self.assertIn("model.network_recovered", _TURN_PROGRESS_EVENT_TYPES)
