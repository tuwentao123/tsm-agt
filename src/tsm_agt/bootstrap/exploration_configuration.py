"""Configurable, project-local limits for evidence-gathering exploration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


EXPLORATION_ENV_NAMES = (
    "TSM_AGT_EXPLORATION_PROFILE",
    "TSM_AGT_AGENT_MAX_MODEL_CALLS",
    "TSM_AGT_AGENT_MAX_TOOL_CALLS",
    "TSM_AGT_AGENT_FINALIZATION_MODEL_CALLS",
    "TSM_AGT_AGENT_EXECUTION_RESERVE_MODEL_CALLS",
    "TSM_AGT_AGENT_RECOVERY_RESERVE_MODEL_CALLS",
    "TSM_AGT_AGENT_VERIFICATION_RESERVE_MODEL_CALLS",
    "TSM_AGT_AGENT_MODEL_CALL_RENEWAL_INCREMENTS",
    "TSM_AGT_AGENT_MODEL_CALL_RENEWAL_MAX_COUNT",
    "TSM_AGT_AGENT_MODEL_CALL_RENEWAL_ABSOLUTE_LIMIT",
    "TSM_AGT_AGENT_MODEL_CALL_RENEWAL_THRESHOLD",
    "TSM_AGT_EXPLORATION_MAX_TOOL_CALLS",
    "TSM_AGT_EXPLORATION_MAX_ACTIONS",
    "TSM_AGT_EXPLORATION_MAX_TOOL_SECONDS",
    "TSM_AGT_EXPLORATION_LOW_VALUE_STREAK",
    "TSM_AGT_EXPLORATION_RESERVE_TOOL_CALLS",
    "TSM_AGT_EXPLORATION_MIN_ACTIONS",
)


@dataclass(frozen=True, slots=True)
class ExplorationBudgetConfiguration:
    """User-facing values used to construct the replaceable budget policy."""

    # Sized from observed engineering Turns rather than from exploration alone:
    # reading a few files, editing them, and then running a build or test suite
    # already costs well over fifteen model calls once each Tool result comes
    # back for interpretation. A ceiling below that turns ordinary work into a
    # budget failure, which the user can only answer by continuing - and each
    # continuation re-reads context instead of finishing the job.
    agent_max_model_calls: int = 40
    agent_max_tool_calls: int = 120
    finalization_model_calls: int = 2
    execution_reserve_model_calls: int = 1
    recovery_reserve_model_calls: int = 1
    verification_reserve_model_calls: int = 1
    model_call_renewal_increments: tuple[int, ...] = (10, 5, 3)
    model_call_renewal_max_count: int = 3
    model_call_renewal_absolute_limit: int = 60
    model_call_renewal_threshold: int = 4
    max_tool_calls: int = 60
    max_actions: int = 60
    max_tool_seconds: int = 300
    low_value_streak: int = 2
    reserve_tool_calls: int = 2
    minimum_actions: int = 2
    profile: str = "balanced"
    sources: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        values = (
            self.agent_max_model_calls, self.agent_max_tool_calls,
            self.finalization_model_calls,
            self.execution_reserve_model_calls,
            self.recovery_reserve_model_calls,
            self.verification_reserve_model_calls,
            self.model_call_renewal_max_count,
            self.model_call_renewal_absolute_limit,
            self.model_call_renewal_threshold,
            self.max_tool_calls, self.max_actions, self.max_tool_seconds,
            self.low_value_streak, self.reserve_tool_calls, self.minimum_actions,
        )
        if min(values) < 1:
            raise ValueError("exploration budget values must be positive integers")
        if self.finalization_model_calls >= self.agent_max_model_calls:
            raise ValueError(
                "TSM_AGT_AGENT_FINALIZATION_MODEL_CALLS must be smaller than "
                "TSM_AGT_AGENT_MAX_MODEL_CALLS"
            )
        if (
            self.finalization_model_calls
            + self.execution_reserve_model_calls
            + self.recovery_reserve_model_calls
            + self.verification_reserve_model_calls
            >= self.agent_max_model_calls
        ):
            raise ValueError(
                "Agent execution, recovery, verification and finalization reserves "
                "must leave at least one normal model call"
            )
        if not self.model_call_renewal_increments or any(
            value < 1 for value in self.model_call_renewal_increments
        ):
            raise ValueError(
                "TSM_AGT_AGENT_MODEL_CALL_RENEWAL_INCREMENTS must contain "
                "positive integers"
            )
        if any(
            later > earlier for earlier, later in zip(
                self.model_call_renewal_increments,
                self.model_call_renewal_increments[1:],
            )
        ):
            raise ValueError(
                "TSM_AGT_AGENT_MODEL_CALL_RENEWAL_INCREMENTS must not increase"
            )
        if self.model_call_renewal_max_count > len(
            self.model_call_renewal_increments
        ):
            raise ValueError(
                "TSM_AGT_AGENT_MODEL_CALL_RENEWAL_MAX_COUNT exceeds the "
                "configured renewal increments"
            )
        if self.model_call_renewal_absolute_limit < self.agent_max_model_calls:
            raise ValueError(
                "TSM_AGT_AGENT_MODEL_CALL_RENEWAL_ABSOLUTE_LIMIT must not be "
                "smaller than TSM_AGT_AGENT_MAX_MODEL_CALLS"
            )
        if self.model_call_renewal_threshold >= self.agent_max_model_calls:
            raise ValueError(
                "TSM_AGT_AGENT_MODEL_CALL_RENEWAL_THRESHOLD must be smaller "
                "than TSM_AGT_AGENT_MAX_MODEL_CALLS"
            )
        if self.max_tool_calls > self.agent_max_tool_calls:
            raise ValueError(
                "TSM_AGT_EXPLORATION_MAX_TOOL_CALLS must not exceed "
                "TSM_AGT_AGENT_MAX_TOOL_CALLS"
            )
        if self.reserve_tool_calls >= self.max_tool_calls:
            raise ValueError(
                "TSM_AGT_EXPLORATION_RESERVE_TOOL_CALLS must be smaller than "
                "TSM_AGT_EXPLORATION_MAX_TOOL_CALLS"
            )
        if self.minimum_actions > self.max_actions:
            raise ValueError(
                "TSM_AGT_EXPLORATION_MIN_ACTIONS must not exceed "
                "TSM_AGT_EXPLORATION_MAX_ACTIONS"
            )
        if self.profile not in {"balanced", "legacy"}:
            raise ValueError(
                "TSM_AGT_EXPLORATION_PROFILE must be 'balanced' or 'legacy'"
            )

    def policy_arguments(self) -> dict[str, int]:
        return {
            "max_total_tool_calls": self.max_tool_calls,
            "max_scored_actions": self.max_actions,
            "max_cumulative_tool_milliseconds": self.max_tool_seconds * 1000,
            "max_low_value_streak": self.low_value_streak,
            "reserve_tool_calls": self.reserve_tool_calls,
            "minimum_scored_actions": self.minimum_actions,
        }

    def snapshot_data(self) -> dict[str, int | str | list[int]]:
        return {
            "policy_id": "builtin.rule-based-exploration-budget",
            "agent_max_model_calls": self.agent_max_model_calls,
            "agent_max_tool_calls": self.agent_max_tool_calls,
            "finalization_model_calls": self.finalization_model_calls,
            "execution_reserve_model_calls": self.execution_reserve_model_calls,
            "recovery_reserve_model_calls": self.recovery_reserve_model_calls,
            "verification_reserve_model_calls": self.verification_reserve_model_calls,
            "model_call_renewal_increments": list(
                self.model_call_renewal_increments
            ),
            "model_call_renewal_max_count": self.model_call_renewal_max_count,
            "model_call_renewal_absolute_limit": (
                self.model_call_renewal_absolute_limit
            ),
            "model_call_renewal_threshold": self.model_call_renewal_threshold,
            "max_tool_calls": self.max_tool_calls,
            "max_actions": self.max_actions,
            "max_tool_seconds": self.max_tool_seconds,
            "low_value_streak": self.low_value_streak,
            "reserve_tool_calls": self.reserve_tool_calls,
            "minimum_actions": self.minimum_actions,
            "profile": self.profile,
        }


_FIELD_BY_ENV = {
    "TSM_AGT_AGENT_MAX_MODEL_CALLS": "agent_max_model_calls",
    "TSM_AGT_AGENT_MAX_TOOL_CALLS": "agent_max_tool_calls",
    "TSM_AGT_AGENT_FINALIZATION_MODEL_CALLS": "finalization_model_calls",
    "TSM_AGT_AGENT_EXECUTION_RESERVE_MODEL_CALLS": "execution_reserve_model_calls",
    "TSM_AGT_AGENT_RECOVERY_RESERVE_MODEL_CALLS": "recovery_reserve_model_calls",
    "TSM_AGT_AGENT_VERIFICATION_RESERVE_MODEL_CALLS": "verification_reserve_model_calls",
    "TSM_AGT_AGENT_MODEL_CALL_RENEWAL_MAX_COUNT": "model_call_renewal_max_count",
    "TSM_AGT_AGENT_MODEL_CALL_RENEWAL_ABSOLUTE_LIMIT": "model_call_renewal_absolute_limit",
    "TSM_AGT_AGENT_MODEL_CALL_RENEWAL_THRESHOLD": "model_call_renewal_threshold",
    "TSM_AGT_EXPLORATION_MAX_TOOL_CALLS": "max_tool_calls",
    "TSM_AGT_EXPLORATION_MAX_ACTIONS": "max_actions",
    "TSM_AGT_EXPLORATION_MAX_TOOL_SECONDS": "max_tool_seconds",
    "TSM_AGT_EXPLORATION_LOW_VALUE_STREAK": "low_value_streak",
    "TSM_AGT_EXPLORATION_RESERVE_TOOL_CALLS": "reserve_tool_calls",
    "TSM_AGT_EXPLORATION_MIN_ACTIONS": "minimum_actions",
}


def load_exploration_budget_configuration(
    path: Path, environment: Mapping[str, str],
) -> ExplorationBudgetConfiguration:
    """Load optional limits; exported values override the workspace .env."""

    env_file = path.expanduser().resolve()
    file_values = _read_optional_values(env_file)
    defaults = ExplorationBudgetConfiguration()
    values: dict[str, int] = {}
    sources: dict[str, str] = {}
    for env_name, field_name in _FIELD_BY_ENV.items():
        exported = environment.get(env_name, "").strip()
        from_file = file_values.get(env_name, "").strip()
        raw_value = exported or from_file
        if not raw_value:
            values[field_name] = int(getattr(defaults, field_name))
            sources[f"exploration_budget.{field_name}"] = "default"
            continue
        try:
            parsed = int(raw_value)
        except ValueError as error:
            raise ValueError(f"{env_name} must be a positive integer") from error
        if parsed < 1:
            raise ValueError(f"{env_name} must be a positive integer")
        values[field_name] = parsed
        sources[f"exploration_budget.{field_name}"] = (
            "environment" if exported else "env_file"
        )
    increments_name = "TSM_AGT_AGENT_MODEL_CALL_RENEWAL_INCREMENTS"
    exported_increments = environment.get(increments_name, "").strip()
    file_increments = file_values.get(increments_name, "").strip()
    raw_increments = exported_increments or file_increments
    if raw_increments:
        try:
            renewal_increments = tuple(
                int(item.strip()) for item in raw_increments.split(",")
                if item.strip()
            )
        except ValueError as error:
            raise ValueError(
                f"{increments_name} must be comma-separated positive integers"
            ) from error
        if not renewal_increments or any(value < 1 for value in renewal_increments):
            raise ValueError(
                f"{increments_name} must be comma-separated positive integers"
            )
        sources["exploration_budget.model_call_renewal_increments"] = (
            "environment" if exported_increments else "env_file"
        )
    else:
        renewal_increments = defaults.model_call_renewal_increments
        sources["exploration_budget.model_call_renewal_increments"] = "default"
    exported_profile = environment.get("TSM_AGT_EXPLORATION_PROFILE", "").strip()
    file_profile = file_values.get("TSM_AGT_EXPLORATION_PROFILE", "").strip()
    profile = (exported_profile or file_profile or defaults.profile).casefold()
    sources["exploration_budget.profile"] = (
        "environment" if exported_profile else
        "env_file" if file_profile else "default"
    )
    return ExplorationBudgetConfiguration(
        **values, model_call_renewal_increments=renewal_increments,
        profile=profile, sources=sources
    )


def _read_optional_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError(f"exploration env path is not a file: {path}")
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
            # The model loader owns validation of unrelated lines.
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if name not in EXPLORATION_ENV_NAMES:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values
