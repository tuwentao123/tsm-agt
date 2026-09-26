from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from .schemas import (
    ProviderTimeoutError, RetrievalProviderTelemetry, RetrievalResult,
    is_timeout_error,
)

#: Per-provider wall-clock budget. Public endpoints (search engines, RSS feeds)
#: answer in well under a second when they answer at all; a longer limit only
#: lets one unresponsive host consume the whole tool deadline.
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 6.0

#: Never start a provider with less than this much budget left: a request that
#: must be abandoned immediately is not worth a socket round trip.
_MIN_PROVIDER_BUDGET_SECONDS = 0.5


class RetrievalProvider(Protocol):
    name: str

    def search(
        self, query: str, top_k: int, *,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        freshness: str = "any",
    ) -> list[RetrievalResult]: ...


@dataclass(slots=True)
class ProviderExecution:
    provider: str
    results: list[RetrievalResult]
    telemetry: list[RetrievalProviderTelemetry]


class SequentialProviderRunner:
    """Run providers in order under one shared wall-clock deadline.

    Providers are deliberately sequential: each one is a cheap fallback for the
    previous, and the first provider with enough results short-circuits the
    rest. The deadline is what stops one hanging provider from starving every
    later source, so it is threaded through to each request as ``timeout_seconds``.
    """

    def __init__(
        self,
        provider_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        if provider_timeout_seconds <= 0:
            raise ValueError("provider_timeout_seconds must be positive")
        self._provider_timeout_seconds = provider_timeout_seconds

    def execute(
        self,
        providers: list[RetrievalProvider],
        query: str,
        top_k: int,
        *,
        deadline: float | None = None,
        freshness: str = "any",
    ) -> ProviderExecution:
        telemetry: list[RetrievalProviderTelemetry] = []
        aggregated: list[RetrievalResult] = []
        provider_names: list[str] = []
        seen_urls: set[str] = set()

        for provider in providers:
            remaining: float | None = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= _MIN_PROVIDER_BUDGET_SECONDS:
                    # The shared budget is spent; record why the remaining
                    # sources were never asked rather than pretending they
                    # returned nothing.
                    telemetry.append(RetrievalProviderTelemetry(
                        provider=provider.name,
                        status="skipped",
                        latency_ms=0,
                        result_count=0,
                        error="search budget exhausted before this provider ran",
                    ))
                    break

            timeout_seconds = (
                self._provider_timeout_seconds
                if remaining is None
                else min(self._provider_timeout_seconds, remaining)
            )
            started = time.monotonic()
            try:
                results = provider.search(
                    query, top_k, timeout_seconds=timeout_seconds,
                    freshness=freshness,
                )
                latency_ms = int((time.monotonic() - started) * 1000)
                unique_results: list[RetrievalResult] = []

                for result in results:
                    normalized_url = result.url.rstrip("/")
                    if normalized_url in seen_urls:
                        continue
                    seen_urls.add(normalized_url)
                    unique_results.append(result)

                entry = RetrievalProviderTelemetry(
                    provider=provider.name,
                    status="success" if unique_results else "empty",
                    latency_ms=latency_ms,
                    result_count=len(unique_results),
                )
                telemetry.append(entry)

                if unique_results:
                    provider_names.append(provider.name)
                    aggregated.extend(unique_results)

                if len(aggregated) >= top_k:
                    break
            except Exception as error:
                latency_ms = int((time.monotonic() - started) * 1000)
                timed_out = (
                    isinstance(error, ProviderTimeoutError)
                    or is_timeout_error(error)
                )
                telemetry.append(
                    RetrievalProviderTelemetry(
                        provider=provider.name,
                        status="timeout" if timed_out else "error",
                        latency_ms=latency_ms,
                        result_count=0,
                        error=str(error),
                    )
                )

        if aggregated:
            return ProviderExecution(
                provider="+".join(provider_names),
                results=aggregated[:top_k],
                telemetry=telemetry,
            )

        return ProviderExecution(
            provider="degraded-no-results",
            results=[],
            telemetry=telemetry,
        )
