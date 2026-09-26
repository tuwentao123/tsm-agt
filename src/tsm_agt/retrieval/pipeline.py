from __future__ import annotations

from dataclasses import replace

from .providers import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS, RetrievalProvider, SequentialProviderRunner,
)
from .schemas import RetrievalSearchResponse


class RetrievalPipeline:
    def __init__(
        self,
        providers: list[RetrievalProvider],
        provider_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        self._providers = providers
        self._runner = SequentialProviderRunner(provider_timeout_seconds)
        self._cache: dict[tuple[str, int, str], RetrievalSearchResponse] = {}

    def search(
        self, query: str, top_k: int, *, deadline: float | None = None,
        freshness: str = "any",
    ) -> RetrievalSearchResponse:
        """Search public providers under an optional shared wall-clock deadline.

        ``deadline`` is a :func:`time.monotonic` timestamp. When omitted the
        pipeline falls back to the runner's per-provider timeout only. Callers
        that invoke this from a Tool must pass the Tool deadline so one slow
        provider cannot consume the whole Tool budget. ``freshness`` is the
        recency window (``any``/``day``/``week``/``month``) mapped onto each
        provider's native filter.
        """
        cache_key = (query.strip().lower(), top_k, freshness)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return replace(cached, cache_hit=True)

        execution = self._runner.execute(
            self._providers, query, top_k, deadline=deadline,
            freshness=freshness,
        )
        response = RetrievalSearchResponse(
            query=query,
            provider=execution.provider,
            results=execution.results,
            telemetry=execution.telemetry,
            cache_hit=False,
        )
        self._cache[cache_key] = response
        return response
