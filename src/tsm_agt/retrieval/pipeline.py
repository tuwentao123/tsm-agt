from __future__ import annotations

from dataclasses import replace

from .providers import RetrievalProvider, SequentialProviderRunner
from .schemas import RetrievalProviderTelemetry, RetrievalSearchResponse


class RetrievalPipeline:
    def __init__(self, providers: list[RetrievalProvider]) -> None:
        self._providers = providers
        self._runner = SequentialProviderRunner()
        self._cache: dict[tuple[str, int], RetrievalSearchResponse] = {}

    def search(self, query: str, top_k: int) -> RetrievalSearchResponse:
        cache_key = (query.strip().lower(), top_k)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return replace(cached, cache_hit=True)

        execution = self._runner.execute(self._providers, query, top_k)
        response = RetrievalSearchResponse(
            query=query,
            provider=execution.provider,
            results=execution.results,
            telemetry=execution.telemetry,
            cache_hit=False,
        )
        self._cache[cache_key] = response
        return response
