"""Private configuration for outbound document fetching."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from tsm_agt.ports import WebEgressMode


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


def load_web_egress_configuration(document: Mapping[str, object]) -> WebEgressConfiguration:
    """Load egress mode from ``web.egress_mode`` in the private YAML file."""

    web = document.get("web", {})
    if not isinstance(web, Mapping) or any(not isinstance(key, str) for key in web):
        raise ValueError("web must be a mapping")
    raw = web.get("egress_mode", str(WebEgressMode.DIRECT))
    if not isinstance(raw, str):
        raise ValueError("web.egress_mode must be a string")
    try:
        mode = WebEgressMode(raw.strip().upper())
    except ValueError as error:
        raise ValueError(
            "web.egress_mode must be 'DIRECT' or 'DELEGATED'"
        ) from error
    return WebEgressConfiguration(
        mode, {"web.egress_mode": "yaml" if "egress_mode" in web else "default"},
    )
