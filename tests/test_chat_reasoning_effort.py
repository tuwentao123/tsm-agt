from pathlib import Path

import pytest

from tsm_agt.bootstrap.composition import _physical_model_provider
from tsm_agt.bootstrap.model_configuration import load_model_configuration
from tsm_agt.adapters.openai_compatible.model import OpenAICompatibleModelProvider
from tsm_agt.adapters.openai_compatible.reasoning_config import (
    DEFAULT_REASONING_EFFORT_REGISTRY, ReasoningEffortProfile, ReasoningEffortRegistry,
)
from tsm_agt.ports import ModelRequest


def _configuration(effort=None):
    document = {"model": {
        "default_profile": "primary",
        "providers": {"local": {"protocol": "openai_chat", "base_url": "https://example.test/v1", "auth": {"api_key": "secret"}}},
        "profiles": {"primary": {"provider": "local", "model": "test-model", "options": {}}},
    }}
    if effort is not None:
        document["model"]["profiles"]["primary"]["options"]["reasoning_effort"] = effort
    return load_model_configuration(Path("/nonexistent/config.yaml"), document)


def _provider(configuration, mode="chat"):
    return _physical_model_provider(
        base_url=configuration.base_url, model=configuration.model,
        api_key=configuration.api_key, protocol=configuration.protocol,
        anthropic_version=configuration.anthropic_version, timeout_seconds=60,
        output_token_parameter="max_completion_tokens", strict_tool_schema=True,
        streaming=True, open_api_mode=mode,
        reasoning_effort=configuration.reasoning_effort,
    )


@pytest.mark.parametrize("effort", [None, "low", "high", "max"])
def test_configured_effort_reaches_chat_payload_only_when_set(effort):
    configuration = _configuration(effort)
    payload, _ = _provider(configuration)._build_payload(ModelRequest("turn", ()))
    if effort is None:
        assert "reasoning_effort" not in payload
    else:
        assert payload["reasoning_effort"] == effort


def test_responses_default_remains_medium():
    payload, _ = _provider(_configuration(), "responses")._responses_payload(
        ModelRequest("turn", ())
    )
    assert payload["reasoning"]["effort"] == "medium"
    assert "reasoning_effort" not in payload


def test_invalid_effort_is_rejected():
    with pytest.raises(ValueError, match="reasoning_effort"):
        _configuration("HIGH")


@pytest.mark.parametrize("model", ["OpenAI-GPT-5.1", "gpt-5.1", "gpt-5.1-2025-11-13"])
def test_gpt_51_aliases_use_configured_capabilities(model):
    registry = DEFAULT_REASONING_EFFORT_REGISTRY
    assert registry.resolve(model, "HIGH") == "high"
    assert registry.profile_for(model).allowed_efforts == ("none", "low", "medium", "high")
    with pytest.raises(ValueError, match="Unsupported reasoning effort"):
        registry.resolve(model, "max")


def test_provider_uses_replaceable_registry():
    registry = ReasoningEffortRegistry({"custom": ReasoningEffortProfile(("low", "max"))})
    provider = OpenAICompatibleModelProvider(
        "https://example.test", "custom", "secret", reasoning_effort="max",
        reasoning_registry=registry,
    )
    payload, _ = provider._build_payload(ModelRequest("turn", ()))
    assert payload["reasoning_effort"] == "max"
