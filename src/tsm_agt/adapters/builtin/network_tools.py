"""Minimal production-style network tools with single responsibilities."""

from __future__ import annotations

import asyncio
import hashlib
import html
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    ModelToolContract,
    ToolCall,
    ToolEffect,
    ToolFactCapability,
    ToolIdempotency,
    ToolInvocationContext,
    ToolFactDescriptor,
    ToolResult,
    ToolResultAuthority,
    ToolResultMaterialization,
    ToolRisk,
    ToolSpec,
    WebEgressMode,
)
from tsm_agt.retrieval.pipeline import RetrievalPipeline
from tsm_agt.retrieval.providers import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS, RetrievalProvider,
)
from tsm_agt.retrieval.schemas import (
    FRESHNESS_VALUES,
    ProviderTimeoutError,
    RetrievalResult,
    freshness_cutoff,
    is_timeout_error,
    normalize_published_at,
    published_at_datetime,
    recency_sort_key,
)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 tsm-agt/0.1"
)
_MAX_FETCH_CHARS = 20000
_MAX_SEARCH_BYTES = 200000
_MAX_SUMMARY_SENTENCES = 5

#: Wall-clock budget for one ``web.search`` when the caller supplies no Tool
#: deadline (for example direct adapter use in tests). Must stay below the
#: Runtime's default 30s Tool deadline so the pipeline can return telemetry
#: instead of being killed mid-flight.
_DEFAULT_SEARCH_DEADLINE_SECONDS = 25.0

#: Leave this much of the Tool deadline unused: the pipeline needs a moment to
#: turn the last provider timeout into a normal ToolResult before the Runtime's
#: own hard timeout fires.
_SEARCH_DEADLINE_RESERVE_SECONDS = 0.5

#: Google News RSS recency operator. The endpoint has no date parameter; this
#: is the only supported freshness control.
_GOOGLE_NEWS_WHEN = {"day": "1d", "week": "7d", "month": "30d"}

#: Tavily only accepts a day count for its ``news`` topic.
_TAVILY_NEWS_DAYS = {"day": 1, "week": 7, "month": 30}


def _apply_freshness(
    results: list[RetrievalResult], freshness: str, top_k: int,
) -> list[RetrievalResult]:
    """Order dated results newest-first and drop clearly stale ones.

    Undated results are kept (we cannot prove they are old) but sort last, so a
    source that exposes no publish time can never outrank a fresh dated item.
    """
    cutoff = freshness_cutoff(freshness)
    if cutoff is not None:
        results = [
            result for result in results
            if (moment := published_at_datetime(result.published_at)) is None
            or moment >= cutoff
        ]
    results.sort(key=recency_sort_key)
    return results[:top_k]


_MAX_FETCH_BYTES = 20000
# Wire limit and text limit are different budgets: a readable article is easily
# ten times its extracted text once markup is counted. Reading more bytes than
# the text budget keeps the fetch bounded without rejecting ordinary documents.
_MIN_RENDERABLE_TEXT_CHARS = 200
_RENDER_TIMEOUT_SECONDS = 20.0
_MAX_FETCH_RESPONSE_BYTES = 2_000_000
_MAX_DECOMPRESSED_FETCH_BYTES = _MAX_FETCH_RESPONSE_BYTES
_MAX_STORED_TEXT_CHARS = 2_000_000
_MAX_WEB_PREVIEW_CHARS = 15_000
_MAX_FETCH_REDIRECTS = 3
_ALLOWED_FETCH_CONTENT_TYPES = frozenset({
    "text/html", "text/markdown", "text/plain", "text/x-markdown",
})
_MAX_DOCUMENT_CACHE_ENTRIES = 32
_MAX_DOCUMENT_CACHE_BYTES = 64_000_000
_MAX_REFERENCE_CACHE_ENTRIES = 128
_DOCUMENT_CACHE_TTL_SECONDS = 600.0
_MAX_FIND_MATCHES = 20
_MAX_FIND_PATTERN_CHARS = 512
_MAX_FIND_CONTEXT_CHARS = 240
_GITHUB_REPOSITORY_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_GITHUB_REF_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_GITHUB_SHA = re.compile(r"^[0-9a-f]{40,64}$", re.IGNORECASE)


def _search_outage_failure(
    call_id: str, data: Mapping[str, Any], mode: str,
) -> ToolResult | None:
    """Turn a total search failure into ``ok=False``.

    ``ok=True`` with an empty list lets a model read "no provider answered" as
    "the fact does not exist". The reference implementation reports that case as ``success: false``,
    and the mapping onto this Runtime is ``ok=False``. Three outcomes are kept
    apart:

    * ``cache-miss`` -- cached-only mode held nothing; reaching the network is
      the caller's explicit choice.
    * every provider errored or timed out -- an outage, and retryable.
    * every provider answered and matched nothing -- a genuine "no results",
      which stays a success so an empty answer is still allowed to mean empty.
    """

    if data.get("provider") == "cache-miss":
        return ToolResult(
            call_id, False, error_code="SEARCH_NOT_CACHED",
            message=(
                "web search is in cached-only mode "
                f"(TSM_AGT_SEARCH_MODE={mode}) and this query is not cached; "
                "set TSM_AGT_SEARCH_MODE=live to search the public web"
            ),
            data=data,
        )
    if data.get("results"):
        return None
    failed = sorted(
        str(item.get("provider"))
        for item in data.get("telemetry", ())
        if item.get("status") in {"error", "timeout"}
    )
    if not failed:
        return None
    return ToolResult(
        call_id, False, error_code="SEARCH_UNAVAILABLE",
        message=(
            "no search provider returned results; failed: " + ", ".join(failed)
        ),
        retryable=True, data=data,
    )


class _NoSearchResults(RuntimeError):
    """The provider was reachable but returned no usable public results."""


class _FetchFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _FetchTarget:
    """One validated hop: the URL to request and the address to connect to."""

    url: str
    pinned_address: str | None


@dataclass(frozen=True, slots=True)
class _WebDocument:
    """One bounded document that only its Task/turn scope may read."""

    scope: tuple[str, str]
    document_ref: str
    canonical_url: str
    requested_url: str
    content_type: str
    content: str
    content_hash: str
    truncated: bool
    provenance: Mapping[str, Any]
    stored_at: float

    @property
    def size_bytes(self) -> int:
        return len(self.content.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class _WebReference:
    """A Task/turn scoped reference to one search-result URL."""

    scope: tuple[str, str]
    ref_id: str
    url: str
    stored_at: float


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """Bounded response returned by the isolated web fetch transport.

    ``truncated`` means the remote document was larger than the byte budget and
    the body holds only its beginning. Oversize is reported, not failed: a long
    document is still readable evidence as long as the caller is told that the
    tail is missing.
    """

    status_code: int
    url: str
    headers: Mapping[str, str]
    body: bytes
    truncated: bool = False

    def header(self, name: str) -> str:
        normalized = name.casefold()
        return next((
            value for key, value in self.headers.items()
            if key.casefold() == normalized
        ), "")


class WebFetchTransport(Protocol):
    """Synchronous, injectable transport used only by ``web.fetch_markdown``.

    ``pinned_address`` is the already-validated destination address. A transport
    that receives one must connect to exactly that address and must not resolve
    the hostname again; a transport that cannot honour a pin must fail loudly
    rather than silently reconnect by name.
    """

    def fetch(
        self, url: str, *, headers: Mapping[str, str],
        timeout_seconds: float, max_bytes: int,
        pinned_address: str | None = None,
    ) -> FetchResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    """Return redirect responses to the caller for per-hop validation."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> Request | None:
        return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connect to a validated address while keeping the requested Host."""

    def __init__(
        self, host: str, port: int, *, pinned_address: str, timeout: float,
    ) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_address = pinned_address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a validated address; certificate and SNI stay hostname-based."""

    def __init__(
        self, host: str, port: int, *, pinned_address: str, timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_address = pinned_address

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout
        )
        # server_hostname keeps SNI and certificate verification bound to the
        # name the user asked for, even though the socket went to a pinned IP.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class PinnedAddressWebFetchTransport:
    """DIRECT-mode transport: one resolution, then connect to that address."""

    def __init__(self, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl_context or ssl.create_default_context()

    def fetch(
        self, url: str, *, headers: Mapping[str, str],
        timeout_seconds: float, max_bytes: int,
        pinned_address: str | None = None,
    ) -> FetchResponse:
        if not pinned_address:
            raise _FetchFailure(
                "EGRESS_PIN_REQUIRED",
                "DIRECT egress requires a validated destination address",
            )
        parsed = urlsplit(url)
        host = parsed.hostname
        if not host:
            raise _FetchFailure("URL_NOT_ALLOWED", "url has no host")
        secure = parsed.scheme == "https"
        port = parsed.port or (443 if secure else 80)
        connection: http.client.HTTPConnection = (
            _PinnedHTTPSConnection(
                host, port, pinned_address=pinned_address,
                timeout=timeout_seconds, context=self._ssl_context,
            )
            if secure else
            _PinnedHTTPConnection(
                host, port, pinned_address=pinned_address,
                timeout=timeout_seconds,
            )
        )
        try:
            target = parsed.path or "/"
            if parsed.query:
                target = f"{target}?{parsed.query}"
            connection.request("GET", target, headers=dict(headers))
            response = connection.getresponse()
            body = response.read(max_bytes + 1)
            truncated = len(body) > max_bytes
            return FetchResponse(
                status_code=int(response.status), url=url,
                headers={
                    str(key): str(value) for key, value in response.getheaders()
                },
                body=body[:max_bytes], truncated=truncated,
            )
        finally:
            connection.close()


class UrllibWebFetchTransport:
    """DELEGATED-mode transport: resolution and egress policy live outside."""

    def fetch(
        self, url: str, *, headers: Mapping[str, str],
        timeout_seconds: float, max_bytes: int,
        pinned_address: str | None = None,
    ) -> FetchResponse:
        if pinned_address:
            raise _FetchFailure(
                "EGRESS_PIN_UNSUPPORTED",
                "this transport cannot honour a pinned address; it must only "
                "be used when egress policy is delegated",
            )
        request = Request(url, headers=dict(headers), method="GET")
        response: Any = None
        try:
            try:
                response = build_opener(_NoRedirectHandler()).open(
                    request, timeout=timeout_seconds
                )
            except HTTPError as error:
                response = error
            body = response.read(max_bytes + 1)
            truncated = len(body) > max_bytes
            return FetchResponse(
                status_code=int(getattr(response, "status", response.getcode())),
                url=str(response.geturl()),
                headers={str(key): str(value) for key, value in response.headers.items()},
                body=body[:max_bytes], truncated=truncated,
            )
        finally:
            if response is not None:
                response.close()


AddressResolver = Callable[[str, int], Sequence[str]]


def _resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    addresses = {
        info[4][0] for info in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    }
    if not addresses:
        raise _FetchFailure("URL_NOT_ALLOWED", "URL host did not resolve")
    return tuple(addresses)


def _validate_fetch_url(
    value: str, resolver: AddressResolver, *, previous_scheme: str | None = None,
    mode: WebEgressMode = WebEgressMode.DIRECT,
) -> _FetchTarget:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _FetchFailure(
            "URL_NOT_ALLOWED", "url must be an absolute http or https URL"
        )
    if parsed.username is not None or parsed.password is not None:
        raise _FetchFailure("URL_NOT_ALLOWED", "url credentials are not allowed")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise _FetchFailure("URL_NOT_ALLOWED", "url port is invalid") from error
    if previous_scheme == "https" and parsed.scheme != "https":
        raise _FetchFailure(
            "REDIRECT_NOT_ALLOWED", "redirects may not downgrade HTTPS to HTTP"
        )
    if mode is WebEgressMode.DELEGATED:
        # Resolving here would classify the tunnel, not the destination. The
        # allow/deny decision belongs to the proxy or network policy that owns
        # egress, so no address is pinned and no address is judged.
        return _FetchTarget(value, None)
    try:
        addresses = resolver(parsed.hostname, port)
    except _FetchFailure:
        raise
    except (OSError, ValueError) as error:
        raise _FetchFailure(
            "URL_NOT_ALLOWED", "url host could not be resolved safely"
        ) from error
    if not addresses:
        raise _FetchFailure("URL_NOT_ALLOWED", "url host did not resolve")
    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError as error:
            raise _FetchFailure(
                "URL_NOT_ALLOWED", "url resolver returned an invalid address"
            ) from error
        if not resolved.is_global:
            raise _FetchFailure(
                "URL_NOT_ALLOWED", "url must resolve only to public addresses"
            )
    # Pin the address that was just validated. Reconnecting by hostname would
    # allow a second resolution to return an internal address instead.
    return _FetchTarget(value, addresses[0])


class _HTMLToTextParser(HTMLParser):
    _HIDDEN_TAGS = frozenset({"script", "style", "noscript", "template"})
    _BLOCK_TAGS = frozenset({
        "article", "aside", "blockquote", "div", "h1", "h2", "h3", "h4",
        "h5", "h6", "main", "p", "pre", "section",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self._HIDDEN_TAGS:
            self._hidden_depth += 1
            return
        if self._hidden_depth:
            return
        if tag in self._BLOCK_TAGS or tag == "br":
            self._chunks.append("\n")
        elif tag == "li":
            self._chunks.append("\n- ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._HIDDEN_TAGS:
            self._hidden_depth = max(0, self._hidden_depth - 1)
            return
        if not self._hidden_depth and (tag in self._BLOCK_TAGS or tag == "li"):
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth and data:
            self._chunks.append(data)

    def text(self) -> str:
        text = "".join(self._chunks)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class _DuckDuckGoResultsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._current_href: str | None = None
        self._current_title: str = ""
        self._current_snippet: str = ""
        self._capture_title = False
        self._capture_snippet = False
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        classes = attr_map.get("class", "") or ""

        if tag == "a":
            href = attr_map.get("href")
            if "result__a" in classes and href:
                self._current_href = href
                self._capture_title = True
                self._text_parts = []
            return

        if (
            tag in {"a", "div", "span"}
            and "result__snippet" in classes
            and self._current_href
        ):
            self._capture_snippet = True
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_title or self._capture_snippet:
            cleaned = data.strip()
            if cleaned:
                self._text_parts.append(cleaned)

    def handle_endtag(self, tag: str) -> None:
        if self._capture_title and tag == "a":
            self._current_title = re.sub(
                r"\s+", " ", " ".join(self._text_parts)
            ).strip()
            self._capture_title = False
            self._text_parts = []
            return

        if self._capture_snippet and tag in {"a", "div", "span"}:
            self._current_snippet = re.sub(
                r"\s+", " ", " ".join(self._text_parts)
            ).strip()
            self._capture_snippet = False
            self._text_parts = []
            return

        if tag != "article" or not self._current_href:
            return

        if self._current_title:
            self.results.append(
                {
                    "title": self._current_title,
                    "url": html.unescape(self._current_href),
                    "snippet": self._current_snippet or self._current_title,
                }
            )

        self._current_href = None
        self._current_title = ""
        self._current_snippet = ""
        self._capture_title = False
        self._capture_snippet = False
        self._text_parts = []


class _DuckDuckGoInstantProvider:
    name = "duckduckgo-instant-answer"

    def __init__(self, tools: NetworkToolProvider) -> None:
        self._tools = tools

    def search(
        self, query: str, top_k: int, *,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        freshness: str = "any",
    ) -> list[RetrievalResult]:
        try:
            payload = self._tools._load_json(
                "https://api.duckduckgo.com/?q="
                f"{quote_plus(query)}&format=json&no_redirect=1&no_html=1",
                timeout_seconds=timeout_seconds,
            )
        except (
            HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError
        ) as error:
            if is_timeout_error(error):
                raise ProviderTimeoutError(
                    self.name, timeout_seconds, error
                ) from error
            return []

        results: list[RetrievalResult] = []
        for item in payload.get("RelatedTopics", []):
            if len(results) >= top_k:
                break
            if not isinstance(item, Mapping):
                continue
            nested = item.get("Topics") if "Topics" in item else [item]
            for candidate in nested:
                if not isinstance(candidate, Mapping):
                    continue
                parsed = self._tools._parse_search_result(candidate)
                if parsed is None:
                    continue
                results.append(
                    RetrievalResult(
                        title=parsed["title"],
                        url=parsed["url"],
                        snippet=parsed["snippet"],
                        provider=self.name,
                    )
                )
                if len(results) >= top_k:
                    break
        return results


class _DuckDuckGoHtmlProvider:
    name = "duckduckgo-html"

    def __init__(self, tools: "NetworkToolProvider") -> None:
        self._tools = tools

    def search(
        self, query: str, top_k: int, *,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        freshness: str = "any",
    ) -> list[RetrievalResult]:
        return [
            RetrievalResult(
                title=item["title"],
                url=item["url"],
                snippet=item["snippet"],
                provider=self.name,
            )
            for item in self._tools._fallback_html_search(
                query, top_k, timeout_seconds=timeout_seconds
            )
        ]


class _RssNewsProvider:
    name = "rss-news"

    def __init__(self, tools: "NetworkToolProvider") -> None:
        self._tools = tools

    def search(
        self, query: str, top_k: int, *,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        freshness: str = "any",
    ) -> list[RetrievalResult]:
        # Google News ranks by relevance, not recency: a query containing
        # "today"/"latest" tends to surface evergreen round-ups. The `when:`
        # operator is the only recency control this endpoint exposes.
        effective_query = query
        if freshness in _GOOGLE_NEWS_WHEN:
            effective_query = f"{query} when:{_GOOGLE_NEWS_WHEN[freshness]}"
        rss_query = quote_plus(effective_query)
        url = (
            "https://news.google.com/rss/search?q="
            f"{rss_query}&hl=en-US&gl=US&ceid=US:en"
        )
        request = Request(url, headers={"User-Agent": _USER_AGENT})

        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read(_MAX_SEARCH_BYTES).decode(
                    "utf-8", errors="replace"
                )
            root = ET.fromstring(payload)
        except (HTTPError, URLError, TimeoutError, OSError, ET.ParseError) as error:
            if is_timeout_error(error):
                raise ProviderTimeoutError(
                    self.name, timeout_seconds, error
                ) from error
            return []
        results: list[RetrievalResult] = []

        for item in root.findall("./channel/item"):
            title = item.findtext("title") or "RSS result"
            link = item.findtext("link") or ""
            description = item.findtext("description") or ""
            description = re.sub(r"<[^>]+>", " ", description)

            if not link.strip():
                continue

            snippet = html.unescape(description.strip())
            source_name = item.findtext("source") or ""
            if source_name.strip():
                # Google wraps every link in an opaque redirect, so name the
                # real publisher in the snippet the model can actually cite.
                snippet = f"[{source_name.strip()}] {snippet}".strip()

            results.append(
                RetrievalResult(
                    title=html.unescape(title.strip()),
                    url=link.strip(),
                    snippet=snippet,
                    provider=self.name,
                    source="news",
                    content_type="rss",
                    published_at=normalize_published_at(
                        item.findtext("pubDate")
                    ),
                )
            )

        return _apply_freshness(results, freshness, top_k)


class _RssFeedProvider:
    """Shared RSS reader for keyword feeds that need no API key."""

    name = "rss-feed"
    feed_template = ""
    source = "news"

    def __init__(self, tools: "NetworkToolProvider") -> None:
        self._tools = tools

    def search(
        self, query: str, top_k: int, *,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        freshness: str = "any",
    ) -> list[RetrievalResult]:
        url = self.feed_template.format(query=quote_plus(query))
        request = Request(url, headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        })
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read(_MAX_SEARCH_BYTES).decode(
                    "utf-8", errors="replace"
                )
            root = ET.fromstring(payload)
        except (HTTPError, URLError, TimeoutError, OSError, ET.ParseError) as error:
            if is_timeout_error(error):
                raise ProviderTimeoutError(
                    self.name, timeout_seconds, error
                ) from error
            return []

        results: list[RetrievalResult] = []
        for item in root.findall("./channel/item"):
            link = (item.findtext("link") or "").strip()
            if not link:
                continue
            description = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
            results.append(RetrievalResult(
                title=html.unescape((item.findtext("title") or "RSS result").strip()),
                url=link,
                snippet=html.unescape(re.sub(r"\s+", " ", description).strip()),
                provider=self.name,
                source=self.source,
                content_type="rss",
                published_at=normalize_published_at(item.findtext("pubDate")),
            ))
        return _apply_freshness(results, freshness, top_k)


class _BingNewsRssProvider(_RssFeedProvider):
    """Bing News keyword feed; independent of Google News availability."""

    name = "bing-news-rss"
    feed_template = "https://www.bing.com/news/search?format=RSS&q={query}"


class _DuckDuckGoLiteProvider:
    """DuckDuckGo's lite endpoint still serves parsable result links."""

    name = "duckduckgo-lite"
    _endpoint = "https://lite.duckduckgo.com/lite/"

    def __init__(self, tools: "NetworkToolProvider") -> None:
        self._tools = tools

    def search(
        self, query: str, top_k: int, *,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        freshness: str = "any",
    ) -> list[RetrievalResult]:
        request = Request(
            f"{self._endpoint}?q={quote_plus(query)}",
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read(_MAX_SEARCH_BYTES).decode(
                    "utf-8", errors="replace"
                )
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            if is_timeout_error(error):
                raise ProviderTimeoutError(
                    self.name, timeout_seconds, error
                ) from error
            return []

        parser = _DuckDuckGoLiteParser()
        parser.feed(payload)
        results: list[RetrievalResult] = []
        for item in parser.results:
            if len(results) >= top_k:
                break
            url = self._tools._normalize_search_url(item["url"])
            if not url:
                continue
            results.append(RetrievalResult(
                title=item["title"],
                url=url,
                snippet=item["snippet"],
                provider=self.name,
                source="web",
                content_type="html",
            ))
        return results


class _DuckDuckGoLiteParser(HTMLParser):
    """Extract result rows from the lite endpoint's table layout."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._capture_title = False
        self._capture_snippet = False
        self._parts: list[str] = []
        self._href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        classes = attr_map.get("class") or ""
        if tag == "a" and "result-link" in classes:
            self._href = attr_map.get("href") or ""
            self._capture_title = True
            self._parts = []
        elif tag == "td" and "result-snippet" in classes:
            self._capture_snippet = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_title or self._capture_snippet:
            cleaned = data.strip()
            if cleaned:
                self._parts.append(cleaned)

    def handle_endtag(self, tag: str) -> None:
        text = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
        if self._capture_title and tag == "a":
            self._capture_title = False
            self._parts = []
            if text and self._href:
                self.results.append(
                    {"title": text, "url": self._href, "snippet": ""}
                )
            self._href = ""
        elif self._capture_snippet and tag == "td":
            self._capture_snippet = False
            self._parts = []
            if text and self.results:
                self.results[-1]["snippet"] = text


class _WikipediaProvider:
    """Encyclopedic fallback so factual lookups still return a citation."""

    name = "wikipedia"

    def __init__(self, tools: "NetworkToolProvider") -> None:
        self._tools = tools

    def search(
        self, query: str, top_k: int, *,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        freshness: str = "any",
    ) -> list[RetrievalResult]:
        results: list[RetrievalResult] = []
        for language in ("zh", "en"):
            if results:
                break
            try:
                payload = self._tools._load_json(
                    f"https://{language}.wikipedia.org/w/api.php?action=query"
                    "&list=search&format=json&srlimit="
                    f"{top_k}&srsearch={quote_plus(query)}",
                    timeout_seconds=timeout_seconds,
                )
            except (
                HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError
            ) as error:
                if is_timeout_error(error):
                    raise ProviderTimeoutError(
                        self.name, timeout_seconds, error
                    ) from error
                continue
            hits = payload.get("query", {}).get("search", [])
            if not isinstance(hits, list):
                continue
            for hit in hits:
                if not isinstance(hit, Mapping):
                    continue
                title = str(hit.get("title", "")).strip()
                if not title:
                    continue
                results.append(RetrievalResult(
                    title=title,
                    url=(
                        f"https://{language}.wikipedia.org/wiki/"
                        f"{quote_plus(title.replace(' ', '_'))}"
                    ),
                    snippet=re.sub(
                        r"<[^>]+>", "", html.unescape(str(hit.get("snippet", "")))
                    ).strip(),
                    provider=self.name,
                    source="encyclopedia",
                    content_type="html",
                ))
                if len(results) >= top_k:
                    break
        return results


class _TavilyProvider:
    """Tavily search adapter (commercial, optional, opt-in).

    Tavily is purpose-built for agent consumption: it ranks results and returns
    a real ``published_date`` plus clean content. It supports an API key
    (``Authorization: Bearer``) and a free keyless mode
    (``X-Tavily-Access-Mode: keyless``). This provider is only added to the
    pipeline when the host explicitly enables it, because it sends the user's
    query to a third party.
    """

    name = "tavily"
    _endpoint = "https://api.tavily.com/search"

    def __init__(
        self, tools: "NetworkToolProvider", api_key: str = "",
        *, keyless: bool = False,
    ) -> None:
        self._tools = tools
        self._api_key = api_key
        self._keyless = keyless

    def search(
        self, query: str, top_k: int, *,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        freshness: str = "any",
    ) -> list[RetrievalResult]:
        topic = "news" if freshness in _TAVILY_NEWS_DAYS else "general"
        body: dict[str, Any] = {
            "query": query,
            "max_results": max(1, min(int(top_k), 20)),
            "search_depth": "basic",
            "topic": topic,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        if topic == "news":
            body["days"] = _TAVILY_NEWS_DAYS[freshness]
        headers = {
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        elif self._keyless:
            headers["X-Tavily-Access-Mode"] = "keyless"
        request = Request(
            self._endpoint, data=json.dumps(body).encode("utf-8"),
            headers=headers,
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(
                    response.read(_MAX_SEARCH_BYTES).decode("utf-8", "replace")
                )
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            if is_timeout_error(error):
                raise ProviderTimeoutError(
                    self.name, timeout_seconds, error
                ) from error
            return []
        if not isinstance(payload, Mapping):
            return []

        results: list[RetrievalResult] = []
        for item in payload.get("results") or []:
            if not isinstance(item, Mapping):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            results.append(RetrievalResult(
                title=str(item.get("title") or url).strip(),
                url=url,
                snippet=str(item.get("content") or "").strip(),
                provider=self.name,
                source="news" if topic == "news" else "web",
                content_type="search_result",
                published_at=normalize_published_at(item.get("published_date")),
            ))
            if len(results) >= top_k:
                break
        return _apply_freshness(results, freshness, top_k)


class NetworkToolProvider:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.network-tools",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({
            "web.search", "web.fetch_markdown", "web.open",
            "web.find_in_page", "web.read", "content.summarize",
            "source.github.resolve",
        }),
    )

    _tools = (
        ToolSpec(
            name="web.search",
            description=(
                "Search the public web and return lightweight search results "
                "with source URLs, and publish times when the source exposes "
                "one. Set freshness for time-sensitive requests such as "
                "'today's news'. This finds where something is; it does not read "
                "it. Use it to locate the repository a request names, then read "
                "that repository's source with the advertised GitHub source tools "
                "or core.run_command -- a snippet is a lead, not evidence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Public-web search query; required."},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Maximum leads to return; defaults to 5."},
                    "freshness": {
                        "type": "string",
                        "enum": list(FRESHNESS_VALUES),
                        "description": (
                            "Recency window: 'day' keeps only very recent items "
                            "where the source supports it. Defaults to 'any'."
                        ),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            requires_network=True,
            data_transmission="public search query",
            rollback="read-only public search request; no remote mutation",
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.EXTERNAL_SERVICE,
            model_contract=ModelToolContract(
                purpose="Find public-web documents that may answer a question.",
                use_when=("a current or external source must be located",),
                avoid_when=("you need to claim document details from a result snippet; open or fetch the relevant result first",),
                returns=("search-result leads with URLs/ref_ids and optional publish times", "no document content is materialized"),
            ),
        ),
        ToolSpec(
            name="web.fetch_markdown",
            description=(
                "Fetch one public HTTP(S) document with bounded redirects and "
                "return Markdown or safe visible HTML text -- a document page, not "
                "a repository's source tree. To read another project's source, use "
                "the advertised GitHub source tools or core.run_command instead."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "format": "uri", "description": "One public HTTP(S) document URL."}},
                "required": ["url"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            requires_network=True,
            data_transmission="user-selected public HTTP(S) URL",
            rollback="read-only bounded public document request; no remote mutation",
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.EXTERNAL_SERVICE,
            model_contract=ModelToolContract(
                purpose="Fetch and materialize one public document as bounded readable text.",
                use_when=("a known public URL needs to be read",),
                avoid_when=("you need a repository tree or source history; use GitHub source tools or a task command",),
                returns=("document content, canonical URL, content hash, and truncation state",),
            ),
        ),
        ToolSpec(
            name="web.open",
            description=(
                "Open one public HTTP(S) document from a current Task/turn "
                "search-result ref_id or an explicit URL. The fetched document "
                "is bounded and can then be searched with web.find_in_page."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ref_id": {"type": "string", "minLength": 1, "description": "Task/turn-scoped search lead to open."},
                    "url": {"type": "string", "format": "uri", "description": "One public HTTP(S) document URL when no ref_id is available."},
                },
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            requires_network=True,
            data_transmission="public search-result reference or user-selected public HTTP(S) URL",
            rollback="read-only bounded public document request; no remote mutation",
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.EXTERNAL_SERVICE,
            model_contract=ModelToolContract(
                purpose="Open one public document from a search lead or explicit URL.",
                use_when=("a search lead needs source content",),
                avoid_when=("you only need to locate candidates; use web.search",),
                returns=("a bounded document preview plus document_ref for follow-up reads/searches", "preview truncation means the full document was not shown"),
            ),
        ),
        ToolSpec(
            name="web.find_in_page",
            description=(
                "Find plain-text matches only in a document_ref opened during "
                "the current Task and turn; this tool never makes a network request."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "document_ref": {"type": "string", "minLength": 1, "description": "Task/turn-scoped ref returned by web.open or fetch."},
                    "pattern": {"type": "string", "minLength": 1, "description": "Literal text to locate within that document."},
                    "max_matches": {"type": "integer", "minimum": 1, "maximum": _MAX_FIND_MATCHES, "description": "Maximum matches to return."},
                },
                "required": ["document_ref", "pattern"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            requires_network=False,
            data_transmission="none",
            rollback="reads an already-opened bounded document only; no remote mutation",
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.DERIVED,
            model_contract=ModelToolContract(
                purpose="Locate text offsets in one already-opened document.",
                use_when=("a long document needs a relevant passage located",),
                avoid_when=("you need surrounding or complete content; use web.read at the returned offset",),
                returns=("matching offsets and snippets only",),
            ),
        ),
        ToolSpec(
            name="web.read",
            description=(
                "Read a bounded character window from a document_ref already "
                "opened during the current Task and turn. Use it to read the "
                "middle of a long document after web.find_in_page reports an "
                "offset; it never makes a network request."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "document_ref": {"type": "string", "minLength": 1, "description": "Task/turn-scoped ref returned by web.open or fetch."},
                    "offset": {"type": "integer", "minimum": 0, "description": "Zero-based character offset; defaults to 0."},
                    "max_chars": {
                        "type": "integer", "minimum": 1,
                        "maximum": _MAX_WEB_PREVIEW_CHARS,
                        "description": "Maximum characters in this window.",
                    },
                },
                "required": ["document_ref"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            requires_network=False,
            data_transmission="none",
            rollback="reads an already-opened bounded document only; no remote mutation",
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.DERIVED,
            model_contract=ModelToolContract(
                purpose="Read one bounded content window from an already-opened document.",
                use_when=("a relevant offset is known or more of an opened document is needed",),
                avoid_when=("the document has not been opened in this turn",),
                returns=("a content window and next offset when more content remains",),
            ),
        ),
        ToolSpec(
            name="source.github.resolve",
            description=(
                "Entry point once you know which GitHub repository a request is "
                "about but not which file: give it the repository URL and it returns "
                "the repository identity plus the source tool and arguments to call "
                "next. Parses locally and never makes a network request."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "search_ref": {"type": "string", "minLength": 1},
                },
                # Keep the provider-facing schema a plain object.  Several
                # OpenAI-compatible endpoints reject a top-level oneOf even
                # though JSON Schema permits it; exact-one validation remains
                # enforced by _resolve_github_source below.
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            requires_network=False,
            data_transmission="none",
            rollback="local URL parsing only; no remote mutation or request",
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.DERIVED,
        ),
        ToolSpec(
            name="content.summarize",
            description="Create a short extractive summary from provided content.",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "max_sentences": {"type": "integer"},
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.DERIVED,
        ),
    )

    def __init__(
        self, *, enable_fetch: bool = True,
        fetch_transport: WebFetchTransport | None = None,
        address_resolver: AddressResolver | None = None,
        egress_mode: WebEgressMode | str = WebEgressMode.DIRECT,
        search_provider_timeout_seconds: float = (
            DEFAULT_PROVIDER_TIMEOUT_SECONDS
        ),
        search_fallback_deadline_seconds: float = (
            _DEFAULT_SEARCH_DEADLINE_SECONDS
        ),
        search_tavily_mode: str = "off",
        search_tavily_api_key: str = "",
        search_mode: str = "live",
        render_url: str = "",
        github_source_tools: bool = False,
        document_cache_max_entries: int = _MAX_DOCUMENT_CACHE_ENTRIES,
        document_cache_max_bytes: int = _MAX_DOCUMENT_CACHE_BYTES,
        reference_cache_max_entries: int = _MAX_REFERENCE_CACHE_ENTRIES,
        document_cache_ttl_seconds: float = _DOCUMENT_CACHE_TTL_SECONDS,
    ) -> None:
        """Expose public search and a bounded document fetch.

        The default DIRECT mode validates and pins the destination address
        itself. DELEGATED mode is for hosts where egress already goes through a
        proxy, VPN, or TUN device that owns the policy; see ``WebEgressMode``.

        ``search_provider_timeout_seconds`` caps a single public provider (each
        source fails soft), while the whole ``web.search`` stays inside the Tool
        deadline derived from the invocation context. The fallback deadline is
        used only when no context supplies one.

        ``search_tavily_mode`` is ``off`` (default), ``keyless``, or ``key``.
        Tavily is prepended as the primary source only when explicitly enabled,
        because it forwards the query to a third party.

        ``search_mode`` is ``live`` (default), ``cached``, or ``off`` and sits
        above every provider: ``live`` queries the public providers, ``cached``
        serves only what this process already holds and never opens a
        connection, and ``off`` refuses outright. The default departs from
        Codex's ``WebSearchMode::Cached`` on purpose: Codex's provider indexes
        the web server-side, so its cache is never empty, while this Runtime
        would ship a search tool that answers nothing. Hosts wanting the
        stricter posture set ``cached`` or ``off``.
        """
        if search_provider_timeout_seconds <= 0:
            raise ValueError("search_provider_timeout_seconds must be positive")
        if search_fallback_deadline_seconds <= 0:
            raise ValueError("search_fallback_deadline_seconds must be positive")
        if document_cache_max_entries <= 0:
            raise ValueError("document_cache_max_entries must be positive")
        if document_cache_max_bytes <= 0:
            raise ValueError("document_cache_max_bytes must be positive")
        if reference_cache_max_entries <= 0:
            raise ValueError("reference_cache_max_entries must be positive")
        if document_cache_ttl_seconds <= 0:
            raise ValueError("document_cache_ttl_seconds must be positive")
        mode = (search_tavily_mode or "off").strip().lower()
        if mode not in {"off", "keyless", "key"}:
            raise ValueError(
                "search_tavily_mode must be 'off', 'keyless', or 'key'"
            )
        if mode == "key" and not search_tavily_api_key.strip():
            raise ValueError(
                "search_tavily_mode='key' requires TSM_AGT_SEARCH_TAVILY_API_KEY"
            )
        self._enable_fetch = enable_fetch
        self._github_source_tools = github_source_tools
        search_scope = (search_mode or "cached").strip().lower()
        if search_scope not in {"off", "cached", "live"}:
            raise ValueError("search_mode must be 'off', 'cached', or 'live'")
        self._search_mode = search_scope
        #: Optional host-supplied service that returns rendered text for a URL.
        #: Empty means "no rendering available", which is the previous behavior.
        self._render_url = (render_url or "").strip()
        self._egress_mode = WebEgressMode(egress_mode)
        self._fetch_transport = fetch_transport or (
            PinnedAddressWebFetchTransport()
            if self._egress_mode is WebEgressMode.DIRECT
            else UrllibWebFetchTransport()
        )
        self._address_resolver = address_resolver or _resolve_public_addresses
        self._document_cache_max_entries = document_cache_max_entries
        self._document_cache_max_bytes = document_cache_max_bytes
        self._reference_cache_max_entries = reference_cache_max_entries
        self._document_cache_ttl_seconds = document_cache_ttl_seconds
        self._document_cache: OrderedDict[tuple[tuple[str, str], str], _WebDocument] = OrderedDict()
        self._reference_cache: OrderedDict[tuple[tuple[str, str], str], _WebReference] = OrderedDict()
        self._document_cache_bytes = 0
        self._cache_lock = threading.RLock()
        self._started = False
        self._search_fallback_deadline_seconds = (
            search_fallback_deadline_seconds
        )
        # Ordered by observed usefulness: an opt-in commercial source first,
        # then general web results, news feeds, and encyclopedic fallback. Each
        # provider fails soft so one blocked endpoint cannot disable search.
        providers: list[RetrievalProvider] = []
        if mode != "off":
            providers.append(_TavilyProvider(
                self, search_tavily_api_key.strip(),
                keyless=mode == "keyless",
            ))
        providers.extend([
            _DuckDuckGoLiteProvider(self),
            _DuckDuckGoHtmlProvider(self),
            _BingNewsRssProvider(self),
            _RssNewsProvider(self),
            _DuckDuckGoInstantProvider(self),
            _WikipediaProvider(self),
        ])
        self._pipeline = RetrievalPipeline(
            providers=providers,
            provider_timeout_seconds=search_provider_timeout_seconds,
        )

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "network tools ready" if self._started else "network tools not started",
        )

    async def stop(self, deadline: datetime) -> None:
        with self._cache_lock:
            self._document_cache.clear()
            self._reference_cache.clear()
            self._document_cache_bytes = 0
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        hidden = {
            "web.fetch_markdown", "web.open", "web.find_in_page", "web.read",
        } if not self._enable_fetch else set()
        if not self._github_source_tools:
            # This tool only routes a GitHub URL into the GitHub source adapter's
            # tools. Advertising it while that adapter is absent hands the model
            # targets it cannot call, so it stays out of the toolset entirely.
            hidden.add("source.github.resolve")
        return tuple(tool for tool in self._tools if tool.name not in hidden)

    async def invoke(self, call: ToolCall, context: ToolInvocationContext) -> ToolResult:
        if not self._started:
            raise RuntimeError("network tools are not started")
        try:
            scope = self._scope_for(context)
            if call.name == "web.search":
                if self._search_mode == "off":
                    return ToolResult(
                        call.call_id, False, error_code="SEARCH_DISABLED",
                        message="web search is disabled (TSM_AGT_SEARCH_MODE=off)",
                    )
                data = await asyncio.to_thread(
                    self._web_search, call.arguments,
                    self._search_deadline(context),
                )
                data = self._attach_search_refs(data, scope)
                failure = _search_outage_failure(
                    call.call_id, data, self._search_mode,
                )
                if failure is not None:
                    return failure
                fact_descriptor = self._fact_descriptor(
                    scope,
                    data,
                    authority="external_web_search",
                    materialization=ToolResultMaterialization.POINTER,
                    capabilities=(ToolFactCapability.LOCATE_RESOURCE,),
                )
            elif call.name == "web.fetch_markdown":
                if not self._enable_fetch:
                    return ToolResult(
                        call.call_id, False, error_code="CAPABILITY_DISABLED",
                        message="web.fetch_markdown is disabled for this provider",
                    )
                data = await asyncio.to_thread(
                    self._fetch_document, call.arguments, scope
                )
                fact_descriptor = self._fact_descriptor(
                    scope,
                    data,
                    authority="external_web_document",
                    materialization=ToolResultMaterialization.CONTENT,
                    capabilities=(ToolFactCapability.DESCRIBE_CONTENT,),
                )
            elif call.name == "web.open":
                if not self._enable_fetch:
                    return ToolResult(
                        call.call_id, False, error_code="CAPABILITY_DISABLED",
                        message="web.open is disabled for this provider",
                    )
                data = await asyncio.to_thread(
                    self._open_document, call.arguments, scope
                )
                fact_descriptor = self._fact_descriptor(
                    scope,
                    data,
                    authority="external_web_document",
                    materialization=ToolResultMaterialization.CONTENT,
                    capabilities=(ToolFactCapability.DESCRIBE_CONTENT,),
                )
            elif call.name == "web.find_in_page":
                if not self._enable_fetch:
                    return ToolResult(
                        call.call_id, False, error_code="CAPABILITY_DISABLED",
                        message="web.find_in_page is disabled for this provider",
                    )
                data = await asyncio.to_thread(
                    self._find_in_page, call.arguments, scope
                )
                fact_descriptor = self._fact_descriptor(
                    scope,
                    data,
                    authority="external_web_document",
                    materialization=ToolResultMaterialization.LOCATOR,
                    capabilities=(ToolFactCapability.LOCATE_RESOURCE,),
                )
            elif call.name == "web.read":
                if not self._enable_fetch:
                    return ToolResult(
                        call.call_id, False, error_code="CAPABILITY_DISABLED",
                        message="web.read is disabled for this provider",
                    )
                data = await asyncio.to_thread(
                    self._read_document, call.arguments, scope
                )
                fact_descriptor = self._fact_descriptor(
                    scope,
                    data,
                    authority="external_web_document",
                    materialization=ToolResultMaterialization.CONTENT,
                    capabilities=(ToolFactCapability.DESCRIBE_CONTENT,),
                )
            elif call.name == "source.github.resolve":
                data = self._resolve_github_source(call.arguments, scope)
                fact_descriptor = self._github_fact_descriptor(scope, data)
            elif call.name == "content.summarize":
                data = self._summarize(call.arguments)
                fact_descriptor = ToolFactDescriptor(
                    authority="derived_summary",
                    scope=(f"task:{scope[0]}", f"turn:{scope[1]}"),
                    content_hash="sha256:" + hashlib.sha256(
                        str(call.arguments["content"]).encode("utf-8")
                    ).hexdigest(),
                    materialization=ToolResultMaterialization.DERIVED,
                    capabilities=(ToolFactCapability.DESCRIBE_DERIVED,),
                )
            else:
                return ToolResult(call.call_id, False, error_code="NOT_FOUND", message=f"unknown network tool: {call.name}")
            return ToolResult(
                call.call_id, True, data=data, truncated=bool(data.get("truncated", False)),
                meta={"untrusted_data": True}, fact_descriptor=fact_descriptor,
            )
        except _FetchFailure as error:
            return ToolResult(
                call.call_id, False, error_code=error.code, message=str(error),
                retryable=error.retryable,
            )
        except (TypeError, ValueError) as error:
            return ToolResult(call.call_id, False, error_code="INVALID_PARAM", message=str(error))
        except _NoSearchResults as error:
            return ToolResult(
                call.call_id, False, error_code="NO_RESULTS",
                message=str(error), retryable=False,
            )
        except (OSError, TimeoutError, URLError) as error:
            return ToolResult(call.call_id, False, error_code="NETWORK_ERROR", message=str(error), retryable=True)

    def _search_deadline(
        self, context: ToolInvocationContext | None,
    ) -> float:
        """Translate the Tool deadline into a ``time.monotonic`` budget.

        The Runtime wraps the Tool in its own hard timeout, but that timeout can
        only fail the whole call. Handing the remaining budget to the pipeline
        lets it stop asking slow providers and still return partial telemetry.
        """
        deadline = getattr(context, "deadline", None)
        if deadline is None:
            return time.monotonic() + self._search_fallback_deadline_seconds
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        remaining = (
            (deadline - datetime.now(timezone.utc)).total_seconds()
            - _SEARCH_DEADLINE_RESERVE_SECONDS
        )
        # A non-positive budget means the Runner skips every provider, which is
        # the honest outcome: the Tool deadline is already behind us.
        return time.monotonic() + max(remaining, 0.0)

    def _web_search(
        self, arguments: Mapping[str, Any], deadline: float | None = None,
    ) -> Mapping[str, Any]:
        query = self._required_string(arguments, "query")
        top_k = min(max(int(arguments.get("top_k", 5)), 1), 10)
        freshness = self._freshness(arguments)

        response = self._pipeline.search(
            query, top_k, deadline=deadline, freshness=freshness,
            allow_egress=self._search_mode == "live",
        )
        response_data = response.to_dict()
        if not response_data["results"]:
            response_data["message"] = (
                "public search providers returned no usable results"
            )
        return response_data

    def _attach_search_refs(
        self, response_data: Mapping[str, Any], scope: tuple[str, str],
    ) -> Mapping[str, Any]:
        data = dict(response_data)
        results = data.get("results")
        if not isinstance(results, list):
            return data
        scoped_results: list[dict[str, Any]] = []
        for result in results:
            item = dict(result)
            url = item.get("url")
            if isinstance(url, str) and url:
                item["ref_id"] = self._store_reference(scope, url)
            scoped_results.append(item)
        data["results"] = scoped_results
        return data

    @staticmethod
    def _freshness(arguments: Mapping[str, Any]) -> str:
        raw = arguments.get("freshness")
        if raw is None:
            return "any"
        value = str(raw).strip().lower()
        if value not in FRESHNESS_VALUES:
            raise ValueError(
                "freshness must be one of: " + ", ".join(FRESHNESS_VALUES)
            )
        return value

    @property
    def egress_mode(self) -> WebEgressMode:
        return self._egress_mode

    @staticmethod
    def _scope_for(context: ToolInvocationContext | None) -> tuple[str, str]:
        if context is None:
            return ("direct", "provider")
        return (context.task_id, context.turn_id)

    @staticmethod
    def _reference_id(scope: tuple[str, str], url: str) -> str:
        digest = hashlib.sha256(
            "\x00".join((*scope, url)).encode("utf-8")
        ).hexdigest()
        return f"search:{digest[:24]}"

    @staticmethod
    def _document_id(scope: tuple[str, str], canonical_url: str) -> str:
        digest = hashlib.sha256(
            "\x00".join((*scope, canonical_url)).encode("utf-8")
        ).hexdigest()
        return f"webdoc:{digest[:24]}"

    def _expire_cache(self, now: float) -> None:
        expired_documents = [
            key for key, document in self._document_cache.items()
            if now - document.stored_at > self._document_cache_ttl_seconds
        ]
        for key in expired_documents:
            document = self._document_cache.pop(key)
            self._document_cache_bytes -= document.size_bytes
        expired_references = [
            key for key, reference in self._reference_cache.items()
            if now - reference.stored_at > self._document_cache_ttl_seconds
        ]
        for key in expired_references:
            self._reference_cache.pop(key)

    def _store_reference(self, scope: tuple[str, str], url: str) -> str:
        now = time.monotonic()
        ref_id = self._reference_id(scope, url)
        key = (scope, ref_id)
        with self._cache_lock:
            self._expire_cache(now)
            self._reference_cache[key] = _WebReference(scope, ref_id, url, now)
            self._reference_cache.move_to_end(key)
            while len(self._reference_cache) > self._reference_cache_max_entries:
                self._reference_cache.popitem(last=False)
        return ref_id

    def _reference_url(self, scope: tuple[str, str], ref_id: str) -> str:
        with self._cache_lock:
            self._expire_cache(time.monotonic())
            reference = self._reference_cache.get((scope, ref_id))
            if reference is None:
                raise _FetchFailure(
                    "SEARCH_REF_NOT_FOUND",
                    "search result ref_id is unknown or expired for this Task and turn; search again",
                )
            self._reference_cache.move_to_end((scope, ref_id))
            return reference.url

    def _resolve_github_source(
        self, arguments: Mapping[str, Any], scope: tuple[str, str],
    ) -> Mapping[str, Any]:
        has_url = arguments.get("url") is not None
        has_search_ref = arguments.get("search_ref") is not None
        if has_url == has_search_ref:
            raise ValueError(
                "source.github.resolve requires exactly one of url or search_ref"
            )
        search_ref = None
        if has_search_ref:
            search_ref = self._required_string(arguments, "search_ref")
            url = self._reference_url(scope, search_ref)
            input_kind = "search_ref"
        else:
            url = self._required_string(arguments, "url")
            input_kind = "url"
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise _FetchFailure("INVALID_GITHUB_URL", "GitHub URL has an invalid port") from error
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold() != "github.com"
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise _FetchFailure(
                "INVALID_GITHUB_URL",
                "source.github.resolve accepts only HTTPS github.com URLs without auth, port, query, or fragment",
            )
        segments = parsed.path.split("/")[1:]
        if segments and not segments[-1]:
            segments.pop()
        if not segments or any(not segment for segment in segments):
            raise _FetchFailure("INVALID_GITHUB_URL", "GitHub URL has an invalid path")
        if any(
            unquote(segment) != segment
            or segment in {".", ".."}
            or "\\" in segment
            for segment in segments
        ):
            raise _FetchFailure(
                "INVALID_GITHUB_URL",
                "GitHub URL must not contain encoded or traversing path segments",
            )
        if len(segments) < 2 or any(
            _GITHUB_REPOSITORY_SEGMENT.fullmatch(segment) is None
            or segment in {".", ".."}
            for segment in segments[:2]
        ):
            raise _FetchFailure(
                "INVALID_GITHUB_URL", "GitHub URL has an invalid owner or repository"
            )
        repo = "/".join(segments[:2])
        if len(segments) == 2:
            kind = "repository"
            ref = None
            path = None
            number = None
            target_tool = "source.github.list_tree"
            target_arguments: dict[str, Any] = {"repo": repo, "ref": "HEAD"}
            canonical_url = f"https://github.com/{repo}"
        else:
            resource = segments[2]
            if resource in {"blob", "tree"}:
                if len(segments) < 4:
                    raise _FetchFailure(
                        "INVALID_GITHUB_URL",
                        f"GitHub {resource} URL must include a ref",
                    )
                if segments[3] == "refs" and len(segments) > 4:
                    raise _FetchFailure(
                        "AMBIGUOUS_GITHUB_REF_PATH",
                        "GitHub URL uses a slash ref, which cannot be resolved without a network request",
                    )
                ref = segments[3]
                if _GITHUB_REF_SEGMENT.fullmatch(ref) is None:
                    raise _FetchFailure(
                        "INVALID_GITHUB_URL", "GitHub URL has an invalid ref"
                    )
                if _GITHUB_SHA.fullmatch(ref):
                    ref = ref.lower()
                path_segments = segments[4:]
                if resource == "blob" and not path_segments:
                    raise _FetchFailure(
                        "INVALID_GITHUB_URL", "GitHub blob URL must include a file path"
                    )
                if any(
                    segment in {".", ".."} or "\\" in segment
                    for segment in path_segments
                ):
                    raise _FetchFailure(
                        "INVALID_GITHUB_URL", "GitHub URL has an invalid repository path"
                    )
                kind = resource
                path = "/".join(path_segments) or None
                number = None
                target_tool = (
                    "source.github.get_file"
                    if kind == "blob"
                    else "source.github.list_tree"
                )
                target_arguments = {"repo": repo, "ref": ref}
                if path is not None:
                    target_arguments["path"] = path
                canonical_url = f"https://github.com/{repo}/{kind}/{ref}"
                if path is not None:
                    canonical_url = f"{canonical_url}/{path}"
            elif resource in {"issues", "pull"} and len(segments) == 4:
                raw_number = segments[3]
                if not raw_number.isdecimal() or int(raw_number) < 1:
                    raise _FetchFailure(
                        "INVALID_GITHUB_URL", "GitHub issue or pull number must be positive"
                    )
                kind = "issue" if resource == "issues" else "pull"
                ref = None
                path = None
                number = int(raw_number)
                target_tool = (
                    "source.github.get_issue"
                    if kind == "issue"
                    else "source.github.get_pull_request"
                )
                target_arguments = {"repo": repo, "number": number}
                canonical_url = f"https://github.com/{repo}/{resource}/{number}"
            else:
                raise _FetchFailure(
                    "INVALID_GITHUB_URL", "GitHub URL kind is not supported"
                )
        ref_is_pinned = bool(ref and _GITHUB_SHA.fullmatch(ref))
        result = {
            "canonical_url": canonical_url,
            "repo": repo,
            "kind": kind,
            "ref": ref,
            "path": path,
            "number": number,
            "target_tool": target_tool,
            "target_arguments": target_arguments,
            "ref_is_pinned": ref_is_pinned,
            "provenance": {
                "source": "source.github.resolve",
                "input_kind": input_kind,
                "search_ref": search_ref,
                "network_egress": False,
            },
        }
        result["content_hash"] = "sha256:" + hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return result

    @staticmethod
    def _github_fact_descriptor(
        scope: tuple[str, str], data: Mapping[str, Any],
    ) -> ToolFactDescriptor:
        repo = str(data["repo"])
        ref = data.get("ref")
        fact_scope = [f"task:{scope[0]}", f"turn:{scope[1]}", f"repo:{repo}"]
        if isinstance(ref, str) and ref:
            fact_scope.append(f"ref:{ref}")
        return ToolFactDescriptor(
            authority="derived_github_url",
            scope=tuple(fact_scope),
            artifact_refs=(str(data["canonical_url"]),),
            content_hash=str(data["content_hash"]),
            materialization=ToolResultMaterialization.POINTER,
            capabilities=(ToolFactCapability.LOCATE_RESOURCE,),
        )

    def _store_document(
        self, scope: tuple[str, str], fetched: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        canonical_url = str(fetched["url"])
        content = str(fetched["content"])
        document_ref = self._document_id(scope, canonical_url)
        content_hash = "sha256:" + hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()
        fetched_at = datetime.now(timezone.utc).isoformat()
        provenance = {
            "source": "web.fetch_markdown",
            "requested_url": str(fetched["requested_url"]),
            "canonical_url": canonical_url,
            "fetched_at": fetched_at,
            "egress_mode": str(fetched["egress_mode"]),
            "redirect_count": int(fetched["redirect_count"]),
        }
        document = _WebDocument(
            scope=scope,
            document_ref=document_ref,
            canonical_url=canonical_url,
            requested_url=str(fetched["requested_url"]),
            content_type=str(fetched["content_type"]),
            content=content,
            content_hash=content_hash,
            truncated=bool(fetched["truncated"]),
            provenance=provenance,
            stored_at=time.monotonic(),
        )
        key = (scope, document_ref)
        with self._cache_lock:
            self._expire_cache(document.stored_at)
            replaced = self._document_cache.pop(key, None)
            if replaced is not None:
                self._document_cache_bytes -= replaced.size_bytes
            self._document_cache[key] = document
            self._document_cache_bytes += document.size_bytes
            while (
                len(self._document_cache) > self._document_cache_max_entries
                or self._document_cache_bytes > self._document_cache_max_bytes
            ):
                _, evicted = self._document_cache.popitem(last=False)
                self._document_cache_bytes -= evicted.size_bytes
        return {
            **dict(fetched),
            "content": self._document_preview(document),
            "canonical_url": canonical_url,
            "document_ref": document_ref,
            "content_hash": content_hash,
            "total_chars": len(content),
            "total_bytes": document.size_bytes,
            "preview_truncated": len(content) > _MAX_WEB_PREVIEW_CHARS,
            "document_truncated": document.truncated,
            "truncated": bool(fetched["truncated"]) or len(content) > _MAX_WEB_PREVIEW_CHARS,
            "fetched_at": fetched_at,
            "provenance": provenance,
        }

    @staticmethod
    def _document_preview(document: _WebDocument) -> str:
        content = document.content
        if len(content) <= _MAX_WEB_PREVIEW_CHARS:
            return content
        head_size = (_MAX_WEB_PREVIEW_CHARS * 3) // 4
        tail_size = _MAX_WEB_PREVIEW_CHARS - head_size
        head = content[:head_size]
        tail = content[-tail_size:]
        if (newline := head.rfind("\n")) > head_size // 2:
            head = head[:newline]
        if 0 <= (newline := tail.find("\n")) < tail_size // 2:
            tail = tail[newline + 1:]
        return (
            f"{head}\n\n[... middle omitted; use web.find_in_page with "
            f"document_ref={document.document_ref!r} to locate details in the "
            f"{len(content):,}-character document ...]\n\n{tail}"
        )

    def _fetch_document(
        self, arguments: Mapping[str, Any], scope: tuple[str, str],
    ) -> Mapping[str, Any]:
        return self._store_document(scope, self._fetch_markdown(arguments))

    def _open_document(
        self, arguments: Mapping[str, Any], scope: tuple[str, str],
    ) -> Mapping[str, Any]:
        url = arguments.get("url")
        ref_id = arguments.get("ref_id")
        if (url is None) == (ref_id is None):
            raise ValueError("web.open requires exactly one of url or ref_id")
        if ref_id is not None:
            if not isinstance(ref_id, str) or not ref_id.strip():
                raise ValueError("ref_id must be a non-empty string")
            reference = ref_id.strip()
            try:
                url = self._reference_url(scope, reference)
            except _FetchFailure:
                # Models routinely pass the document_ref returned by
                # web.fetch_markdown here. Re-opening that document is the
                # intent, and the reference cache is not the right home for it.
                with self._cache_lock:
                    document = self._document_cache.get((scope, reference))
                if document is None:
                    raise
                url = document.requested_url
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        return self._fetch_document({"url": url}, scope)

    def _find_in_page(
        self, arguments: Mapping[str, Any], scope: tuple[str, str],
    ) -> Mapping[str, Any]:
        document_ref = self._required_string(arguments, "document_ref")
        pattern = self._required_string(arguments, "pattern")
        if len(pattern) > _MAX_FIND_PATTERN_CHARS:
            raise ValueError(
                f"pattern must contain at most {_MAX_FIND_PATTERN_CHARS} characters"
            )
        max_matches = int(arguments.get("max_matches", 10))
        if not 1 <= max_matches <= _MAX_FIND_MATCHES:
            raise ValueError(f"max_matches must be between 1 and {_MAX_FIND_MATCHES}")
        key = (scope, document_ref)
        with self._cache_lock:
            self._expire_cache(time.monotonic())
            document = self._document_cache.get(key)
            if document is None:
                raise _FetchFailure(
                    "DOCUMENT_REF_NOT_FOUND",
                    "document_ref is unknown or expired for this Task and turn; open the document first",
                )
            self._document_cache.move_to_end(key)
        normalized_pattern = pattern.casefold()
        matches: list[dict[str, Any]] = []
        total = 0
        content_offset = 0
        for line_number, raw_line in enumerate(
            document.content.splitlines(keepends=True), start=1,
        ):
            line = raw_line.rstrip("\r\n")
            start = 0
            folded_line = line.casefold()
            while (index := folded_line.find(normalized_pattern, start)) != -1:
                total += 1
                if len(matches) < max_matches:
                    left = max(0, index - _MAX_FIND_CONTEXT_CHARS // 2)
                    right = min(
                        len(line), index + len(pattern) + _MAX_FIND_CONTEXT_CHARS // 2,
                    )
                    matches.append({
                        "ordinal": total,
                        "offset": content_offset + index,
                        "snippet": line[left:right],
                        "line": line_number,
                        "start": index,
                        "end": index + len(pattern),
                        "text": line[left:right],
                    })
                start = index + max(1, len(pattern))
            content_offset += len(raw_line)
        return {
            "document_ref": document.document_ref,
            "canonical_url": document.canonical_url,
            "content_hash": document.content_hash,
            "document_truncated": document.truncated,
            "provenance": dict(document.provenance),
            "pattern": pattern,
            "matches": matches,
            "match_count": total,
            "returned_count": len(matches),
            "truncated": total > len(matches),
        }

    def _read_document(
        self, arguments: Mapping[str, Any], scope: tuple[str, str],
    ) -> Mapping[str, Any]:
        """Return one bounded character window of an already-open document.

        The document cache is scoped to a single ``(task_id, turn_id)`` pair, so
        this reads only what the current turn already fetched: it never opens a
        socket, never falls back to a workspace file, and never re-authorizes a
        fetch that the current turn did not make.
        """
        document_ref = self._required_string(arguments, "document_ref")
        offset = int(arguments.get("offset", 0))
        if offset < 0:
            raise ValueError("offset must be zero or greater")
        max_chars = int(arguments.get("max_chars", _MAX_WEB_PREVIEW_CHARS))
        if not 1 <= max_chars <= _MAX_WEB_PREVIEW_CHARS:
            raise ValueError(
                f"max_chars must be between 1 and {_MAX_WEB_PREVIEW_CHARS}"
            )
        key = (scope, document_ref)
        with self._cache_lock:
            self._expire_cache(time.monotonic())
            document = self._document_cache.get(key)
            if document is None:
                raise _FetchFailure(
                    "DOCUMENT_REF_NOT_FOUND",
                    "document_ref is unknown or expired for this Task and turn; "
                    "open the document first",
                )
            self._document_cache.move_to_end(key)
        content = document.content
        total_chars = len(content)
        if total_chars and offset >= total_chars:
            raise ValueError(
                f"offset {offset} is beyond the {total_chars}-character document"
            )
        window = content[offset:offset + max_chars]
        next_offset = offset + len(window)
        return {
            "document_ref": document.document_ref,
            "canonical_url": document.canonical_url,
            "content_hash": document.content_hash,
            "offset": offset,
            "max_chars": max_chars,
            "content": window,
            "returned_chars": len(window),
            "total_chars": total_chars,
            "next_offset": next_offset if next_offset < total_chars else None,
            "document_truncated": document.truncated,
            "provenance": dict(document.provenance),
            "truncated": next_offset < total_chars,
        }

    @staticmethod
    def _fact_descriptor(
        scope: tuple[str, str], data: Mapping[str, Any], *,
        authority: str,
        materialization: ToolResultMaterialization,
        capabilities: tuple[ToolFactCapability, ...],
    ) -> ToolFactDescriptor:
        artifact_ref_values: list[str] = [
            str(value) for value in (
                data.get("document_ref"), data.get("canonical_url"),
            ) if isinstance(value, str) and value
        ]
        # A search response names potential documents but has not materialized
        # any of them. Preserve its stable per-turn ref IDs as leads without
        # letting result metadata masquerade as document content.
        results = data.get("results")
        if isinstance(results, list):
            for result in results:
                if not isinstance(result, Mapping):
                    continue
                for key in ("ref_id", "url"):
                    value = result.get(key)
                    if isinstance(value, str) and value:
                        artifact_ref_values.append(value)
        artifact_refs = tuple(dict.fromkeys(artifact_ref_values))
        observed_at = None
        provenance = data.get("provenance")
        if isinstance(provenance, Mapping):
            value = provenance.get("fetched_at")
            observed_at = str(value) if isinstance(value, str) and value else None
        content_hash = data.get("content_hash")
        return ToolFactDescriptor(
            authority=authority,
            scope=(f"task:{scope[0]}", f"turn:{scope[1]}"),
            artifact_refs=artifact_refs,
            observed_at=observed_at,
            content_hash=(
                str(content_hash)
                if isinstance(content_hash, str) and content_hash else None
            ),
            materialization=materialization,
            capabilities=capabilities,
        )

    def _fetch_markdown(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        requested_url = self._required_string(arguments, "url")
        target = _validate_fetch_url(
            requested_url, self._address_resolver, mode=self._egress_mode
        )
        current_url = target.url
        redirect_chain = [current_url]
        redirects = 0
        while True:
            response = self._fetch_transport.fetch(
                current_url,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/markdown, text/plain, text/html;q=0.9",
                    "Accept-Encoding": "gzip, deflate, identity",
                },
                timeout_seconds=15.0,
                max_bytes=_MAX_FETCH_RESPONSE_BYTES,
                pinned_address=target.pinned_address,
            )
            if 300 <= response.status_code < 400:
                if redirects >= _MAX_FETCH_REDIRECTS:
                    raise _FetchFailure(
                        "REDIRECT_LIMIT_EXCEEDED",
                        f"document exceeded the {_MAX_FETCH_REDIRECTS}-redirect limit",
                    )
                location = response.header("location").strip()
                if not location:
                    raise _FetchFailure(
                        "REDIRECT_NOT_ALLOWED", "redirect response has no Location"
                    )
                next_url = urljoin(current_url, location)
                target = _validate_fetch_url(
                    next_url, self._address_resolver,
                    previous_scheme=urlsplit(current_url).scheme,
                    mode=self._egress_mode,
                )
                current_url = target.url
                redirect_chain.append(current_url)
                redirects += 1
                continue
            if not 200 <= response.status_code < 300:
                raise _FetchFailure(
                    "HTTP_STATUS",
                    f"remote server returned HTTP {response.status_code}",
                    retryable=response.status_code >= 500,
                )
            content_encoding = response.header("content-encoding").strip().casefold()
            payload = self._decode_content_encoding(
                response.body, content_encoding, response.truncated,
            )
            raw_content_type = response.header("content-type")
            content_type = raw_content_type.split(";", 1)[0].strip().casefold()
            if content_type not in _ALLOWED_FETCH_CONTENT_TYPES:
                raise _FetchFailure(
                    "UNSUPPORTED_CONTENT_TYPE",
                    "response Content-Type must be text/html, text/plain, or Markdown",
                )
            text = self._decode_fetch_content(payload, raw_content_type)
            if content_type == "text/html":
                parser = _HTMLToTextParser()
                parser.feed(text)
                parser.close()
                text = parser.text()
            content = html.unescape(text[:_MAX_STORED_TEXT_CHARS])
            # A JS-rendered app before its bundle runs converts to almost
            # nothing. When the host configured a render service, ask it for the
            # rendered text instead of handing the model a blank page. Only HTML
            # is retried: plain text and Markdown have no rendering step to miss.
            rendered_by = ""
            if (
                self._render_url
                and content_type == "text/html"
                and len(content.strip()) < _MIN_RENDERABLE_TEXT_CHARS
            ):
                rendered = self._render_remote(current_url)
                if rendered:
                    content = html.unescape(rendered[:_MAX_FETCH_CHARS])
                    rendered_by = "service"
            return {
                "url": current_url,
                "requested_url": requested_url,
                "content_type": content_type,
                "content": content,
                "rendered_by": rendered_by,
                "truncated": response.truncated or len(text) > _MAX_STORED_TEXT_CHARS,
                "redirect_count": redirects,
                "redirect_chain": redirect_chain,
                "egress_mode": str(self._egress_mode),
                "address_validated_in_process": (
                    self._egress_mode is WebEgressMode.DIRECT
                ),
            }

    def _render_remote(self, url: str) -> str | None:
        """Ask the configured render service for a page's rendered text.

        The service is operator-supplied, so it is contacted directly instead of
        through the document transport: it is not the target being fetched, and
        the operator already chose to trust it. Any failure -- unreachable,
        non-2xx, unparseable, or a payload with no usable field -- degrades to
        ``None`` so the caller keeps the thin document it already has rather than
        turning an optional fallback into a hard error.
        """

        request = Request(
            self._render_url,
            data=json.dumps({"url": url}).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/html, text/plain",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=_RENDER_TIMEOUT_SECONDS) as response:
                if not 200 <= int(response.status) < 300:
                    return None
                payload = response.read(_MAX_FETCH_RESPONSE_BYTES)
        except (HTTPError, URLError, OSError, ValueError):
            return None
        try:
            decoded: Any = json.loads(payload.decode("utf-8", errors="replace"))
        except ValueError:
            return payload.decode("utf-8", errors="replace").strip() or None
        if not isinstance(decoded, Mapping):
            return None
        for key in ("content", "text", "markdown"):
            value = decoded.get(key)
            if isinstance(value, str) and value.strip():
                return value
        # A service that answers "here is the rendered HTML" still needs the same
        # conversion the direct path uses.
        raw_html = decoded.get("html")
        if isinstance(raw_html, str) and raw_html.strip():
            parser = _HTMLToTextParser()
            parser.feed(raw_html)
            parser.close()
            return parser.text().strip() or None
        return None

    @staticmethod
    def _decode_content_encoding(
        payload: bytes, content_encoding: str, truncated: bool,
    ) -> bytes:
        if not content_encoding or content_encoding == "identity":
            return payload
        encodings = tuple(
            item.strip() for item in content_encoding.split(",") if item.strip()
        )
        if len(encodings) != 1 or encodings[0] not in {"gzip", "deflate"}:
            raise _FetchFailure(
                "UNSUPPORTED_CONTENT_ENCODING",
                "response Content-Encoding must be identity, gzip, or deflate",
            )
        if truncated:
            raise _FetchFailure(
                "COMPRESSED_RESPONSE_TRUNCATED",
                "compressed response exceeded the byte limit before decoding",
            )
        encoding = encodings[0]
        try:
            return NetworkToolProvider._decompress_bounded(
                payload,
                16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS,
            )
        except zlib.error:
            if encoding != "deflate":
                raise _FetchFailure(
                    "INVALID_COMPRESSED_RESPONSE",
                    "gzip response could not be decoded",
                ) from None
            try:
                return NetworkToolProvider._decompress_bounded(
                    payload, -zlib.MAX_WBITS,
                )
            except zlib.error:
                raise _FetchFailure(
                    "INVALID_COMPRESSED_RESPONSE",
                    "deflate response could not be decoded",
                ) from None

    @staticmethod
    def _decompress_bounded(payload: bytes, window_bits: int) -> bytes:
        decompressor = zlib.decompressobj(window_bits)
        limit = _MAX_DECOMPRESSED_FETCH_BYTES
        decoded = decompressor.decompress(payload, limit + 1)
        if len(decoded) > limit or decompressor.unconsumed_tail:
            raise _FetchFailure(
                "DECOMPRESSED_RESPONSE_TOO_LARGE",
                "decoded response exceeded the decompressed byte limit",
            )
        decoded += decompressor.flush(limit + 1 - len(decoded))
        if len(decoded) > limit:
            raise _FetchFailure(
                "DECOMPRESSED_RESPONSE_TOO_LARGE",
                "decoded response exceeded the decompressed byte limit",
            )
        if not decompressor.eof or decompressor.unused_data:
            raise zlib.error("incomplete or trailing compressed response")
        return decoded

    @staticmethod
    def _decode_fetch_content(payload: bytes, content_type: str) -> str:
        match = re.search(r"charset\\s*=\\s*[\"']?([^;\\s\"']+)", content_type, re.I)
        charset = match.group(1) if match else "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")

    def _summarize(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        content = self._required_string(arguments, "content")
        max_sentences = min(max(int(arguments.get("max_sentences", 3)), 1), _MAX_SUMMARY_SENTENCES)
        sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", content).strip())
        return {
            "summary": " ".join(sentences[:max_sentences]).strip(),
            "sentence_count": min(len(sentences), max_sentences),
        }

    @staticmethod
    def _required_string(arguments: Mapping[str, Any], name: str) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _parse_search_result(item: Mapping[str, Any]) -> dict[str, str] | None:
        text = item.get("Text")
        url = item.get("FirstURL")
        if not isinstance(text, str) or not isinstance(url, str):
            return None
        return {"title": text.split(" - ", 1)[0], "url": url, "snippet": text}

    def _fallback_html_search(
        self, query: str, top_k: int, *,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> list[dict[str, str]]:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        request = Request(url, headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(_MAX_SEARCH_BYTES).decode(
                "utf-8", errors="replace"
            )

        parser = _DuckDuckGoResultsParser()
        parser.feed(payload)
        cleaned: list[dict[str, str]] = []
        for result in parser.results:
            url_value = self._normalize_search_url(result.get("url", ""))
            if url_value is None:
                continue
            cleaned.append({**result, "url": url_value})
            if len(cleaned) >= top_k:
                break
        return cleaned

    @staticmethod
    def _normalize_search_url(value: str) -> str | None:
        """Resolve DuckDuckGo redirect links to the public result URL."""
        candidate = html.unescape(value).strip()
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        parsed = urlsplit(candidate)
        if parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com"}:
            redirected = parse_qs(parsed.query).get("uddg", [])
            if redirected:
                candidate = unquote(redirected[0])
                parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        return candidate

    @staticmethod
    def _load_json(
        url: str,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> Mapping[str, Any]:
        request = Request(url, headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(_MAX_SEARCH_BYTES)
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, Mapping):
            raise ValueError("search provider returned an invalid payload")
        return data


# Backward-compatible alias retained for older import paths.
NetworkToolAdapter = NetworkToolProvider
