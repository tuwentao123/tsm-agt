from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import URLError


#: Freshness windows accepted by ``web.search`` and mapped onto provider params.
FRESHNESS_WINDOWS = {
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
}
FRESHNESS_VALUES = ("any",) + tuple(FRESHNESS_WINDOWS)


def normalize_published_at(value: object) -> str | None:
    """Normalize a provider's publish date into an ISO-8601 UTC string.

    Accepts RFC-822/2822 (RSS ``pubDate``), ISO-8601, or ``datetime``. Returns
    ``None`` when the value is missing or unparseable so callers never invent a
    timestamp.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            moment = None
        if moment is None:
            try:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def published_at_datetime(value: str | None) -> datetime | None:
    """Parse a normalized ``published_at`` back into an aware datetime."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def freshness_cutoff(
    freshness: str, *, now: datetime | None = None,
) -> datetime | None:
    """Return the oldest acceptable publish time for a freshness window."""
    window = FRESHNESS_WINDOWS.get(freshness)
    if window is None:
        return None
    return (now or datetime.now(timezone.utc)) - window


def recency_sort_key(result: "RetrievalResult") -> tuple[int, float]:
    """Sort dated results newest-first, leaving undated results last."""
    moment = published_at_datetime(result.published_at)
    if moment is None:
        return (1, 0.0)
    return (0, -moment.timestamp())


class ProviderTimeoutError(TimeoutError):
    """A provider exceeded the time budget the pipeline granted it.

    Kept distinct from generic provider failures so the runner can record an
    explicit ``timeout`` telemetry status instead of silently degrading a
    timed-out source to an empty result list.
    """

    def __init__(
        self, provider: str, timeout_seconds: float,
        cause: BaseException | None = None,
    ) -> None:
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.cause = cause
        super().__init__(
            f"{provider} provider timed out after {timeout_seconds:.1f}s"
        )


def is_timeout_error(error: BaseException) -> bool:
    """True when a provider failure was caused by a socket/URL time limit."""
    if isinstance(error, TimeoutError):
        return True
    if isinstance(error, URLError):
        return isinstance(error.reason, TimeoutError)
    return False


@dataclass(slots=True)
class RetrievalResult:
    title: str
    url: str
    snippet: str
    provider: str
    source: str = "web"
    content_type: str = "search_result"
    #: When the source published the item. ``None`` when the provider exposes no
    #: publish date; never derived from the fetch time, so a caller can tell the
    #: difference between "old article" and "just fetched".
    published_at: str | None = None
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
