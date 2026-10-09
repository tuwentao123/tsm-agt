"""Project-local configuration for optional commercial search providers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


SEARCH_ENV_NAMES = (
    "TSM_AGT_SEARCH_MODE",
    "TSM_AGT_SEARCH_TAVILY_MODE",
    "TSM_AGT_SEARCH_TAVILY_API_KEY",
    "TSM_AGT_WEB_RENDER_URL",
    "TSM_AGT_WEB_DOCUMENT_CACHE_MAX_ENTRIES",
    "TSM_AGT_WEB_DOCUMENT_CACHE_MAX_BYTES",
    "TSM_AGT_WEB_REFERENCE_CACHE_MAX_ENTRIES",
    "TSM_AGT_WEB_DOCUMENT_CACHE_TTL_SECONDS",
)

DEFAULT_DOCUMENT_CACHE_MAX_ENTRIES = 32
DEFAULT_DOCUMENT_CACHE_MAX_BYTES = 1_000_000
DEFAULT_REFERENCE_CACHE_MAX_ENTRIES = 128
DEFAULT_DOCUMENT_CACHE_TTL_SECONDS = 600.0

#: Modes accepted for the optional Tavily provider.
TAVILY_MODES = ("off", "keyless", "key")

#: How far ``web.search`` may go, mirroring Codex's ``WebSearchMode``.
#:
#: * ``off``    -- the tool refuses; nothing is sent anywhere.
#: * ``cached`` -- only results this process already holds; **no egress**.
#: * ``live``   -- query the public providers.
#:
#: The default is ``live``, a deliberate departure from Codex's ``Cached``
#: default: Codex can afford ``Cached`` because its provider indexes the web
#: server-side, while this Runtime has no index behind the cache -- ``cached``
#: would ship a search tool that answers nothing until something else filled the
#: cache. ``live`` queries public endpoints that need no account; ``cached`` and
#: ``off`` exist for hosts that want the stricter posture.
DEFAULT_SEARCH_MODE = "live"
SEARCH_MODES = ("off", "cached", "live")


@dataclass(frozen=True, slots=True)
class SearchConfiguration:
    """Which third-party search providers the host opted into.

    The default is ``off``: this process only scrapes public endpoints that need
    no account. ``keyless`` and ``key`` explicitly forward user queries to
    Tavily, which is an egress/privacy decision the host must make on purpose.
    ``search_mode`` sits above that and governs whether any egress happens at
    all.
    """

    tavily_mode: str = "off"
    tavily_api_key: str = ""
    search_mode: str = DEFAULT_SEARCH_MODE
    #: Optional host-supplied service that renders a URL to text. Empty means no
    #: rendering: a JS-only page simply comes back thin, as it always has.
    render_url: str = ""
    document_cache_max_entries: int = DEFAULT_DOCUMENT_CACHE_MAX_ENTRIES
    document_cache_max_bytes: int = DEFAULT_DOCUMENT_CACHE_MAX_BYTES
    reference_cache_max_entries: int = DEFAULT_REFERENCE_CACHE_MAX_ENTRIES
    document_cache_ttl_seconds: float = DEFAULT_DOCUMENT_CACHE_TTL_SECONDS
    sources: Mapping[str, str] | None = None
    #: Human-readable configuration problems. Search is an optional provider,
    #: so a bad value degrades to ``off`` and is reported instead of taking the
    #: whole Runtime (and therefore the Web UI) down with it.
    issues: tuple[str, ...] = ()


def load_search_configuration(
    path: Path, environment: Mapping[str, str],
) -> SearchConfiguration:
    values = _read_values(path.expanduser().resolve())
    issues: list[str] = []
    raw_search_mode, search_mode_source = _selected(
        "TSM_AGT_SEARCH_MODE", values, environment, DEFAULT_SEARCH_MODE
    )
    search_mode = raw_search_mode.strip().lower()
    if search_mode not in SEARCH_MODES:
        issues.append(
            f"TSM_AGT_SEARCH_MODE={raw_search_mode.strip()!r} is not one of "
            f"{SEARCH_MODES}; search stays in {DEFAULT_SEARCH_MODE!r} mode"
        )
        search_mode = DEFAULT_SEARCH_MODE
    raw_mode, mode_source = _selected(
        "TSM_AGT_SEARCH_TAVILY_MODE", values, environment, "off"
    )
    mode = raw_mode.strip().lower()
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
    raw_render_url, render_source = _selected(
        "TSM_AGT_WEB_RENDER_URL", values, environment, ""
    )
    render_url = raw_render_url.strip()
    if render_url and not render_url.startswith(("http://", "https://")):
        issues.append(
            f"TSM_AGT_WEB_RENDER_URL={render_url!r} must be an absolute "
            "http(s) URL; page rendering stays disabled"
        )
        render_url = ""
    document_cache_max_entries, document_cache_entries_source = _positive_int(
        "TSM_AGT_WEB_DOCUMENT_CACHE_MAX_ENTRIES", values, environment,
        DEFAULT_DOCUMENT_CACHE_MAX_ENTRIES, issues,
    )
    document_cache_max_bytes, document_cache_bytes_source = _positive_int(
        "TSM_AGT_WEB_DOCUMENT_CACHE_MAX_BYTES", values, environment,
        DEFAULT_DOCUMENT_CACHE_MAX_BYTES, issues,
    )
    reference_cache_max_entries, reference_cache_entries_source = _positive_int(
        "TSM_AGT_WEB_REFERENCE_CACHE_MAX_ENTRIES", values, environment,
        DEFAULT_REFERENCE_CACHE_MAX_ENTRIES, issues,
    )
    document_cache_ttl_seconds, document_cache_ttl_source = _positive_float(
        "TSM_AGT_WEB_DOCUMENT_CACHE_TTL_SECONDS", values, environment,
        DEFAULT_DOCUMENT_CACHE_TTL_SECONDS, issues,
    )
    return SearchConfiguration(
        tavily_mode=mode,
        render_url=render_url,
        tavily_api_key=api_key,
        search_mode=search_mode,
        document_cache_max_entries=document_cache_max_entries,
        document_cache_max_bytes=document_cache_max_bytes,
        reference_cache_max_entries=reference_cache_max_entries,
        document_cache_ttl_seconds=document_cache_ttl_seconds,
        sources={
            "search.mode": search_mode_source,
            "web.render_url": render_source if render_url else "unset",
            "web.document_cache_max_entries": document_cache_entries_source,
            "web.document_cache_max_bytes": document_cache_bytes_source,
            "web.reference_cache_max_entries": reference_cache_entries_source,
            "web.document_cache_ttl_seconds": document_cache_ttl_source,
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


def _positive_int(
    name: str, file_values: Mapping[str, str], environment: Mapping[str, str],
    default: int, issues: list[str],
) -> tuple[int, str]:
    raw, source = _selected(name, file_values, environment, str(default))
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        issues.append(f"{name}={raw.strip()!r} must be a positive integer; using {default}")
        return default, "default"
    return value, source


def _positive_float(
    name: str, file_values: Mapping[str, str], environment: Mapping[str, str],
    default: float, issues: list[str],
) -> tuple[float, str]:
    raw, source = _selected(name, file_values, environment, str(default))
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        issues.append(f"{name}={raw.strip()!r} must be a positive number; using {default}")
        return default, "default"
    return value, source


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
