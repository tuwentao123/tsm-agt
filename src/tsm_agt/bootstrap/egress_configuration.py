"""Project-local configuration for outbound document fetching."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tsm_agt.ports import WebEgressMode


EGRESS_ENV_NAMES = ("TSM_AGT_WEB_EGRESS_MODE",)


@dataclass(frozen=True, slots=True)
class WebEgressConfiguration:
    """Which component owns the egress decision for ``web.fetch_markdown``.

    The default is DIRECT: this process validates and pins the destination
    address itself. DELEGATED is an explicit statement that egress already
    passes through a proxy, VPN, or TUN device that enforces the policy, so
    locally resolved addresses describe the tunnel and must not be judged here.
    """

    mode: WebEgressMode = WebEgressMode.DIRECT
    sources: Mapping[str, str] | None = None


def load_web_egress_configuration(
    path: Path, environment: Mapping[str, str],
) -> WebEgressConfiguration:
    values = _read_values(path.expanduser().resolve())
    raw, source = _selected(
        "TSM_AGT_WEB_EGRESS_MODE", values, environment, str(WebEgressMode.DIRECT)
    )
    try:
        mode = WebEgressMode(raw.strip().upper())
    except ValueError as error:
        raise ValueError(
            "TSM_AGT_WEB_EGRESS_MODE must be 'DIRECT' or 'DELEGATED'"
        ) from error
    return WebEgressConfiguration(mode, {"web.egress_mode": source})


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
        if name not in EGRESS_ENV_NAMES:
            continue
        values[name] = _unquoted(value)
    return values
