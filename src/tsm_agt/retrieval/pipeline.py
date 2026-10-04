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
        freshness: str = "any", allow_egress: bool = True,
    ) -> RetrievalSearchResponse:
        """Search public providers under an optional shared wall-clock deadline.

        ``deadline`` is a :func:`time.monotonic` timestamp. When omitted the
        pipeline falls back to the runner's per-provider timeout only. Callers
        that invoke this from a Tool must pass the Tool deadline so one slow
        provider cannot consume the whole Tool budget. ``freshness`` is the
        recency window (``any``/``day``/``week``/``month``) mapped onto each
        provider's native filter.

        ``allow_egress=False`` serves only from this process's cache. A miss is
        reported as ``provider="cache-miss"`` and is deliberately **not**
        cached: it is the absence of a result, and storing it would make a later
        egress-permitted run answer from a hole instead of searching.
        """
        cache_key = (query.strip().lower(), top_k, freshness)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return replace(cached, cache_hit=True)

        if not allow_egress:
            return RetrievalSearchResponse(
                query=query, provider="cache-miss", results=[], telemetry=[],
                cache_hit=False,
            )

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
        # An outage is not a result, so it must not be cached: storing it would
        # make every later run -- including a `live` one -- serve the failure
        # instead of retrying, and would let `cached` mode present a transient
        # network problem as a settled answer. A run where every provider
        # answered and genuinely matched nothing *is* worth caching.
        outage = not execution.results and any(
            item.status in {"error", "timeout"} for item in execution.telemetry
        )
        if not outage:
            self._cache[cache_key] = response
        return response
