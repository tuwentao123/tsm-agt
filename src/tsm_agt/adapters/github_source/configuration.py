"""Configuration for the bounded, read-only GitHub source adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_GITHUB_ENV = (
    "TSM_AGT_GITHUB_SOURCE_ENABLED", "TSM_AGT_GITHUB_API_BASE",
    "TSM_AGT_GITHUB_ALLOWED_HOSTS", "TSM_AGT_GITHUB_TOKEN",
    "TSM_AGT_GITHUB_PRIVATE_ACCESS", "TSM_AGT_GITHUB_ALLOWED_REPOSITORIES",
    "TSM_AGT_GITHUB_CACHE_TTL_SECONDS", "TSM_AGT_GITHUB_CACHE_MAX_ENTRIES",
    "TSM_AGT_GITHUB_RESPONSE_MAX_BYTES",
)


@dataclass(frozen=True, slots=True)
class GitHubSourceConfiguration:
    enabled: bool = False
    api_base: str = "https://api.github.com"
    allowed_hosts: tuple[str, ...] = ("api.github.com",)
    token: str = ""
    private_access: bool = False
    allowed_repositories: tuple[str, ...] = ()
    cache_ttl_seconds: float = 300.0
    cache_max_entries: int = 128
    response_max_bytes: int = 512_000
    sources: Mapping[str, str] | None = None
    issues: tuple[str, ...] = ()


def load_github_source_configuration(path: Path, environment: Mapping[str, str]) -> GitHubSourceConfiguration:
    values = _read_values(path.expanduser().resolve())
    issues: list[str] = []
    enabled_raw, enabled_source = _selected("TSM_AGT_GITHUB_SOURCE_ENABLED", values, environment, "false")
    enabled = enabled_raw.casefold() in {"1", "true", "yes"}
    base, base_source = _selected("TSM_AGT_GITHUB_API_BASE", values, environment, "https://api.github.com")
    base = base.rstrip("/")
    hosts_raw, hosts_source = _selected("TSM_AGT_GITHUB_ALLOWED_HOSTS", values, environment, "api.github.com")
    hosts = tuple(sorted({item.strip().casefold() for item in hosts_raw.split(",") if item.strip()}))
    token, token_source = _selected("TSM_AGT_GITHUB_TOKEN", values, environment, "")
    private_raw, private_source = _selected("TSM_AGT_GITHUB_PRIVATE_ACCESS", values, environment, "false")
    private_access = private_raw.casefold() in {"1", "true", "yes"}
    repos_raw, repos_source = _selected("TSM_AGT_GITHUB_ALLOWED_REPOSITORIES", values, environment, "")
    repos = tuple(sorted({item.strip() for item in repos_raw.split(",") if item.strip()}))
    ttl, ttl_source = _positive_float("TSM_AGT_GITHUB_CACHE_TTL_SECONDS", values, environment, 300.0, issues)
    entries, entries_source = _positive_int("TSM_AGT_GITHUB_CACHE_MAX_ENTRIES", values, environment, 128, issues)
    bytes_, bytes_source = _positive_int("TSM_AGT_GITHUB_RESPONSE_MAX_BYTES", values, environment, 512_000, issues)
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        issues.append("TSM_AGT_GITHUB_API_BASE must be an HTTPS origin (an optional /api/v3 path is allowed)")
        enabled = False
    elif parsed.hostname.casefold() not in hosts:
        issues.append("TSM_AGT_GITHUB_API_BASE host must be in TSM_AGT_GITHUB_ALLOWED_HOSTS")
        enabled = False
    if private_access and (not token.strip() or not repos):
        issues.append("private GitHub access requires TSM_AGT_GITHUB_TOKEN and explicit TSM_AGT_GITHUB_ALLOWED_REPOSITORIES")
        enabled = False
    return GitHubSourceConfiguration(enabled, base, hosts, token.strip(), private_access, repos, ttl, entries, bytes_, {
        "github_source.enabled": enabled_source, "github_source.api_base": base_source,
        "github_source.allowed_hosts": hosts_source, "github_source.private_access": private_source,
        "github_source.allowed_repositories": repos_source if repos else "unset",
        "github_source.token": "configured" if token.strip() else ("environment" if token_source == "environment" else "unset"),
        "github_source.cache_ttl_seconds": ttl_source, "github_source.cache_max_entries": entries_source,
        "github_source.response_max_bytes": bytes_source,
    }, tuple(issues))


def _selected(name: str, file_values: Mapping[str, str], environment: Mapping[str, str], default: str) -> tuple[str, str]:
    if value := environment.get(name, "").strip(): return value, "environment"
    if value := file_values.get(name, "").strip(): return value, "env_file"
    return default, "default"


def _positive_int(name: str, values: Mapping[str, str], environment: Mapping[str, str], default: int, issues: list[str]) -> tuple[int, str]:
    raw, source = _selected(name, values, environment, str(default))
    try: value = int(raw)
    except ValueError: value = 0
    if value <= 0:
        issues.append(f"{name} must be positive; using {default}")
        return default, "default"
    return value, source


def _positive_float(name: str, values: Mapping[str, str], environment: Mapping[str, str], default: float, issues: list[str]) -> tuple[float, str]:
    raw, source = _selected(name, values, environment, str(default))
    try: value = float(raw)
    except ValueError: value = 0.0
    if value <= 0:
        issues.append(f"{name} must be positive; using {default}")
        return default, "default"
    return value, source


def _read_values(path: Path) -> dict[str, str]:
    if not path.exists(): return {}
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("export "): line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line: continue
        name, value = (part.strip() for part in line.split("=", 1))
        if name in _GITHUB_ENV: result[name] = value.strip("'\"")
    return result
