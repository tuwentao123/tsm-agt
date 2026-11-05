"""Exercise wire ceilings, local reservations, and failed stream evidence."""
import json
from pathlib import Path
import tempfile
import unittest

import pytest

from tests.test_conversation_loop import ScriptedToolProvider, _executing
from tests.test_model_selection import document
from tests.test_openai_compatible_model import RecordingTransport, StreamingTransport
from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider, OpenAICompatibleProviderError
from tsm_agt.adapters.openai_compatible.responses import OpenAIResponsesModelProvider
from tsm_agt.adapters.resilient_model import ResilientModelProvider
from tsm_agt.adapters.rule_based_model_recovery import RuleBasedModelRecoveryPolicy
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.composition import _physical_model_provider
from tsm_agt.bootstrap.model_configuration import load_model_configuration, model_profile_catalog
from tsm_agt.core import ModelInvocationFailed
from tsm_agt.ports import AdapterContext, ModelRequest, ModelRecoveryExhausted, RuntimeStorePort


def configured_model(protocol, limit):
    data = document()
    data["model"]["providers"]["local"]["protocol"] = protocol
    data["model"]["profiles"]["a"]["options"] = {
        "output_token_parameter": "max_completion_tokens", "streaming": False,
    }
    if limit is not None:
        data["model"]["profiles"]["a"]["options"]["max_output_tokens"] = limit
    config = load_model_configuration(document=data)
    return _physical_model_provider(
        base_url=config.base_url, model=config.model, api_key=config.api_key,
        protocol=config.protocol, anthropic_version=config.anthropic_version,
        timeout_seconds=60, output_token_parameter="max_completion_tokens",
        strict_tool_schema=False, streaming=False,
        profile_configurations={"a": config},
        profile_catalog=model_profile_catalog(data), default_profile_id="a",
    )


@pytest.mark.parametrize("protocol,key", [("openai_chat", "max_completion_tokens"),
                                         ("openai_responses", "max_output_tokens"),
                                         ("anthropic_messages", "max_tokens")])
@pytest.mark.parametrize("limit", [None, 16000])
def test_configured_ceiling_reaches_selected_wire_adapter(protocol, key, limit):
    router = configured_model(protocol, limit)
    provider = router.default_provider
    build = provider._responses_payload if protocol == "openai_responses" else provider._build_payload
    payload, _ = build(ModelRequest("turn", ()))
    effective = limit if limit is not None else (8192 if protocol == "anthropic_messages" else None)
    assert router.output_token_limit_for("a") == effective
    assert payload.get(key) == effective
    if effective is None:
        assert not {"max_tokens", "max_output_tokens", "max_completion_tokens"}.intersection(payload)
    # Auxiliary calls keep their explicit per-request ceiling.
    override, _ = build(ModelRequest("turn", (), max_output_tokens=123))
    assert override[key] == 123


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "8192"])
def test_invalid_configured_ceiling_is_rejected(limit):
    with pytest.raises(ValueError):
        configured_model("openai_chat", limit)


def test_disabled_limit_cannot_silently_ignore_explicit_configuration():
    data = document()
    data["model"]["profiles"]["a"]["options"] = {
        "send_output_token_limit": False, "max_output_tokens": 16000,
    }
    with pytest.raises(ValueError, match="requires send_output_token_limit"):
        load_model_configuration(document=data)


class OutputLimitsTest(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_preserves_reservation_without_imposing_wire_cap(self):
        for limit, reserve in ((None, 8192), (16000, 16000)):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as root:
                model = configured_model("openai_chat", limit)
                transport = RecordingTransport([{
                    "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
                }])
                model.default_provider._transport = transport
                policy = RuleBasedModelRecoveryPolicy(0)
                await policy.start(AdapterContext({}, lambda *_: None))
                wrapped = ResilientModelProvider(model, policy)
                app = compose_fixture_application(model_adapter=wrapped, tool_adapters=())
                await app.registry.start_all()
                try:
                    task = await _executing(app, Path(root))
                    await app.kernel.run_agent_turn(task.task_id, "hello", max_output_tokens=8192)
                    events = await app.registry.require(RuntimeStorePort).read_events(task.task_id)
                    completed = next(e for e in events if e.event_type == "llm.completed")
                    self.assertEqual(completed.payload["context_budget"]["reserved_output_tokens"], reserve)
                    payload = transport.requests[0]["payload"]
                    self.assertEqual(payload.get("max_completion_tokens"), limit)
                    if limit is None:
                        self.assertNotIn("max_completion_tokens", payload)
                finally:
                    await app.registry.stop_all()

    async def test_large_configured_cap_cannot_bypass_context_guard(self):
        with tempfile.TemporaryDirectory() as root:
            model = configured_model("openai_chat", 160000)
            transport = RecordingTransport([])
            model.default_provider._transport = transport
            app = compose_fixture_application(model_adapter=model, tool_adapters=())
            await app.registry.start_all()
            try:
                task = await _executing(app, Path(root))
                with self.assertRaises(ModelInvocationFailed):
                    await app.kernel.run_agent_turn(task.task_id, "hello", max_output_tokens=8192)
                self.assertEqual(transport.requests, [])
            finally:
                await app.registry.stop_all()

    async def test_failed_stream_persists_usage_and_lengths_without_executing_partial_calls(self):
        tools = ScriptedToolProvider()
        partial = '{"query":"private-argument-body'
        transport = StreamingTransport([
            json.dumps({"choices": [{"delta": {"content": "Searching now. "}}]}),
            json.dumps({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "complete", "function": {"name": "web__search", "arguments": '{"query":"valid"}'}},
                {"index": 1, "id": "partial", "function": {"name": "web__search", "arguments": partial}},
            ]}, "finish_reason": "length"}]}),
            json.dumps({"choices": [], "usage": {
                "prompt_tokens": 50000, "completion_tokens": 8192, "total_tokens": 58192,
                "completion_tokens_details": {"reasoning_tokens": 4000, "secret": "private-usage"},
                "secret": "private-usage",
            }}),
            "[DONE]",
        ])
        physical = OpenAICompatibleModelProvider("http://example.test/v1", "test", "secret", transport=transport)
        policy = RuleBasedModelRecoveryPolicy(0)
        await policy.start(AdapterContext({}, lambda *_: None))
        model = ResilientModelProvider(physical, policy)
        app = compose_fixture_application(model_adapter=model, tool_adapters=(tools,))
        await app.registry.start_all()
        try:
            with tempfile.TemporaryDirectory() as root:
                task = await _executing(app, Path(root))
                with self.assertRaises(ModelRecoveryExhausted):
                    await app.kernel.run_agent_turn(task.task_id, "搜索网页 example", max_output_tokens=8192)
                events = await app.registry.require(RuntimeStorePort).read_events(task.task_id)
                failed = next(e for e in events if e.event_type == "model.attempt_failed")
                self.assertEqual(failed.payload["diagnostic_code"], "truncated_tool_arguments")
                self.assertEqual(failed.payload["retry_safety"], "UNSAFE_AFTER_VISIBLE_OUTPUT")
                facts = failed.payload["diagnostics"]
                self.assertIsNone(facts["requested_output_limit"])
                self.assertIsNone(facts["output_limit_parameter"])
                self.assertEqual(facts["usage"]["completion_tokens"], 8192)
                self.assertEqual(facts["usage"]["completion_tokens_details"], {"reasoning_tokens": 4000})
                self.assertEqual(facts["tool_calls"][1], {
                    "name": "web.search", "argument_chars": len(partial), "json_valid": False,
                })
                self.assertEqual(facts["tool_call_count"], 2)
                self.assertTrue(facts["tool_calls"][0]["json_valid"])
                encoded = json.dumps(facts)
                self.assertNotIn("private-", encoded)
                self.assertEqual(tools.calls, [])
                self.assertEqual(len(transport.requests), 1)
        finally:
            await app.registry.stop_all()

    async def test_nonstream_truncation_retains_actual_requested_ceiling(self):
        transport = RecordingTransport([{
            "choices": [{"message": {"tool_calls": [{"id": "c", "function": {"name": "unknown", "arguments": "{"}}]},
                         "finish_reason": "length"}],
            "usage": {"completion_tokens": 64, "completion_tokens_details": {"reasoning_tokens": 10}},
        }])
        model = OpenAICompatibleModelProvider("http://example.test/v1", "test", "secret", transport=transport,
                                             max_output_tokens=64, output_token_parameter="max_completion_tokens")
        await model.start(AdapterContext({}, lambda *_: None))
        with self.assertRaises(OpenAICompatibleProviderError) as caught:
            await model.complete(ModelRequest("turn", ()))
        facts = caught.exception.failure.diagnostics
        self.assertEqual(facts["requested_output_limit"], 64)
        self.assertEqual(facts["output_limit_parameter"], "max_completion_tokens")
        self.assertEqual(facts["usage"]["completion_tokens"], 64)


def test_responses_incomplete_tool_arguments_preserve_truncation_diagnosis():
    provider = OpenAIResponsesModelProvider("http://example.test/v1", "test", "secret", max_output_tokens=64)
    with pytest.raises(OpenAICompatibleProviderError) as caught:
        provider._normalize_responses(ModelRequest("turn", ()), {
            "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "function_call", "call_id": "c", "name": "echo", "arguments": "{"}],
            "usage": {"input_tokens": 100, "output_tokens": 64, "output_tokens_details": {"reasoning_tokens": 40}},
        }, {"echo": "fixture.echo"})
    assert caught.value.reason_code == "truncated_tool_arguments"
    facts = caught.value.failure.diagnostics
    assert facts["requested_output_limit"] == 64
    assert facts["output_limit_parameter"] == "max_output_tokens"
    assert facts["usage"]["completion_tokens_details"] == {"reasoning_tokens": 40}


def test_reasoning_only_length_response_keeps_usage_in_failure():
    with pytest.raises(OpenAICompatibleProviderError) as caught:
        OpenAICompatibleModelProvider._normalize_response("turn", {
            "choices": [{"message": {"content": None}, "finish_reason": "length"}],
            "usage": {"completion_tokens": 32, "completion_tokens_details": {"reasoning_tokens": 32}},
        }, {})
    assert caught.value.failure.diagnostics["usage"]["completion_tokens"] == 32
