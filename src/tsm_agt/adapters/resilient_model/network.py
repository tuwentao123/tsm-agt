"""Reachability probe for the configured model endpoint.

The recovery policy has to tell "the provider answered badly" apart from "the
network was gone". Only a transport-level connect answers the second question
without sending the request again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit


def network_probe_for(
    origin: str, *, timeout_seconds: float = 3.0,
) -> Callable[[], Awaitable[bool]]:
    """Return a probe that resolves to whether ``origin`` accepts a TCP connect."""

    parsed = urlsplit(origin.strip())
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    async def probe() -> bool:
        if not host:
            return False
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout_seconds
            )
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, RuntimeError):
            pass
        return True

    return probe
