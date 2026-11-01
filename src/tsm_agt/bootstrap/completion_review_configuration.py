"""Completion Reviewer configuration loaded without mutating the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from tsm_agt.ports import CompletionReviewFailurePolicy, CompletionReviewMode


@dataclass(frozen=True, slots=True)
class CompletionReviewConfiguration:
    mode: CompletionReviewMode = CompletionReviewMode.OFF
    max_continuations: int = 1
    failure_policy: CompletionReviewFailurePolicy = (
        CompletionReviewFailurePolicy.VISIBLE_UNVERIFIED
    )


def load_completion_review_configuration(
    document: Mapping[str, object],
) -> CompletionReviewConfiguration:
    """Load Completion Reviewer policy from ``completion_review`` YAML."""

    values = document.get("completion_review", {})
    if not isinstance(values, Mapping) or any(not isinstance(key, str) for key in values):
        raise ValueError("completion_review must be a mapping")
    mode_value = values.get("mode", "off")
    maximum_value = values.get("max_continuations", 1)
    failure_value = values.get("failure_policy", "visible_unverified")
    if not isinstance(mode_value, str) or not isinstance(failure_value, str):
        raise ValueError("completion_review mode and failure_policy must be strings")
    if isinstance(maximum_value, bool) or not isinstance(maximum_value, int):
        raise ValueError("completion_review.max_continuations must be an integer")

    try:
        mode = CompletionReviewMode(mode_value.lower())
        maximum = maximum_value
        failure = CompletionReviewFailurePolicy(failure_value.lower())
    except ValueError as error:
        raise ValueError(
            "invalid completion review configuration: mode=always|required|off, "
            "failure_policy=visible_unverified|incomplete|accept, "
            "max_continuations>=0"
        ) from error
    if maximum < 0:
        raise ValueError("completion review max_continuations must not be negative")
    return CompletionReviewConfiguration(mode, maximum, failure)


__all__ = [
    "CompletionReviewConfiguration", "load_completion_review_configuration",
]
