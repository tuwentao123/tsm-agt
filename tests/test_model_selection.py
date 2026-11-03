from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pytest

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.model_profiles import ModelProfileProvider
from tsm_agt.adapters.openai_compatible.model import OpenAICompatibleModelProvider
from tsm_agt.adapters.openai_compatible.responses import OpenAIResponsesModelProvider
from tsm_agt.adapters.anthropic_messages import AnthropicMessagesModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.model_configuration import load_model_configuration, model_profile_catalog
from tsm_agt.core import AgentTurnCheckpoint, TaskState, canonical_hash
from tsm_agt.ports import (
    AdapterContext, Message, MessageRole, ModelRequest, ModelResponse,
    ModelStreamCompleted, ProviderCapabilities, TextBlock,
)
from tsm_agt.ports.model import ReasoningBlock


def document():
    return {"model": {"default_profile": "a", "providers": {
        "local": {"protocol": "openai_chat", "base_url": "http://127.0.0.1:9999/v1", "auth": {"api_key": "test-secret"}},
    }, "profiles": {
        "a": {"provider": "local", "model": "test-a", "reasoning_efforts": ["none", "high"]},
        "b": {"provider": "local", "model": "test-b"},
        "image": {"provider": "local", "model": "test-image", "kind": "image"},
        "unavailable": {"provider": "local", "model": "test-disabled", "enabled": False},
    }}}


class RecordingModel(EchoModelProvider):
    def __init__(self, name, window=128000):
        super().__init__()
        self.name = name
        self.requests = []
        self.capabilities = ProviderCapabilities(tools=True, context_window=window)

    async def complete(self, request):
        self.requests.append(request)
        await asyncio.sleep(0)
        return ModelResponse(Message("result-" + request.turn_id, MessageRole.ASSISTANT, (TextBlock(self.name),)))


def router():
    a, b = RecordingModel("a"), RecordingModel("b", 64000)
    return ModelProfileProvider({"a": a, "b": b}, "a", model_profile_catalog(document())), a, b


def test_catalog_never_exports_private_configuration():
    data = document()
    metadata = model_profile_catalog(data)
    text = json.dumps(metadata)
    assert "test-secret" not in text
    assert "127.0.0.1" not in text
    assert "auth" not in text
    assert not next(row for row in metadata if row["id"] == "image")["enabled"]
    for name in ("image", "unavailable", "missing"):
        with pytest.raises(ValueError):
            load_model_configuration(document=data, profile_id=name)
    assert load_model_configuration(document=data, profile_id="b").model == "test-b"


@pytest.mark.parametrize("parameters", [{"model": "override"}, {"messages": []}, {"Authorization": "secret"},
                                           {"thinking": {"type": "enabled", "unknown": True}}])
def test_request_parameters_cannot_override_identity_or_history(parameters):
    data = document()
    data["model"]["profiles"]["a"]["options"] = {"request_parameters": parameters}
    with pytest.raises(ValueError):
        load_model_configuration(document=data)


class SelectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_text_only_profile_can_answer_without_exposing_or_executing_tools(self):
        from tests.test_conversation_loop import _executing
        from tests.test_approval_context_continuity import ChangeProvider
        from tsm_agt.ports import ToolRisk
        a = RecordingModel("text answer")
        a.capabilities = replace(a.capabilities, tools=False, parallel_tools=False)
        model = ModelProfileProvider({"a": a}, "a", model_profile_catalog(document()))
        tools = ChangeProvider(ToolRisk.R0)
        app = compose_fixture_application(model_adapter=model, tool_adapters=(tools,))
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        with tempfile.TemporaryDirectory() as root:
            task = await _executing(app, Path(root))
            result = await app.kernel.run_agent_turn(task.task_id, "explain this topic")
        self.assertEqual(result.assistant_message.text, "text answer")
        self.assertEqual(a.requests[-1].tools, ())
        self.assertFalse(a.requests[-1].allow_tool_calls)
        self.assertIn("text-only conversation", "\n".join(message.text for message in a.requests[-1].messages))
        self.assertEqual(tools.calls, [])

    async def test_text_only_profile_rejects_unexpected_native_tool_calls(self):
        from tests.test_conversation_loop import ScriptedModel, _executing, tool_response
        from tests.test_approval_context_continuity import ChangeProvider
        from tsm_agt.core import ModelInvocationFailed
        from tsm_agt.ports import ToolRisk
        a = ScriptedModel([tool_response("tool", "change", "fixture.change", {"item": "A"})])
        a.capabilities = replace(a.capabilities, tools=False, parallel_tools=False)
        model = ModelProfileProvider({"a": a}, "a", model_profile_catalog(document()))
        tools = ChangeProvider(ToolRisk.R0)
        app = compose_fixture_application(model_adapter=model, tool_adapters=(tools,))
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        with tempfile.TemporaryDirectory() as root:
            task = await _executing(app, Path(root))
            with self.assertRaisesRegex(ModelInvocationFailed, "unexpected tool call"):
                await app.kernel.run_agent_turn(task.task_id, "change A")
        self.assertEqual(tools.calls, [])

    async def test_disallowed_reasoning_is_rejected_before_calling_provider(self):
        model, a, _ = router()
        with self.assertRaisesRegex(ValueError, "not supported by profile"):
            await model.complete(ModelRequest("bad", (), reasoning_effort="low", model_profile_id="a"))
        self.assertEqual(a.requests, [])

    async def test_switch_during_generation_keeps_current_turn_and_changes_next_turn(self):
        from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response
        from tests.test_approval_context_continuity import ChangeProvider
        from tests.history_helpers import session_history_view
        from tsm_agt.ports import CheckpointCompatibilityAction, ToolRisk

        class GatedModel(ScriptedModel):
            def __init__(self):
                super().__init__([tool_response("tool", "change", "fixture.change", {"item": "A"}),
                                  text_response("done", "original model answer")])
                self.entered, self.release = asyncio.Event(), asyncio.Event()

            async def complete(self, request):
                if not self.requests:
                    self.entered.set()
                    await self.release.wait()
                return await super().complete(request)

        a, b = GatedModel(), ScriptedModel([text_response("next", "new model answer")])
        model = ModelProfileProvider({"a": a, "b": b}, "a", model_profile_catalog(document()))
        app = compose_fixture_application(model_adapter=model, tool_adapters=(ChangeProvider(ToolRisk.R0),))
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        with tempfile.TemporaryDirectory() as directory:
            task = await _executing(app, Path(directory))
            running = asyncio.create_task(app.kernel.run_agent_turn(task.task_id, "change A"))
            try:
                await asyncio.wait_for(a.entered.wait(), 5)
                await app.kernel.update_session_model_profile(task.session_id, "b")
                current = await app.kernel.get_task(task.task_id)
                checkpoint = AgentTurnCheckpoint.from_data(current.active_agent_checkpoint)
                self.assertEqual(checkpoint.model_profile_id, "a")
                decision = await app.kernel._evaluate_checkpoint_compatibility(current, checkpoint, await app.kernel.list_tools())
                self.assertIs(decision.action, CheckpointCompatibilityAction.EXACT_RESUME)
            finally:
                a.release.set()
                outcome = await running
            self.assertEqual(outcome.assistant_message.text, "original model answer")
            self.assertEqual([request.model_profile_id for request in a.requests], ["a", "a"])
            self.assertEqual(b.requests, [])
            await app.kernel.transition_task(task.task_id, TaskState.EXECUTING, "next turn")
            result = await app.kernel.run_agent_turn(task.task_id, "continue with the prior answer")
            self.assertEqual(result.assistant_message.text, "new model answer")
            self.assertEqual(b.requests[-1].model_profile_id, "b")
            self.assertIn("original model answer", json.dumps(session_history_view(b.requests[-1])))
            configuration = (await app.kernel.get_task(task.task_id)).effective_configurations[-1]
            self.assertEqual(configuration.model["profile"], "b")

    async def test_pending_approval_keeps_original_model_after_switch_and_restart(self):
        from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response
        from tests.test_approval_context_continuity import ChangeProvider
        from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
        from tsm_agt.core import ApprovalDecision
        from tsm_agt.ports import ToolRisk

        def application(root, responses):
            a = ScriptedModel(responses)
            b = ScriptedModel([text_response("b", "B answer")])
            model = ModelProfileProvider({"a": a, "b": b}, "a", model_profile_catalog(document()))
            tools = ChangeProvider(ToolRisk.R1)
            app = compose_fixture_application(model_adapter=model, tool_adapters=(tools,),
                                              store_adapter=SQLiteRuntimeStore(root / "runtime.db"))
            return app, a, b, tools

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, _, _, _ = application(root, [tool_response("tool", "edit", "fixture.change", {"item": "A"})])
            await app.registry.start_all()
            try:
                task = await _executing(app, root)
                suspended = await app.kernel.run_agent_turn(task.task_id, "change A")
                await app.kernel.update_session_model_profile(task.session_id, "b")
            finally:
                await app.registry.stop_all()
            restored, a, b, tools = application(root, [text_response("done", "A finished")])
            await restored.registry.start_all()
            try:
                result = await restored.kernel.resolve_agent_approval(suspended.approval_request_id, ApprovalDecision.APPROVE, "approved")
                self.assertEqual(result.assistant_message.text, "A finished")
                self.assertEqual(a.requests[-1].model_profile_id, "a")
                self.assertEqual(b.requests, [])
                self.assertEqual(tools.calls, ["A"])
                self.assertEqual((await restored.kernel.get_session(task.session_id)).model_profile_id, "b")
            finally:
                await restored.registry.stop_all()

    async def test_reasoning_is_owned_by_profile_while_portable_history_survives_switch(self):
        from tsm_agt.ports import ToolCall, ToolCallBlock, ToolResult, ToolResultBlock
        class ReasoningModel(RecordingModel):
            async def complete(self, request):
                await super().complete(request)
                return ModelResponse(Message("signed", MessageRole.ASSISTANT, (
                    ReasoningBlock("anthropic_messages", "thinking", text="private", signature="signature"),
                    TextBlock("portable answer"),
                )))

        a, b = ReasoningModel("a"), RecordingModel("b")
        model = ModelProfileProvider({"a": a, "b": b}, "a", model_profile_catalog(document()))
        response = await model.complete(ModelRequest("first", (), model_profile_id="a"))
        message = Message.from_data(response.message.to_data())
        message = replace(message, content=message.content + (ToolCallBlock(ToolCall("call", "echo", {})),))
        result = Message("result", MessageRole.TOOL, (ToolResultBlock(ToolResult("call", True, data={"value": 7})),))
        self.assertEqual(message.content[0].model_profile_id, "a")
        await model.complete(ModelRequest("same", (message,), model_profile_id="a"))
        self.assertEqual(a.requests[-1].messages[0].content[0].signature, "signature")
        await model.complete(ModelRequest("other", (message, result), model_profile_id="b"))
        self.assertEqual(b.requests[-1].messages[0].text, "portable answer")
        self.assertFalse(any(isinstance(block, ReasoningBlock) for block in b.requests[-1].messages[0].content))
        self.assertIsInstance(b.requests[-1].messages[0].content[-1], ToolCallBlock)
        self.assertEqual(b.requests[-1].messages[1], result)

    async def test_web_model_selection_saves_next_turn_and_rejects_disabled_profiles(self):
        from fastapi import HTTPException
        from types import SimpleNamespace
        web = importlib.import_module("tsm_agt.web.app")
        model, _, _ = router()

        class Kernel:
            async def update_session_model_profile(self, session_id, profile_id):
                if session_id == "missing":
                    raise LookupError(session_id)
                return SimpleNamespace(session_id=session_id, model_profile_id=model.resolve_profile(profile_id), reasoning_effort=None)

        class Server:
            _client = SimpleNamespace(application=SimpleNamespace(kernel=Kernel()))

            def _call(self, coroutine):
                try:
                    coroutine.send(None)
                except StopIteration as done:
                    return done.value
                raise AssertionError("unexpected suspension")

        with patch.object(web, "runtime_server", Server()):
            result = await web.update_session_model_profile("existing", {"model_profile_id": "b"})
            self.assertEqual(result["model_profile_id"], "b")
            self.assertEqual(result["applies_to"], "next_turn")
            for session_id, profile, status in (("existing", "image", 409), ("existing", None, 400), ("missing", "a", 404)):
                with self.assertRaises(HTTPException) as failure:
                    await web.update_session_model_profile(session_id, {"model_profile_id": profile})
                self.assertEqual(failure.exception.status_code, status)

    async def test_concurrent_requests_do_not_share_selection(self):
        model, a, b = router()
        await model.start(AdapterContext({}, lambda *_: None))
        results = await asyncio.gather(*[
            model.complete(ModelRequest(str(i), (), model_profile_id=profile))
            for i, profile in enumerate(("b", "a", "b", "a"))
        ])
        self.assertEqual([r.message.text for r in results], ["b", "a", "b", "a"])
        self.assertEqual([r.diagnostics["model_profile"]["id"] for r in results], ["b", "a", "b", "a"])
        self.assertEqual(model.capabilities_for("b").context_window, 64000)
        with self.assertRaises(ValueError):
            await model.complete(ModelRequest("bad", (), model_profile_id="unavailable"))

    async def test_session_selection_reaches_turn_and_survives_snapshot(self):
        model, a, b = router()
        app = compose_fixture_application(model_adapter=model)
        await app.start()
        try:
            session = await app.kernel.create_session("model selection")
            await app.kernel.update_session_model_profile(session.session_id, "b")
            with tempfile.TemporaryDirectory() as root:
                task = await app.kernel.create_task("answer", Path(root), session_id=session.session_id)
                for state in (TaskState.INTAKE, TaskState.RESOLVING_PROJECT, TaskState.SELECTING_EXTENSIONS,
                              TaskState.ROUTING, TaskState.EXECUTING):
                    await app.kernel.transition_task(task.task_id, state, "selection test")
                result = await app.kernel.run_agent_turn(task.task_id, "answer")
            self.assertEqual(result.assistant_message.text, "b")
            self.assertEqual(b.requests[-1].model_profile_id, "b")
            snapshot = await app.kernel.get_session(session.session_id)
            self.assertEqual(type(snapshot).from_data(snapshot.to_data()).model_profile_id, "b")
            await app.kernel.update_session_model_profile(session.session_id, "a")
            # Older clients omit the field: retain the existing pin.
            retained = await app.kernel.update_session_model_profile(session.session_id, None)
            self.assertEqual(retained.model_profile_id, "a")
        finally:
            await app.registry.stop_all()

    async def test_catalog_web_endpoint_exports_only_public_fields(self):
        web = importlib.import_module("tsm_agt.web.app")
        model, _, _ = router()
        from types import SimpleNamespace
        server = SimpleNamespace(_client=SimpleNamespace(application=SimpleNamespace(
            kernel=SimpleNamespace(dependencies=SimpleNamespace(model=model)))))
        with patch.object(web, "runtime_server", server):
            response = await web.list_model_profiles()
        self.assertEqual(response["default_profile"], "a")
        self.assertNotIn("test-secret", json.dumps(response))


class WireControlsTest(unittest.IsolatedAsyncioTestCase):
    async def test_anthropic_signed_thinking_stream_and_replay(self):
        from tests.test_anthropic_messages_model import RecordingTransport
        events = [
            {"type": "message_start", "message": {"id": "thinking", "usage": {"input_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "opaque-private"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "signed"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "OK"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2, "cache_read_input_tokens": 1024}},
            {"type": "message_stop"},
        ]
        transport = RecordingTransport(events=[json.dumps(event) for event in events])
        provider = AnthropicMessagesModelProvider("http://127.0.0.1/v1", "test", "secret", transport=transport,
                                                  request_parameters={"cache_control": {"type": "ephemeral"}})
        await provider.start(AdapterContext({}, lambda *_: None))
        emitted = [event async for event in provider.stream_complete(ModelRequest("one", (), reasoning_effort="high"))]
        result = emitted[-1].response
        self.assertEqual(result.message.text, "OK")
        self.assertNotIn("opaque-private", "".join(getattr(e, "text", "") for e in emitted))
        replay, _ = provider._build_payload(ModelRequest("two", (result.message,), reasoning_effort="none"))
        self.assertEqual(replay["messages"][0]["content"][0], {"type": "thinking", "thinking": "opaque-private", "signature": "signed"})
        self.assertEqual(replay["thinking"], {"type": "disabled"})
        self.assertEqual(result.diagnostics["provider"]["usage"]["cache_read_input_tokens"], 1024)

    async def test_chat_reasoning_is_opaque_and_cache_parameters_are_configured(self):
        provider = OpenAICompatibleModelProvider("http://127.0.0.1/v1", "test", "secret",
            request_parameters={"thinking": {"type": "enabled"}, "prompt_cache_key": "synthetic-key"})
        response = provider._normalize_response("t", {"id": "r", "choices": [{"message": {
            "role": "assistant", "content": "OK", "reasoning_content": "opaque"}, "finish_reason": "stop"}]}, {})
        payload, _ = provider._build_payload(ModelRequest("two", (response.message,), reasoning_effort="none"))
        self.assertEqual(response.message.text, "OK")
        self.assertEqual(payload["messages"][0]["reasoning_content"], "opaque")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["prompt_cache_key"], "synthetic-key")


def test_responses_respects_per_turn_effort_and_retains_cache_accounting():
    provider = OpenAIResponsesModelProvider("http://127.0.0.1/v1", "test", "secret",
                                           request_parameters={"prompt_cache_key": "synthetic-key"})
    request = ModelRequest("turn", (), reasoning_effort="high")
    payload, _ = provider._responses_payload(request)
    assert payload["reasoning"]["effort"] == "high"
    assert payload["prompt_cache_key"] == "synthetic-key"
    response = provider._normalize_responses(request, {"id": "r", "status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
        "usage": {"input_tokens": 5, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 4}}}, {})
    assert response.diagnostics["provider"]["prompt_tokens_details"]["cached_tokens"] == 4


def test_previous_checkpoint_hash_still_loads_without_profile_field():
    checkpoint = AgentTurnCheckpoint("task", "turn", 1, (), (), (), 0, 0, 0, 0, 10, 10, 100, 1.0)
    data = checkpoint.to_data()
    data.pop("model_profile_id")
    content = {key: value for key, value in data.items() if key != "checkpoint_hash"}
    data["checkpoint_hash"] = canonical_hash(content)
    assert AgentTurnCheckpoint.from_data(data).model_profile_id is None


def test_legacy_checkpoint_recovers_its_original_configuration_profile():
    from types import SimpleNamespace
    from tsm_agt.core.kernel_checkpoint import CheckpointMixin
    checkpoint = AgentTurnCheckpoint("task", "turn", 1, (), (), (), 0, 0, 0, 0, 10, 10, 100, 1.0,
                                     effective_config_hash="original")
    task = SimpleNamespace(effective_configurations=(
        SimpleNamespace(effective_config_hash="original", model={"profile": "a"}),
        SimpleNamespace(effective_config_hash="new", model={"profile": "b"}),
    ))
    restored = CheckpointMixin._restore_checkpoint_model_profile(task, checkpoint)
    assert restored.model_profile_id == "a"
    assert checkpoint.model_profile_id is None


def test_probe_parameters_follow_profile_and_empty_thinking_is_not_evidence():
    from scripts.probe_model_capabilities import request_for, facts
    data = document()
    data["model"]["profiles"]["a"]["options"] = {"reasoning_effort": "none"}
    configuration = load_model_configuration(document=data)
    _, headers, payload = request_for(configuration, "openai_chat")
    assert payload["reasoning_effort"] == "none"
    assert "Authorization" in headers
    result = facts({"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "API_PROBE_OK"}],
                    "stop_reason": "end_turn", "usage": {"cache_read_input_tokens": 0}}, "anthropic_messages")
    assert result["answer_ok"]
    assert not result["reasoning_observed"]
    assert result["cached_tokens"] == 0


def test_omit_output_limit_is_explicit_and_preserved_by_composition():
    from tsm_agt.bootstrap.composition import _physical_model_provider
    data = document()
    data["model"]["providers"]["local"]["protocol"] = "openai_responses"
    data["model"]["profiles"]["a"]["options"] = {"send_output_token_limit": False}
    configuration = load_model_configuration(document=data)
    assert configuration.send_output_token_limit is False
    model = _physical_model_provider(
        base_url=configuration.base_url, model=configuration.model, api_key=configuration.api_key,
        protocol=configuration.protocol, anthropic_version=configuration.anthropic_version,
        timeout_seconds=60, output_token_parameter="max_completion_tokens", strict_tool_schema=False,
        streaming=True, profile_configurations={"a": configuration},
        profile_catalog=model_profile_catalog(data), default_profile_id="a",
    )
    payload, _ = model.default_provider._responses_payload(ModelRequest("test", (), max_output_tokens=2048))
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"}.intersection(payload)
    from scripts.probe_model_capabilities import request_for
    assert "max_output_tokens" not in request_for(configuration, "openai_responses")[2]
    data["model"]["providers"]["local"]["protocol"] = "anthropic_messages"
    with pytest.raises(ValueError, match="requires an output token limit"):
        load_model_configuration(document=data)


@pytest.mark.parametrize("failure", ["parameter", "timeout"])
def test_reasoning_probe_uses_declared_efforts_and_keeps_independent_results(failure):
    import io
    from urllib.error import HTTPError
    from scripts.probe_model_capabilities import probe
    data = document()
    data["model"]["profiles"]["a"]["reasoning_efforts"] = ["low", "high"]
    configuration = load_model_configuration(document=data)
    error = (HTTPError("http://127.0.0.1", 400, "bad parameter", {}, io.BytesIO(json.dumps({
        "error": {"message": "Unsupported value", "param": "reasoning_effort", "code": "unsupported_value"}
    }).encode())) if failure == "parameter" else TimeoutError())
    response = {"choices": [{"message": {"content": "32743"}, "finish_reason": "stop"}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 4}}}
    with patch("scripts.probe_model_capabilities.exchange", side_effect=[error, response]) as exchange:
        result = probe(configuration, "openai_chat", "reasoning")
    assert [call.args[2]["reasoning_effort"] for call in exchange.call_args_list] == ["low", "high"]
    assert result["status"] == "observed"
    assert result["samples"][1]["status"] == "accepted"
    assert result["samples"][0]["status"] in {"rejected", "error"}


@pytest.mark.parametrize("message,code,status,kind", [
    ("Unsupported value for reasoning_effort", "unsupported_value", 400, "parameter_incompatible"),
    ("Access to Anthropic models is not allowed for this account.", None, 400, "service_permission"),
    ("Model supports no callable protocol; configured/available protocols: [DECISIONS]", None, 400, "unsupported_protocol"),
    ("该模型已下线或已被删除", None, 403, "service_unavailable"),
])
def test_probe_errors_distinguish_request_compatibility_from_service_restrictions(message, code, status, kind):
    import io
    from urllib.error import HTTPError
    from scripts.probe_model_capabilities import error_facts
    configuration = load_model_configuration(document=document())
    nested = {"error": {"message": json.dumps({"error": {"message": message, "code": code}})}}
    error = HTTPError("http://127.0.0.1", status, "bad", {}, io.BytesIO(json.dumps(nested).encode()))
    result = error_facts(error, configuration)
    assert result["failure_kind"] == kind
    assert result["status"] == "rejected"
    assert "model_unavailable" not in result
