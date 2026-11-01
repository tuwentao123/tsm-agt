"""Private configuration for optional commercial search providers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

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


def load_search_configuration(document: Mapping[str, object]) -> SearchConfiguration:
    """Load optional search settings from ``web.search`` in the YAML config."""

    web = _mapping(document.get("web", {}), "web")
    values = _mapping(web.get("search", {}), "web.search")
    issues: list[str] = []
    raw_search_mode, search_mode_source = _selected(values, "mode", DEFAULT_SEARCH_MODE)
    search_mode = raw_search_mode.strip().lower()
    if search_mode not in SEARCH_MODES:
        issues.append(
            f"web.search.mode={raw_search_mode.strip()!r} is not one of "
            f"{SEARCH_MODES}; search stays in {DEFAULT_SEARCH_MODE!r} mode"
        )
        search_mode = DEFAULT_SEARCH_MODE
    raw_mode, mode_source = _selected(values, "tavily_mode", "off")
    mode = raw_mode.strip().lower()
    if mode not in TAVILY_MODES:
        issues.append(
            "web.search.tavily_mode="
            f"{raw_mode.strip()!r} is not one of {TAVILY_MODES}; "
            "search fell back to the free providers"
        )
        mode = "off"
    raw_key, key_source = _selected(values, "tavily_api_key", "")
    api_key = raw_key.strip()
    if mode == "key" and not api_key:
        issues.append(
            "web.search.tavily_mode='key' requires web.search.tavily_api_key; "
            "search fell back to the free "
            "providers"
        )
        mode = "off"
    raw_render_url, render_source = _selected(values, "render_url", "")
    render_url = raw_render_url.strip()
    if render_url and not render_url.startswith(("http://", "https://")):
        issues.append(
            f"web.search.render_url={render_url!r} must be an absolute "
            "http(s) URL; page rendering stays disabled"
        )
        render_url = ""
    document_cache_max_entries, document_cache_entries_source = _positive_int(
        "document_cache_max_entries", values,
        DEFAULT_DOCUMENT_CACHE_MAX_ENTRIES, issues,
    )
    document_cache_max_bytes, document_cache_bytes_source = _positive_int(
        "document_cache_max_bytes", values,
        DEFAULT_DOCUMENT_CACHE_MAX_BYTES, issues,
    )
    reference_cache_max_entries, reference_cache_entries_source = _positive_int(
        "reference_cache_max_entries", values,
        DEFAULT_REFERENCE_CACHE_MAX_ENTRIES, issues,
    )
    document_cache_ttl_seconds, document_cache_ttl_source = _positive_float(
        "document_cache_ttl_seconds", values,
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
                "configured" if api_key else "unset"
            ),
        },
        issues=tuple(issues),
    )


def _selected(
    values: Mapping[str, object], name: str, default: str,
) -> tuple[str, str]:
    value = values.get(name)
    if value is None:
        return default, "default"
    if not isinstance(value, str):
        raise ValueError(f"web.search.{name} must be a string")
    return value, "yaml"


def _positive_int(
    name: str, values: Mapping[str, object], default: int, issues: list[str],
) -> tuple[int, str]:
    raw_value = values.get(name)
    if raw_value is None:
        return default, "default"
    value = raw_value if isinstance(raw_value, int) and not isinstance(raw_value, bool) else 0
    if value <= 0:
        issues.append(f"web.search.{name} must be a positive integer; using {default}")
        return default, "default"
    return value, "yaml"


def _positive_float(
    name: str, values: Mapping[str, object], default: float, issues: list[str],
) -> tuple[float, str]:
    raw_value = values.get(name)
    if raw_value is None:
        return default, "default"
    value = float(raw_value) if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool) else 0.0
    if value <= 0:
        issues.append(f"web.search.{name} must be a positive number; using {default}")
        return default, "default"
    return value, "yaml"


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a mapping")
    return value
