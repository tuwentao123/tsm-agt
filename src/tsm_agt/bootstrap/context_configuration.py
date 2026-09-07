"""Project-local configuration for context-window compaction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


CONTEXT_ENV_NAMES = (
    "TSM_AGT_CONTEXT_COMPACTION_RATIO",
    "TSM_AGT_CONTEXT_LATENCY_SOFT_TOKENS",
)


@dataclass(frozen=True, slots=True)
class ContextConfiguration:
    """When to compact model-visible history; unrelated to tool budgets."""

    compaction_ratio: float = 0.80
    latency_soft_tokens: int = 0
    sources: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not 0 < self.compaction_ratio < 1:
            raise ValueError(
                "TSM_AGT_CONTEXT_COMPACTION_RATIO must be between 0 and 1"
            )
        if self.latency_soft_tokens < 0:
            raise ValueError(
                "TSM_AGT_CONTEXT_LATENCY_SOFT_TOKENS must be zero or positive"
            )


def load_context_configuration(
    path: Path, environment: Mapping[str, str],
) -> ContextConfiguration:
    values = _read_values(path.expanduser().resolve())
    ratio_raw, ratio_source = _selected(
        "TSM_AGT_CONTEXT_COMPACTION_RATIO", values, environment, "0.80"
    )
    soft_raw, soft_source = _selected(
        "TSM_AGT_CONTEXT_LATENCY_SOFT_TOKENS", values, environment, "0"
    )
    try:
        ratio = float(ratio_raw)
        soft_tokens = int(soft_raw)
    except ValueError as error:
        raise ValueError("context compaction settings must be numeric") from error
    return ContextConfiguration(
        ratio, soft_tokens, {
            "context.compaction_ratio": ratio_source,
            "context.latency_soft_tokens": soft_source,
        },
    )


def _selected(name, file_values, environment, default):
    exported = environment.get(name, "").strip()
    from_file = file_values.get(name, "").strip()
    if exported:
        return exported, "environment"
    if from_file:
        return from_file, "env_file"
    return default, "default"


def _read_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        name, value = (part.strip() for part in line.split("=", 1))
        if name not in CONTEXT_ENV_NAMES:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values
