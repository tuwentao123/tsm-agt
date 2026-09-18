from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class RetrievalResult:
    title: str
    url: str
    snippet: str
    provider: str
    source: str = "web"
    content_type: str = "search_result"
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RetrievalProviderTelemetry:
    provider: str
    status: str
    latency_ms: int
    result_count: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RetrievalSearchResponse:
    query: str
    results: list[RetrievalResult]
    provider: str
    telemetry: list[RetrievalProviderTelemetry]
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "result_count": len(self.results),
            "results": [result.to_dict() for result in self.results],
            "telemetry": [item.to_dict() for item in self.telemetry],
            "cache_hit": self.cache_hit,
        }
