"""Configuration for the bounded, read-only GitHub source adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

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


def load_github_source_configuration(
    document: Mapping[str, object],
    *,
    cli_token_resolver: "Callable[[], str] | None" = None,
) -> GitHubSourceConfiguration:
    """Load GitHub source settings, reusing an existing ``gh`` login when asked.

    The adapter's tools are the discoverable way to read one file of a public
    repository, but GitHub's REST API allows an anonymous caller only 60 requests
    per hour. Shell agents never hit that because they run inside
    the developer's already-authenticated shell. Enabling this adapter therefore
    adopts the same credential the developer already created with ``gh auth
    login`` instead of requiring a second, separately managed secret.
    """
    values = _mapping(document.get("github_source", {}), "github_source")
    issues: list[str] = []
    enabled, enabled_source = _bool(values, "enabled", False)
    base, base_source = _string(values, "api_base", "https://api.github.com")
    base = base.rstrip("/")
    hosts_value = values.get("allowed_hosts", ["api.github.com"])
    if not isinstance(hosts_value, list) or not all(isinstance(item, str) and item.strip() for item in hosts_value):
        raise ValueError("github_source.allowed_hosts must be a list of host names")
    hosts = tuple(sorted({item.strip().casefold() for item in hosts_value}))
    hosts_source = "yaml" if "allowed_hosts" in values else "default"
    token, token_source = _string(values, "token", "")
    if enabled and not token.strip():
        adopted = (cli_token_resolver or _gh_cli_token)()
        if adopted:
            token, token_source = adopted, "gh_cli"
    private_access, private_source = _bool(values, "private_access", False)
    repositories = values.get("allowed_repositories", [])
    if not isinstance(repositories, list) or not all(isinstance(item, str) and item.strip() for item in repositories):
        raise ValueError("github_source.allowed_repositories must be a list of repository names")
    repos = tuple(sorted({item.strip() for item in repositories}))
    repos_source = "yaml" if "allowed_repositories" in values else "default"
    ttl, ttl_source = _positive_float("cache_ttl_seconds", values, 300.0, issues)
    entries, entries_source = _positive_int("cache_max_entries", values, 128, issues)
    bytes_, bytes_source = _positive_int("response_max_bytes", values, 512_000, issues)
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        issues.append("github_source.api_base must be an HTTPS origin (an optional /api/v3 path is allowed)")
        enabled = False
    elif parsed.hostname.casefold() not in hosts:
        issues.append("github_source.api_base host must be in github_source.allowed_hosts")
        enabled = False
    if private_access and (not token.strip() or not repos):
        issues.append("private GitHub access requires github_source.token and explicit github_source.allowed_repositories")
        enabled = False
    return GitHubSourceConfiguration(enabled, base, hosts, token.strip(), private_access, repos, ttl, entries, bytes_, {
        "github_source.enabled": enabled_source, "github_source.api_base": base_source,
        "github_source.allowed_hosts": hosts_source, "github_source.private_access": private_source,
        "github_source.allowed_repositories": repos_source if repos else "unset",
        # Where a present token came from, so an operator can audit it: an
        # explicit setting ("configured"/"environment") or the developer's own
        # ``gh`` login ("gh_cli"). No secret is ever reported.
        "github_source.token": (
            token_source if token.strip() else "unset"
        ),
        "github_source.cache_ttl_seconds": ttl_source, "github_source.cache_max_entries": entries_source,
        "github_source.response_max_bytes": bytes_source,
    }, tuple(issues))


def _gh_executable() -> str | None:
    """Locate ``gh`` even when this process PATH omits the package manager."""

    found = shutil.which("gh")
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidate = Path(prefix) / "gh"
        if candidate.is_file():
            return str(candidate)
    return None


def _gh_cli_token() -> str:
    """The token of an existing ``gh`` login, or "" when there is none.

    Fail-soft by construction: a missing CLI, a logged-out host, or a slow
    command must leave the adapter on the anonymous path rather than break
    composition.
    """

    executable = _gh_executable()
    if executable is None:
        return ""
    try:
        completed = subprocess.run(
            [executable, "auth", "token"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _positive_int(name: str, values: Mapping[str, object], default: int, issues: list[str]) -> tuple[int, str]:
    raw = values.get(name)
    if raw is None:
        return default, "default"
    value = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
    if value <= 0:
        issues.append(f"github_source.{name} must be positive; using {default}")
        return default, "default"
    return value, "yaml"


def _positive_float(name: str, values: Mapping[str, object], default: float, issues: list[str]) -> tuple[float, str]:
    raw = values.get(name)
    if raw is None:
        return default, "default"
    value = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.0
    if value <= 0:
        issues.append(f"github_source.{name} must be positive; using {default}")
        return default, "default"
    return value, "yaml"


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a mapping")
    return value


def _string(values: Mapping[str, object], name: str, default: str) -> tuple[str, str]:
    value = values.get(name)
    if value is None:
        return default, "default"
    if not isinstance(value, str):
        raise ValueError(f"github_source.{name} must be a string")
    return value.strip(), "yaml"


def _bool(values: Mapping[str, object], name: str, default: bool) -> tuple[bool, str]:
    value = values.get(name)
    if value is None:
        return default, "default"
    if not isinstance(value, bool):
        raise ValueError(f"github_source.{name} must be true or false")
    return value, "yaml"
