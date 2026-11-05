"""YAML-only model configuration for the local tsm-agt installation.

The application deliberately has no dotenv or process-environment model
configuration path. A model name alone does not identify its wire protocol,
so each named profile selects a provider and that provider declares its
protocol explicitly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml


MODEL_PROTOCOL_OPEN = "open"
MODEL_PROTOCOL_ANTHROPIC = "anthropic"
MODEL_PROTOCOL_ANTHROPIC_MESSAGES = MODEL_PROTOCOL_ANTHROPIC

WIRE_PROTOCOL_OPENAI_CHAT = "openai_chat"
WIRE_PROTOCOL_OPENAI_RESPONSES = "openai_responses"
WIRE_PROTOCOL_ANTHROPIC_MESSAGES = "anthropic_messages"
WIRE_PROTOCOLS = frozenset({
    WIRE_PROTOCOL_OPENAI_CHAT,
    WIRE_PROTOCOL_OPENAI_RESPONSES,
    WIRE_PROTOCOL_ANTHROPIC_MESSAGES,
})

CONFIGURATION_TEMPLATE = """# Private tsm-agt configuration. Keep this file outside every repository.
# The file can hold credentials and is created with mode 0600 on POSIX systems.
version: 1

model:
  default_profile: local-model
  providers:
    local-openai-compatible:
      # One of: openai_chat, openai_responses, anthropic_messages
      protocol: openai_chat
      base_url: http://127.0.0.1:5580/v1
      auth:
        api_key: your-api-key
  profiles:
    local-model:
      provider: local-openai-compatible
      model: your-model-name
      options:
        streaming: true
        timeout_seconds: 180
        max_retries: 2
        strict_tool_schema: false
      # Declare only known limitations. A profile can reduce, never add,
      # adapter capabilities.
      # capabilities:
      #   parallel_tools: false

# All following sections are optional. Omitted values use the built-in defaults.
# agent:
#   max_model_calls: 90
#   max_tool_calls: 120
# execution:
#   max_parallel_tool_calls: 8
# exploration:
#   profile: balanced
#   max_tool_calls: 60
#   max_actions: 60
#   max_tool_seconds: 300
# context:
#   compaction_ratio: 0.80
#   latency_soft_tokens: 0
#   recent_message_floor: 8
#   history_soft_tokens: 16384
# session:
#   recent_messages: 8
#   task_summaries: 4
#   history_format: native
# web:
#   egress_mode: DIRECT
#   search:
#     mode: live
#     tavily_mode: off
#     # tavily_api_key: your-tavily-key
# mcp:
#   enabled: true
#   servers:
#     example:
#       kind: executable
#       command: ["python", "-m", "example_mcp"]
# completion_review:
#   mode: off
#   max_continuations: 1
#   failure_policy: visible_unverified
"""

_PLACEHOLDER_API_KEYS = {"your-api-key", ""}


class ModelConfigurationError(ValueError):
    """The selected model profile is incomplete."""

    def __init__(self, missing: tuple[str, ...], configuration_file: Path) -> None:
        self.missing = missing
        self.configuration_file = configuration_file
        super().__init__(self._message())

    def _message(self) -> str:
        return (
            "Model configuration is incomplete.\n"
            f"Missing: {', '.join(self.missing)}\n"
            "Create a private YAML template with:\n"
            "  tsm-agt init\n"
            f"Then edit {self.configuration_file} and run:\n"
            "  tsm-agt doctor --workspace <project> --model-check"
        )


@dataclass(frozen=True, slots=True)
class ModelConfiguration:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    sources: Mapping[str, str]
    configuration_file: Path
    timeout_seconds: float = 180.0
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    output_token_parameter: str = "max_tokens"
    protocol: str = MODEL_PROTOCOL_OPEN
    anthropic_version: str = "2023-06-01"
    strict_tool_schema: bool = True
    streaming: bool = True
    open_api_mode: str = "chat"
    reasoning_effort: str | None = None
    profile_id: str | None = None
    capability_overrides: Mapping[str, bool | int] = field(default_factory=dict)
    request_parameters: Mapping[str, object] = field(default_factory=dict)
    send_output_token_limit: bool = True
    reasoning_efforts: tuple[str, ...] = ()
    # Optional wire ceiling, independent of the runtime's context reservation.
    max_output_tokens: int | None = None

    @property
    def effective_output_token_parameter(self) -> str:
        return "max_tokens" if self.protocol == MODEL_PROTOCOL_ANTHROPIC else self.output_token_parameter

    @property
    def endpoint_origin(self) -> str:
        return endpoint_origin(self.base_url)

    @property
    def wire_protocol(self) -> str:
        if self.protocol == MODEL_PROTOCOL_ANTHROPIC:
            return WIRE_PROTOCOL_ANTHROPIC_MESSAGES
        return WIRE_PROTOCOL_OPENAI_RESPONSES if self.open_api_mode == "responses" else WIRE_PROTOCOL_OPENAI_CHAT


def default_configuration_path() -> Path:
    """Return the private, user-level configuration location."""

    return Path.home() / ".tsm-agt" / "config.yaml"


def endpoint_origin(base_url: str) -> str:
    """Return only scheme/host/port so diagnostics never expose paths or keys."""

    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("model.providers.<id>.base_url must be an http(s) URL")
    hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError as error:
        raise ValueError("model provider base_url contains an invalid port") from error
    return f"{parsed.scheme}://{hostname}{port}"


def load_configuration_document(path: Path | None = None) -> tuple[Path, Mapping[str, object]]:
    """Read the one private YAML configuration file without any fallback layer."""

    configuration_file = (path or default_configuration_path()).expanduser().resolve()
    if not configuration_file.exists():
        raise ModelConfigurationError(("configuration file",), configuration_file)
    if not configuration_file.is_file():
        raise ValueError(f"configuration path is not a file: {configuration_file}")
    try:
        data = yaml.safe_load(configuration_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid YAML in {configuration_file}: {error}") from error
    if not isinstance(data, Mapping) or any(not isinstance(key, str) for key in data):
        raise ValueError(f"configuration YAML must be a mapping: {configuration_file}")
    if data.get("version", 1) != 1:
        raise ValueError("configuration YAML supports only version: 1")
    return configuration_file, data


def load_model_configuration(
    path: Path | None = None,
    document: Mapping[str, object] | None = None,
    *,
    require_complete: bool = True,
    profile_id: str | None = None,
) -> ModelConfiguration | None:
    """Load the selected YAML model profile.

    ``document`` exists so the application configuration is parsed once by the
    composition root. It is configuration data, never an environment overlay.
    """

    if document is None:
        configuration_file, document = load_configuration_document(path)
    else:
        configuration_file = (path or default_configuration_path()).expanduser().resolve()
    model_data = _mapping(document.get("model"), "model")
    providers = _mapping(model_data.get("providers"), "model.providers")
    profiles = _mapping(model_data.get("profiles"), "model.profiles")
    profile_id = profile_id or _string(model_data.get("default_profile"), "model.default_profile")
    profile = _mapping(profiles.get(profile_id), f"model.profiles.{profile_id}")
    if not _bool(profile.get("enabled", True), f"model.profiles.{profile_id}.enabled"):
        raise ValueError(f"model profile is disabled: {profile_id}")
    if profile.get("kind", "text") != "text":
        raise ValueError(f"model profile is not a text agent: {profile_id}")
    provider_id = _string(profile.get("provider"), f"model.profiles.{profile_id}.provider")
    provider = _mapping(providers.get(provider_id), f"model.providers.{provider_id}")
    wire_protocol = _choice(provider.get("protocol"), f"model.providers.{provider_id}.protocol", WIRE_PROTOCOLS)
    auth = _mapping(provider.get("auth"), f"model.providers.{provider_id}.auth")
    api_key = _string(auth.get("api_key"), f"model.providers.{provider_id}.auth.api_key")
    model = _string(profile.get("model"), f"model.profiles.{profile_id}.model")
    missing: list[str] = []
    if api_key in _PLACEHOLDER_API_KEYS:
        missing.append(f"model.providers.{provider_id}.auth.api_key")
    if model == "your-model-name":
        missing.append(f"model.profiles.{profile_id}.model")
    if missing:
        if require_complete:
            raise ModelConfigurationError(tuple(missing), configuration_file)
        return None
    base_url = _string(provider.get("base_url"), f"model.providers.{provider_id}.base_url")
    endpoint_origin(base_url)
    provider_options = _optional_mapping(provider.get("options"), f"model.providers.{provider_id}.options")
    profile_options = _optional_mapping(profile.get("options"), f"model.profiles.{profile_id}.options")
    options = {**provider_options, **profile_options}
    _validate_option_names(options, profile_id)
    capability_overrides = _capability_overrides(profile.get("capabilities"), profile_id)
    protocol = MODEL_PROTOCOL_ANTHROPIC if wire_protocol == WIRE_PROTOCOL_ANTHROPIC_MESSAGES else MODEL_PROTOCOL_OPEN
    send_output_token_limit = _bool(options.get("send_output_token_limit", True), "model options.send_output_token_limit")
    if protocol == MODEL_PROTOCOL_ANTHROPIC and not send_output_token_limit:
        raise ValueError("Anthropic Messages requires an output token limit")
    max_output_tokens = options.get("max_output_tokens")
    if max_output_tokens is not None:
        max_output_tokens = _int_range(max_output_tokens, "model options.max_output_tokens", 1, 1_000_000)
        if not send_output_token_limit:
            raise ValueError("max_output_tokens requires send_output_token_limit=true")
    sources = {name: "yaml" for name in (
        "model.provider", "model.endpoint_origin", "model.model",
        "model.credentials_configured", "model.protocol", "model.profile",
        "model.wire_protocol", "model.options",
    )}
    return ModelConfiguration(
        base_url=base_url,
        model=model,
        api_key=api_key,
        sources=sources,
        configuration_file=configuration_file,
        timeout_seconds=_positive_number(options.get("timeout_seconds", 180), "model options.timeout_seconds"),
        max_retries=_int_range(options.get("max_retries", 2), "model options.max_retries", 0, 10),
        retry_backoff_seconds=_number_range(options.get("retry_backoff_seconds", 1), "model options.retry_backoff_seconds", 0, 60),
        output_token_parameter=_choice(options.get("output_token_parameter", "max_tokens"), "model options.output_token_parameter", {"max_tokens", "max_completion_tokens"}),
        protocol=protocol,
        anthropic_version=_string_or_default(options.get("anthropic_version"), "model options.anthropic_version", "2023-06-01"),
        strict_tool_schema=_bool(options.get("strict_tool_schema", True), "model options.strict_tool_schema"),
        streaming=_bool(options.get("streaming", True), "model options.streaming"),
        open_api_mode="responses" if wire_protocol == WIRE_PROTOCOL_OPENAI_RESPONSES else "chat",
        reasoning_effort=_optional_choice(options.get("reasoning_effort"), "model options.reasoning_effort", {"none", "minimal", "low", "medium", "high", "xhigh", "max"}),
        profile_id=profile_id,
        capability_overrides=capability_overrides,
        request_parameters=_request_parameters(options.get("request_parameters"), wire_protocol),
        send_output_token_limit=send_output_token_limit,
        reasoning_efforts=_reasoning_efforts(profile, profile_id),
        max_output_tokens=max_output_tokens,
    )


def model_profile_catalog(document: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    """Public profile metadata only: never return endpoints, options or credentials."""
    model = _mapping(document.get("model"), "model")
    providers = _mapping(model.get("providers"), "model.providers")
    profiles = _mapping(model.get("profiles"), "model.profiles")
    rows = []
    for profile_id, raw in profiles.items():
        profile = _mapping(raw, f"model.profiles.{profile_id}")
        provider_id = _string(profile.get("provider"), "model profile provider")
        provider = _mapping(providers.get(provider_id), "model provider")
        kind = _choice(profile.get("kind", "text"), "model profile kind", {"text", "image"})
        enabled = _bool(profile.get("enabled", True), "model profile enabled")
        efforts = _reasoning_efforts(profile, profile_id)
        rows.append({
            "id": profile_id, "label": _string(profile.get("label", profile.get("model")), "model profile label"),
            "model": _string(profile.get("model"), "model profile model"),
            "protocol": _choice(provider.get("protocol"), "model provider protocol", WIRE_PROTOCOLS),
            "enabled": enabled and kind == "text", "kind": kind,
            "disabled_reason": str(profile.get("disabled_reason", "图像模型不能用于代码会话" if kind == "image" else "")),
            "reasoning_efforts": list(efforts),
            "status": _choice(profile.get("status", "unverified"), "model profile status",
                              {"ready", "limited", "unverified", "blocked", "unsupported"}),
            "status_detail": str(profile.get("status_detail", "")),
        })
    return tuple(rows)


def _reasoning_efforts(profile: Mapping[str, object], profile_id: str) -> tuple[str, ...]:
    efforts = profile.get("reasoning_efforts", [])
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
    if not isinstance(efforts, list) or any(not isinstance(value, str) or value not in allowed for value in efforts):
        raise ValueError(f"invalid reasoning_efforts for {profile_id}")
    return tuple(efforts)


def _request_parameters(value: object, protocol: str) -> Mapping[str, object]:
    import copy
    parameters = _optional_mapping(value, "model options.request_parameters")
    common = {"thinking", "output_config", "cache_control"}
    allowed = common if protocol == WIRE_PROTOCOL_ANTHROPIC_MESSAGES else {
        "thinking", "prompt_cache_key", "prompt_cache_retention", "parallel_tool_calls",
    }
    if protocol == WIRE_PROTOCOL_OPENAI_RESPONSES:
        allowed = allowed - {"thinking"}
    if set(parameters) - allowed:
        raise ValueError("unsupported request_parameters: " + ", ".join(sorted(set(parameters) - allowed)))
    for key in ("thinking", "output_config", "cache_control"):
        if key in parameters:
            _mapping(parameters[key], "request_parameters." + key)
    if "thinking" in parameters:
        thinking = parameters["thinking"]
        if set(thinking) - {"type", "budget_tokens"}:
            raise ValueError("unsupported thinking parameters")
        _choice(thinking.get("type"), "thinking.type", {"enabled", "disabled", "adaptive"})
        if "budget_tokens" in thinking:
            _int_range(thinking["budget_tokens"], "thinking.budget_tokens", 1024, 1_000_000)
    if "output_config" in parameters:
        output = parameters["output_config"]
        if set(output) != {"effort"}:
            raise ValueError("output_config only accepts effort")
        _choice(output["effort"], "output_config.effort", {"low", "medium", "high", "max"})
    if "cache_control" in parameters and parameters["cache_control"] not in (
        {"type": "ephemeral"}, {"type": "ephemeral", "ttl": "5m"}, {"type": "ephemeral", "ttl": "1h"},
    ):
        raise ValueError("invalid cache_control")
    for key in ("prompt_cache_key", "prompt_cache_retention"):
        if key in parameters:
            _string(parameters[key], "request_parameters." + key)
    if "parallel_tool_calls" in parameters:
        _bool(parameters["parallel_tool_calls"], "parallel_tool_calls")
    return copy.deepcopy(dict(parameters))


def inspect_model_configuration(path: Path | None = None) -> tuple[ModelConfiguration | None, tuple[str, ...]]:
    try:
        return load_model_configuration(path), ()
    except ModelConfigurationError as error:
        return None, error.missing


def model_configuration_sources(configuration: ModelConfiguration) -> dict[str, str]:
    return dict(configuration.sources)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a mapping")
    return value


def _optional_mapping(value: object, name: str) -> Mapping[str, object]:
    return {} if value is None else _mapping(value, name)


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _string_or_default(value: object, name: str, default: str) -> str:
    return default if value is None else _string(value, name)


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _number_range(value: object, name: str, minimum: float, maximum: float | None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if number < minimum or (maximum is not None and number > maximum):
        message = f"{name} must be >= {minimum}"
        if maximum is not None:
            message += f" and <= {maximum}"
        raise ValueError(message)
    return number


def _positive_number(value: object, name: str) -> float:
    number = _number_range(value, name, 0, None)
    if number <= 0:
        raise ValueError(f"{name} must be > 0")
    return number


def _int_range(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _choice(value: object, name: str, choices: frozenset[str] | set[str]) -> str:
    selected = _string(value, name)
    if selected not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(choices))}")
    return selected


def _optional_choice(value: object, name: str, choices: set[str]) -> str | None:
    return None if value is None else _choice(value, name, choices)


def _validate_option_names(options: Mapping[str, object], profile_id: str) -> None:
    supported = {
        "timeout_seconds", "max_retries", "retry_backoff_seconds",
        "output_token_parameter", "anthropic_version", "strict_tool_schema",
        "streaming", "reasoning_effort", "request_parameters", "send_output_token_limit",
        "max_output_tokens",
    }
    unknown = sorted(set(options) - supported)
    if unknown:
        raise ValueError(f"model.profiles.{profile_id}.options has unsupported keys: {', '.join(unknown)}")


def _capability_overrides(value: object, profile_id: str) -> Mapping[str, bool | int]:
    capabilities = _optional_mapping(value, f"model.profiles.{profile_id}.capabilities")
    boolean_names = {
        "tools", "parallel_tools", "strict_json_schema", "reasoning_blocks",
        "vision", "prompt_cache", "stream_cancel", "structured_conclusion",
    }
    allowed = boolean_names | {"context_window"}
    unknown = sorted(set(capabilities) - allowed)
    if unknown:
        raise ValueError(f"model.profiles.{profile_id}.capabilities has unsupported keys: {', '.join(unknown)}")
    normalized: dict[str, bool | int] = {}
    for name, item in capabilities.items():
        normalized[name] = _int_range(item, f"model.profiles.{profile_id}.capabilities.context_window", 1, 10_000_000) if name == "context_window" else _bool(item, f"model.profiles.{profile_id}.capabilities.{name}")
    return normalized
