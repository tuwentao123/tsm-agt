"""Safe loading and redacted inspection of local model configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


MODEL_ENV_NAMES = (
    "TSM_AGT_MODEL_BASE_URL",
    "TSM_AGT_MODEL",
    "TSM_AGT_MODEL_API_KEY",
)

MODEL_OPTIONAL_ENV_NAMES = (
    "TSM_AGT_MODEL_TIMEOUT_SECONDS",
    "TSM_AGT_MODEL_MAX_RETRIES",
    "TSM_AGT_MODEL_RETRY_BACKOFF_SECONDS",
    "TSM_AGT_MODEL_OUTPUT_TOKEN_PARAMETER",
    "TSM_AGT_MODEL_STRICT_TOOL_SCHEMA",
)
MODEL_SUPPORTED_ENV_NAMES = MODEL_ENV_NAMES + MODEL_OPTIONAL_ENV_NAMES

MODEL_ENV_TEMPLATE = """# tsm-agt model configuration. Keep this file private.
# Exported TSM_AGT_MODEL_* variables override values in this file.
TSM_AGT_MODEL_BASE_URL=http://127.0.0.1:5580
TSM_AGT_MODEL=your-model-name
TSM_AGT_MODEL_API_KEY=your-api-key
# TSM_AGT_MODEL_TIMEOUT_SECONDS=180
# TSM_AGT_MODEL_MAX_RETRIES=2
# TSM_AGT_MODEL_RETRY_BACKOFF_SECONDS=1
# TSM_AGT_MODEL_OUTPUT_TOKEN_PARAMETER=max_tokens
# TSM_AGT_MODEL_STRICT_TOOL_SCHEMA=true

# Optional Agent-loop hard limits and exploration soft limits.
# TSM_AGT_AGENT_MAX_MODEL_CALLS=15
# TSM_AGT_AGENT_MAX_TOOL_CALLS=40
# TSM_AGT_EXPLORATION_MAX_TOOL_CALLS=24
# TSM_AGT_EXPLORATION_MAX_ACTIONS=24
# TSM_AGT_EXPLORATION_MAX_TOOL_SECONDS=120
# TSM_AGT_EXPLORATION_LOW_VALUE_STREAK=2
# TSM_AGT_EXPLORATION_RESERVE_TOOL_CALLS=2
# TSM_AGT_EXPLORATION_MIN_ACTIONS=2
"""

_PLACEHOLDER_VALUES = {
    "TSM_AGT_MODEL": {"your-model-name"},
    "TSM_AGT_MODEL_API_KEY": {"your-api-key"},
}


class ModelConfigurationError(ValueError):
    """The selected model cannot be composed from the available settings."""

    def __init__(self, missing: tuple[str, ...], env_file: Path) -> None:
        self.missing = missing
        self.env_file = env_file
        super().__init__(self._message())

    def _message(self) -> str:
        workspace = self.env_file.parent
        return (
            "Model configuration is incomplete.\n"
            f"Missing: {', '.join(self.missing)}\n"
            "Create a private template with:\n"
            f"  tsm-agt init --workspace {workspace}\n"
            f"Then edit {self.env_file} or export the missing variables, and run:\n"
            f"  tsm-agt doctor --workspace {workspace} --model-check"
        )


@dataclass(frozen=True, slots=True)
class ModelConfiguration:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    sources: Mapping[str, str]
    env_file: Path
    timeout_seconds: float = 180.0
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    output_token_parameter: str = "max_tokens"
    strict_tool_schema: bool = True

    @property
    def endpoint_origin(self) -> str:
        return endpoint_origin(self.base_url)


def endpoint_origin(base_url: str) -> str:
    """Return only scheme/host/port so paths, queries, and credentials stay hidden."""

    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("TSM_AGT_MODEL_BASE_URL must be an http(s) URL")
    hostname = parsed.hostname
    if ":" in hostname:
        hostname = f"[{hostname}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError as error:
        raise ValueError(
            "TSM_AGT_MODEL_BASE_URL contains an invalid port"
        ) from error
    return f"{parsed.scheme}://{hostname}{port}"


def load_model_configuration(
    path: Path, environment: Mapping[str, str], *, require_complete: bool = True,
) -> ModelConfiguration | None:
    """Load the three supported settings without mutating the process environment."""

    env_file = path.expanduser().resolve()
    file_values = _read_model_env_file(env_file)
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for name in MODEL_SUPPORTED_ENV_NAMES:
        exported = environment.get(name, "").strip()
        from_file = file_values.get(name, "").strip()
        if exported:
            values[name] = exported
            sources[name] = "environment"
        elif from_file:
            values[name] = from_file
            sources[name] = "env_file"
        else:
            values[name] = ""

    missing = tuple(
        name for name in MODEL_ENV_NAMES
        if not values[name] or values[name] in _PLACEHOLDER_VALUES.get(name, set())
    )
    if missing:
        if require_complete:
            raise ModelConfigurationError(missing, env_file)
        return None
    endpoint_origin(values["TSM_AGT_MODEL_BASE_URL"])
    try:
        timeout_seconds = float(
            values["TSM_AGT_MODEL_TIMEOUT_SECONDS"] or "180"
        )
        max_retries = int(values["TSM_AGT_MODEL_MAX_RETRIES"] or "2")
        retry_backoff_seconds = float(
            values["TSM_AGT_MODEL_RETRY_BACKOFF_SECONDS"] or "1"
        )
    except ValueError as error:
        raise ValueError("model retry settings must be numeric") from error
    if timeout_seconds <= 0:
        raise ValueError("TSM_AGT_MODEL_TIMEOUT_SECONDS must be positive")
    if max_retries < 0 or max_retries > 10:
        raise ValueError("TSM_AGT_MODEL_MAX_RETRIES must be between 0 and 10")
    if retry_backoff_seconds < 0 or retry_backoff_seconds > 60:
        raise ValueError(
            "TSM_AGT_MODEL_RETRY_BACKOFF_SECONDS must be between 0 and 60"
        )
    output_token_parameter = (
        values["TSM_AGT_MODEL_OUTPUT_TOKEN_PARAMETER"] or "max_tokens"
    )
    if output_token_parameter not in {
        "max_tokens", "max_completion_tokens"
    }:
        raise ValueError(
            "TSM_AGT_MODEL_OUTPUT_TOKEN_PARAMETER must be 'max_tokens' or "
            "'max_completion_tokens'"
        )
    raw_strict_tool_schema = (
        values["TSM_AGT_MODEL_STRICT_TOOL_SCHEMA"] or "true"
    ).casefold()
    if raw_strict_tool_schema not in {"true", "false"}:
        raise ValueError(
            "TSM_AGT_MODEL_STRICT_TOOL_SCHEMA must be 'true' or 'false'"
        )
    return ModelConfiguration(
        base_url=values["TSM_AGT_MODEL_BASE_URL"],
        model=values["TSM_AGT_MODEL"],
        api_key=values["TSM_AGT_MODEL_API_KEY"],
        sources=sources,
        env_file=env_file,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        output_token_parameter=output_token_parameter,
        strict_tool_schema=(raw_strict_tool_schema == "true"),
    )


def inspect_model_configuration(
    path: Path, environment: Mapping[str, str],
) -> tuple[ModelConfiguration | None, tuple[str, ...]]:
    """Return complete configuration or only the missing variable names."""

    try:
        return load_model_configuration(path, environment), ()
    except ModelConfigurationError as error:
        return None, error.missing


def model_configuration_sources(
    configuration: ModelConfiguration,
) -> dict[str, str]:
    return {
        "model.provider": "composition",
        "model.endpoint_origin": configuration.sources[
            "TSM_AGT_MODEL_BASE_URL"
        ],
        "model.model": configuration.sources["TSM_AGT_MODEL"],
        "model.credentials_configured": configuration.sources[
            "TSM_AGT_MODEL_API_KEY"
        ],
        "model.output_token_parameter": configuration.sources.get(
            "TSM_AGT_MODEL_OUTPUT_TOKEN_PARAMETER", "default"
        ),
        "model.strict_tool_schema": configuration.sources.get(
            "TSM_AGT_MODEL_STRICT_TOOL_SCHEMA", "default"
        ),
    }


def _read_model_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError(f"model env path is not a file: {path}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError(f"invalid model env line {line_number} in {path}")
        name, value = (part.strip() for part in line.split("=", 1))
        if name not in MODEL_SUPPORTED_ENV_NAMES:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values
