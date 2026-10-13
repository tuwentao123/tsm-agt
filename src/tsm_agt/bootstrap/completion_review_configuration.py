"""Completion Reviewer configuration loaded without mutating the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tsm_agt.ports import CompletionReviewFailurePolicy, CompletionReviewMode


@dataclass(frozen=True, slots=True)
class CompletionReviewConfiguration:
    mode: CompletionReviewMode = CompletionReviewMode.OFF
    max_continuations: int = 1
    failure_policy: CompletionReviewFailurePolicy = (
        CompletionReviewFailurePolicy.VISIBLE_UNVERIFIED
    )


def load_completion_review_configuration(
    path: Path, environment: Mapping[str, str],
) -> CompletionReviewConfiguration:
    values: dict[str, str] = {}
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")

    def value(name: str, default: str) -> str:
        return environment.get(name, values.get(name, default)).strip()

    try:
        mode = CompletionReviewMode(
            value("TSM_AGT_COMPLETION_REVIEW_MODE", "off").lower()
        )
        maximum = int(value(
            "TSM_AGT_COMPLETION_REVIEW_MAX_CONTINUATIONS", "1"
        ))
        failure = CompletionReviewFailurePolicy(value(
            "TSM_AGT_COMPLETION_REVIEW_FAILURE_POLICY", "visible_unverified"
        ).lower())
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
