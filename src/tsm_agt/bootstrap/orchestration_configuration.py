"""Configuration for choosing a Task's immutable orchestration mode."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tsm_agt.core.single_loop import OrchestrationMode


ORCHESTRATION_MODE_ENV_NAME = "TSM_AGT_ORCHESTRATION_MODE"
SINGLE_LOOP_ALLOW_FALLBACK_ENV_NAME = "TSM_AGT_SINGLE_LOOP_ALLOW_FALLBACK"
_ORCHESTRATION_ENV_NAMES = frozenset({
    ORCHESTRATION_MODE_ENV_NAME,
    SINGLE_LOOP_ALLOW_FALLBACK_ENV_NAME,
})


_MODE_BY_CONFIG_VALUE = {
    "legacy": OrchestrationMode.LEGACY,
    "single_loop": OrchestrationMode.SINGLE_LOOP,
}


@dataclass(frozen=True, slots=True)
class OrchestrationConfiguration:
    """Creation-time mode selection; Task events remain the runtime authority."""

    mode: OrchestrationMode | str = OrchestrationMode.SINGLE_LOOP
    single_loop_allow_fallback: bool = False
    sources: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, OrchestrationMode):
            try:
                normalized_mode = _MODE_BY_CONFIG_VALUE[str(self.mode).casefold()]
            except KeyError as error:
                raise ValueError(
                    "orchestration.mode must be 'legacy' or 'single_loop'"
                ) from error
            object.__setattr__(self, "mode", normalized_mode)
        if self.single_loop_allow_fallback:
            raise ValueError("single_loop_allow_fallback must be false")

    def snapshot_data(self) -> dict[str, str | bool]:
        return {
            "mode": self.mode.value.casefold(),
            "single_loop_allow_fallback": self.single_loop_allow_fallback,
        }


def load_orchestration_configuration(
    path: Path,
    environment: Mapping[str, str],
) -> OrchestrationConfiguration:
    """Load an optional mode selection without mutating process environment."""

    file_values = _read_optional_values(path.expanduser().resolve())
    exported_mode = environment.get(ORCHESTRATION_MODE_ENV_NAME, "").strip()
    file_mode = file_values.get(ORCHESTRATION_MODE_ENV_NAME, "").strip()
    raw_mode = (exported_mode or file_mode or "single_loop").casefold()
    try:
        mode = _MODE_BY_CONFIG_VALUE[raw_mode]
    except KeyError as error:
        raise ValueError(
            "TSM_AGT_ORCHESTRATION_MODE must be 'legacy' or 'single_loop'"
        ) from error

    exported_fallback = environment.get(
        SINGLE_LOOP_ALLOW_FALLBACK_ENV_NAME, ""
    ).strip()
    file_fallback = file_values.get(
        SINGLE_LOOP_ALLOW_FALLBACK_ENV_NAME, ""
    ).strip()
    raw_fallback = (exported_fallback or file_fallback or "false").casefold()
    if raw_fallback not in {"true", "false"}:
        raise ValueError(
            "TSM_AGT_SINGLE_LOOP_ALLOW_FALLBACK must be 'true' or 'false'"
        )
    return OrchestrationConfiguration(
        mode=mode,
        single_loop_allow_fallback=raw_fallback == "true",
        sources={
            "orchestration.mode": (
                "environment" if exported_mode else
                "env_file" if file_mode else "default"
            ),
            "orchestration.single_loop_allow_fallback": (
                "environment" if exported_fallback else
                "env_file" if file_fallback else "default"
            ),
        },
    )


def _read_optional_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError(f"orchestration env path is not a file: {path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if name not in _ORCHESTRATION_ENV_NAMES:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values
