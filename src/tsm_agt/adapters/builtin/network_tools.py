"""Minimal production-style network tools with single responsibilities."""

from __future__ import annotations

import asyncio
import html
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import xml.etree.ElementTree as ET
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
    ToolCall,
    ToolEffect,
    ToolIdempotency,
    ToolInvocationContext,
    ToolResult,
    ToolResultAuthority,
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
_MAX_FETCH_RESPONSE_BYTES = 512_000
_MAX_FETCH_REDIRECTS = 3
_ALLOWED_FETCH_CONTENT_TYPES = frozenset({
    "text/html", "text/markdown", "text/plain", "text/x-markdown",
})


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
        capabilities=frozenset({"web.search", "web.fetch_markdown", "content.summarize"}),
    )

    _tools = (
        ToolSpec(
            name="web.search",
            description=(
                "Search the public web and return lightweight search results "
                "with source URLs, and publish times when the source exposes "
                "one. Set freshness for time-sensitive requests such as "
                "'today's news'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
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
        ),
        ToolSpec(
            name="web.fetch_markdown",
            description=(
                "Fetch one user-supplied public HTTP(S) document with bounded "
                "redirects and return Markdown or safe visible HTML text."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "format": "uri"}},
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
        """
        if search_provider_timeout_seconds <= 0:
            raise ValueError("search_provider_timeout_seconds must be positive")
        if search_fallback_deadline_seconds <= 0:
            raise ValueError("search_fallback_deadline_seconds must be positive")
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
        self._egress_mode = WebEgressMode(egress_mode)
        self._fetch_transport = fetch_transport or (
            PinnedAddressWebFetchTransport()
            if self._egress_mode is WebEgressMode.DIRECT
            else UrllibWebFetchTransport()
        )
        self._address_resolver = address_resolver or _resolve_public_addresses
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
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(
            tool for tool in self._tools
            if self._enable_fetch or tool.name != "web.fetch_markdown"
        )

    async def invoke(self, call: ToolCall, context: ToolInvocationContext) -> ToolResult:
        if not self._started:
            raise RuntimeError("network tools are not started")
        try:
            if call.name == "web.search":
                data = await asyncio.to_thread(
                    self._web_search, call.arguments,
                    self._search_deadline(context),
                )
            elif call.name == "web.fetch_markdown":
                if not self._enable_fetch:
                    return ToolResult(
                        call.call_id, False, error_code="CAPABILITY_DISABLED",
                        message="web.fetch_markdown is disabled for this provider",
                    )
                data = await asyncio.to_thread(
                    self._fetch_markdown, call.arguments
                )
            elif call.name == "content.summarize":
                data = self._summarize(call.arguments)
            else:
                return ToolResult(call.call_id, False, error_code="NOT_FOUND", message=f"unknown network tool: {call.name}")
            return ToolResult(call.call_id, True, data=data, meta={"untrusted_data": True})
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
        )
        if not response.results:
            degraded = response.to_dict()
            degraded["message"] = (
                "public search providers returned no usable results"
            )
            return degraded

        return response.to_dict()

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

    def _fetch_markdown(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        requested_url = self._required_string(arguments, "url")
        target = _validate_fetch_url(
            requested_url, self._address_resolver, mode=self._egress_mode
        )
        current_url = target.url
        redirects = 0
        while True:
            response = self._fetch_transport.fetch(
                current_url,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/markdown, text/plain, text/html;q=0.9",
                    "Accept-Encoding": "identity",
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
                redirects += 1
                continue
            if not 200 <= response.status_code < 300:
                raise _FetchFailure(
                    "HTTP_STATUS",
                    f"remote server returned HTTP {response.status_code}",
                    retryable=response.status_code >= 500,
                )
            content_encoding = response.header("content-encoding").strip().casefold()
            if content_encoding and content_encoding != "identity":
                raise _FetchFailure(
                    "UNSUPPORTED_CONTENT_ENCODING",
                    "compressed responses are not accepted",
                )
            raw_content_type = response.header("content-type")
            content_type = raw_content_type.split(";", 1)[0].strip().casefold()
            if content_type not in _ALLOWED_FETCH_CONTENT_TYPES:
                raise _FetchFailure(
                    "UNSUPPORTED_CONTENT_TYPE",
                    "response Content-Type must be text/html, text/plain, or Markdown",
                )
            text = self._decode_fetch_content(response.body, raw_content_type)
            if content_type == "text/html":
                parser = _HTMLToTextParser()
                parser.feed(text)
                parser.close()
                text = parser.text()
            content = html.unescape(text[:_MAX_FETCH_CHARS])
            return {
                "url": current_url,
                "requested_url": requested_url,
                "content_type": content_type,
                "content": content,
                "truncated": response.truncated or len(text) > _MAX_FETCH_CHARS,
                "redirect_count": redirects,
                "egress_mode": str(self._egress_mode),
                "address_validated_in_process": (
                    self._egress_mode is WebEgressMode.DIRECT
                ),
            }

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
