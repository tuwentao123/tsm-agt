"""Project-local configuration for optional commercial search providers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


SEARCH_ENV_NAMES = (
    "TSM_AGT_SEARCH_TAVILY_MODE",
    "TSM_AGT_SEARCH_TAVILY_API_KEY",
)

#: Modes accepted for the optional Tavily provider.
TAVILY_MODES = ("off", "keyless", "key")


@dataclass(frozen=True, slots=True)
class SearchConfiguration:
    """Which third-party search providers the host opted into.

    The default is ``off``: this process only scrapes public endpoints that need
    no account. ``keyless`` and ``key`` explicitly forward user queries to
    Tavily, which is an egress/privacy decision the host must make on purpose.
    """

    tavily_mode: str = "off"
    tavily_api_key: str = ""
    sources: Mapping[str, str] | None = None
    #: Human-readable configuration problems. Search is an optional provider,
    #: so a bad value degrades to ``off`` and is reported instead of taking the
    #: whole Runtime (and therefore the Web UI) down with it.
    issues: tuple[str, ...] = ()


def load_search_configuration(
    path: Path, environment: Mapping[str, str],
) -> SearchConfiguration:
    values = _read_values(path.expanduser().resolve())
    raw_mode, mode_source = _selected(
        "TSM_AGT_SEARCH_TAVILY_MODE", values, environment, "off"
    )
    mode = raw_mode.strip().lower()
    issues: list[str] = []
    if mode not in TAVILY_MODES:
        issues.append(
            "TSM_AGT_SEARCH_TAVILY_MODE="
            f"{raw_mode.strip()!r} is not one of {TAVILY_MODES}; "
            "search fell back to the free providers"
        )
        mode = "off"
    raw_key, key_source = _selected(
        "TSM_AGT_SEARCH_TAVILY_API_KEY", values, environment, ""
    )
    api_key = raw_key.strip()
    if mode == "key" and not api_key:
        issues.append(
            "TSM_AGT_SEARCH_TAVILY_MODE='key' requires "
            "TSM_AGT_SEARCH_TAVILY_API_KEY; search fell back to the free "
            "providers"
        )
        mode = "off"
    return SearchConfiguration(
        tavily_mode=mode,
        tavily_api_key=api_key,
        sources={
            "search.tavily_mode": mode_source,
            # Record provenance without ever exposing the secret value.
            "search.tavily_api_key": (
                "configured" if api_key else ("environment" if key_source == "environment" else "unset")
            ),
        },
        issues=tuple(issues),
    )


def _selected(
    name: str, file_values: Mapping[str, str], environment: Mapping[str, str],
    default: str,
) -> tuple[str, str]:
    exported = environment.get(name, "").strip()
    from_file = file_values.get(name, "").strip()
    if exported:
        return exported, "environment"
    if from_file:
        return from_file, "env_file"
    return default, "default"


def _unquoted(value: str) -> str:
    """Return an unquoted dotenv value with any trailing `` # comment`` removed."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    for marker in (" #", "\t#"):
        index = value.find(marker)
        if index != -1:
            value = value[:index]
    return value.strip()


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
        if name not in SEARCH_ENV_NAMES:
            continue
        values[name] = _unquoted(value)
    return values
