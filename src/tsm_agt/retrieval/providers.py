from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from .schemas import RetrievalProviderTelemetry, RetrievalResult


class RetrievalProvider(Protocol):
    name: str

    def search(self, query: str, top_k: int) -> list[RetrievalResult]: ...


@dataclass(slots=True)
class ProviderExecution:
    provider: str
    results: list[RetrievalResult]
    telemetry: list[RetrievalProviderTelemetry]


class SequentialProviderRunner:
    def execute(
        self,
        providers: list[RetrievalProvider],
        query: str,
        top_k: int,
    ) -> ProviderExecution:
        telemetry: list[RetrievalProviderTelemetry] = []
        aggregated: list[RetrievalResult] = []
        provider_names: list[str] = []
        seen_urls: set[str] = set()

        for provider in providers:
            started = time.monotonic()
            try:
                results = provider.search(query, top_k)
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
                telemetry.append(
                    RetrievalProviderTelemetry(
                        provider=provider.name,
                        status="error",
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
