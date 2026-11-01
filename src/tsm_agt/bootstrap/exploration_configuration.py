"""Configurable limits for evidence-gathering exploration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExplorationBudgetConfiguration:
    """User-facing values used to construct the replaceable budget policy."""

    # Sized from observed engineering Turns rather than from exploration alone:
    # reading a few files, editing them, and then running a build or test suite
    # already costs well over fifteen model calls once each Tool result comes
    # back for interpretation. A ceiling below that turns ordinary work into a
    # budget failure, which the user can only answer by continuing - and each
    # continuation re-reads context instead of finishing the job.
    agent_max_model_calls: int = 90
    agent_max_tool_calls: int = 120
    finalization_model_calls: int = 2
    execution_reserve_model_calls: int = 1
    recovery_reserve_model_calls: int = 1
    verification_reserve_model_calls: int = 1
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
            self.max_tool_calls, self.max_actions, self.max_tool_seconds,
            self.low_value_streak, self.reserve_tool_calls, self.minimum_actions,
        )
        if min(values) < 1:
            raise ValueError("exploration budget values must be positive integers")
        if self.finalization_model_calls >= self.agent_max_model_calls:
            raise ValueError(
                "agent.finalization_model_calls must be smaller than "
                "agent.max_model_calls"
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
        if self.max_tool_calls > self.agent_max_tool_calls:
            raise ValueError(
                "exploration.max_tool_calls must not exceed "
                "agent.max_tool_calls"
            )
        if self.reserve_tool_calls >= self.max_tool_calls:
            raise ValueError(
                "exploration.reserve_tool_calls must be smaller than "
                "exploration.max_tool_calls"
            )
        if self.minimum_actions > self.max_actions:
            raise ValueError(
                "exploration.minimum_actions must not exceed "
                "exploration.max_actions"
            )
        if self.profile not in {"balanced", "legacy"}:
            raise ValueError(
                "exploration.profile must be 'balanced' or 'legacy'"
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
            "max_tool_calls": self.max_tool_calls,
            "max_actions": self.max_actions,
            "max_tool_seconds": self.max_tool_seconds,
            "low_value_streak": self.low_value_streak,
            "reserve_tool_calls": self.reserve_tool_calls,
            "minimum_actions": self.minimum_actions,
            "profile": self.profile,
        }


def load_exploration_budget_configuration(
    document: Mapping[str, object],
) -> ExplorationBudgetConfiguration:
    """Load optional budget settings from the single private YAML document."""

    agent = _section(document, "agent")
    exploration = _section(document, "exploration")
    defaults = ExplorationBudgetConfiguration()
    values: dict[str, int] = {
        "agent_max_model_calls": _positive_int(
            agent.get("max_model_calls"), "agent.max_model_calls",
            defaults.agent_max_model_calls,
        ),
        "agent_max_tool_calls": _positive_int(
            agent.get("max_tool_calls"), "agent.max_tool_calls",
            defaults.agent_max_tool_calls,
        ),
        "finalization_model_calls": _positive_int(
            agent.get("finalization_model_calls"), "agent.finalization_model_calls",
            defaults.finalization_model_calls,
        ),
        "execution_reserve_model_calls": _positive_int(
            agent.get("execution_reserve_model_calls"), "agent.execution_reserve_model_calls",
            defaults.execution_reserve_model_calls,
        ),
        "recovery_reserve_model_calls": _positive_int(
            agent.get("recovery_reserve_model_calls"), "agent.recovery_reserve_model_calls",
            defaults.recovery_reserve_model_calls,
        ),
        "verification_reserve_model_calls": _positive_int(
            agent.get("verification_reserve_model_calls"), "agent.verification_reserve_model_calls",
            defaults.verification_reserve_model_calls,
        ),
        "max_tool_calls": _positive_int(
            exploration.get("max_tool_calls"), "exploration.max_tool_calls",
            defaults.max_tool_calls,
        ),
        "max_actions": _positive_int(
            exploration.get("max_actions"), "exploration.max_actions",
            defaults.max_actions,
        ),
        "max_tool_seconds": _positive_int(
            exploration.get("max_tool_seconds"), "exploration.max_tool_seconds",
            defaults.max_tool_seconds,
        ),
        "low_value_streak": _positive_int(
            exploration.get("low_value_streak"), "exploration.low_value_streak",
            defaults.low_value_streak,
        ),
        "reserve_tool_calls": _positive_int(
            exploration.get("reserve_tool_calls"), "exploration.reserve_tool_calls",
            defaults.reserve_tool_calls,
        ),
        "minimum_actions": _positive_int(
            exploration.get("minimum_actions"), "exploration.minimum_actions",
            defaults.minimum_actions,
        ),
    }
    sources: dict[str, str] = {}
    for field_name in values:
        source_section = "agent" if field_name.startswith(("agent_", "finalization", "execution_", "recovery_", "verification_")) else "exploration"
        sources[f"exploration_budget.{field_name}"] = "yaml" if source_section == "agent" and agent else "yaml" if source_section == "exploration" and exploration else "default"
    raw_profile = exploration.get("profile", defaults.profile)
    if not isinstance(raw_profile, str):
        raise ValueError("exploration.profile must be a string")
    profile = raw_profile.casefold()
    sources["exploration_budget.profile"] = "yaml" if "profile" in exploration else "default"
    return ExplorationBudgetConfiguration(
        **values, profile=profile, sources=sources
    )


def _section(document: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = document.get(name, {})
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a mapping")
    return value


def _positive_int(value: object, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value
