"""Replaceable model capability lookup for OpenAI reasoning effort."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReasoningEffortProfile:
    allowed_efforts: tuple[str, ...]


class ReasoningEffortRegistry:
    """Resolve profiles from a supplied mapping; unknown models remain upstream-defined."""

    def __init__(self, profiles: Mapping[str, ReasoningEffortProfile]):
        self._profiles = {name.casefold(): profile for name, profile in profiles.items()}

    def profile_for(self, model: str) -> ReasoningEffortProfile | None:
        return self._profiles.get(model.strip().casefold())

    def resolve(self, model: str, effort: str | None) -> str | None:
        if effort is None or not effort.strip():
            return None
        value = effort.strip().casefold()
        profile = self.profile_for(model)
        if profile is not None and value not in profile.allowed_efforts:
            raise ValueError(
                f"Unsupported reasoning effort {value!r} for {model!r}; "
                f"allowed: {', '.join(profile.allowed_efforts)}"
            )
        return value


_GPT_51 = ReasoningEffortProfile(("none", "low", "medium", "high"))
DEFAULT_REASONING_EFFORT_REGISTRY = ReasoningEffortRegistry({
    "gpt-5.1-2025-11-13": _GPT_51,
    "gpt-5.1": _GPT_51,
    "OpenAI-GPT-5.1": _GPT_51,
})
