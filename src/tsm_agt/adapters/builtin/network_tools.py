"""Minimal production-style network tools with single responsibilities."""

from __future__ import annotations

import asyncio
import html
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit
from urllib.request import Request, urlopen

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
)
from tsm_agt.retrieval.pipeline import RetrievalPipeline
from tsm_agt.retrieval.schemas import RetrievalResult

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 tsm-agt/0.1"
)
_MAX_FETCH_CHARS = 20000
_MAX_SEARCH_BYTES = 200000
_MAX_SUMMARY_SENTENCES = 5


class _NoSearchResults(RuntimeError):
    """The provider was reachable but returned no usable public results."""


class _HTMLToTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        cleaned = data.strip()
        if cleaned:
            self._chunks.append(cleaned)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._chunks)).strip()


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

    def search(self, query: str, top_k: int) -> list[RetrievalResult]:
        try:
            payload = self._tools._load_json(
                "https://api.duckduckgo.com/?q="
                f"{quote_plus(query)}&format=json&no_redirect=1&no_html=1"
            )
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
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

    def search(self, query: str, top_k: int) -> list[RetrievalResult]:
        return [
            RetrievalResult(
                title=item["title"],
                url=item["url"],
                snippet=item["snippet"],
                provider=self.name,
            )
            for item in self._tools._fallback_html_search(query, top_k)
        ]


class _RssNewsProvider:
    name = "rss-news"

    def __init__(self, tools: "NetworkToolProvider") -> None:
        self._tools = tools

    def search(self, query: str, top_k: int) -> list[RetrievalResult]:
        rss_query = quote_plus(query)
        url = (
            "https://news.google.com/rss/search?q="
            f"{rss_query}&hl=en-US&gl=US&ceid=US:en"
        )
        request = Request(url, headers={"User-Agent": _USER_AGENT})

        with urlopen(request, timeout=15) as response:
            payload = response.read(_MAX_SEARCH_BYTES).decode(
                "utf-8", errors="replace"
            )

        root = ET.fromstring(payload)
        results: list[RetrievalResult] = []

        for item in root.findall("./channel/item"):
            if len(results) >= top_k:
                break

            title = item.findtext("title") or "RSS result"
            link = item.findtext("link") or ""
            description = item.findtext("description") or ""
            description = re.sub(r"<[^>]+>", " ", description)

            if not link.strip():
                continue

            results.append(
                RetrievalResult(
                    title=html.unescape(title.strip()),
                    url=link.strip(),
                    snippet=html.unescape(description.strip()),
                    provider=self.name,
                    source="news",
                    content_type="rss",
                )
            )

        return results


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
            description="Search the public web and return lightweight search results only.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
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
            description="Fetch one web page and convert visible content into compact text.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            requires_network=True,
            data_transmission="user-selected public URL",
            rollback="read-only public page request; no remote mutation",
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

    def __init__(self, *, enable_fetch: bool = False) -> None:
        """Expose fixed-endpoint search; arbitrary fetch is opt-in only.

        ``web.fetch_markdown`` remains disabled in production composition until
        a replaceable NetworkAccessPolicy validates URL, DNS, resolved address,
        redirects and response limits. This keeps the useful search capability
        without turning a partial local HTTP client into an SSRF primitive.
        """
        self._enable_fetch = enable_fetch
        self._started = False
        self._pipeline = RetrievalPipeline(
            providers=[
                _DuckDuckGoInstantProvider(self),
                _DuckDuckGoHtmlProvider(self),
                _RssNewsProvider(self),
            ]
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
                    self._web_search, call.arguments
                )
            elif call.name == "web.fetch_markdown":
                if not self._enable_fetch:
                    return ToolResult(
                        call.call_id, False, error_code="CAPABILITY_DISABLED",
                        message=(
                            "arbitrary URL fetch requires a configured "
                            "NetworkAccessPolicy"
                        ),
                    )
                data = await asyncio.to_thread(
                    self._fetch_markdown, call.arguments
                )
            elif call.name == "content.summarize":
                data = self._summarize(call.arguments)
            else:
                return ToolResult(call.call_id, False, error_code="NOT_FOUND", message=f"unknown network tool: {call.name}")
            return ToolResult(call.call_id, True, data=data, meta={"untrusted_data": True})
        except (TypeError, ValueError) as error:
            return ToolResult(call.call_id, False, error_code="INVALID_PARAM", message=str(error))
        except _NoSearchResults as error:
            return ToolResult(
                call.call_id, False, error_code="NO_RESULTS",
                message=str(error), retryable=False,
            )
        except OSError as error:
            return ToolResult(call.call_id, False, error_code="NETWORK_ERROR", message=str(error), retryable=True)

    def _web_search(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        query = self._required_string(arguments, "query")
        top_k = min(max(int(arguments.get("top_k", 5)), 1), 10)

        response = self._pipeline.search(query, top_k)
        if not response.results:
            degraded = response.to_dict()
            degraded["message"] = (
                "public search providers returned no usable results"
            )
            return degraded

        return response.to_dict()

    def _fetch_markdown(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        url = self._required_string(arguments, "url")
        request = Request(url, headers={"User-Agent": _USER_AGENT})
        with urlopen(request, timeout=15) as response:
            raw = response.read(_MAX_FETCH_CHARS)
            content_type = response.headers.get_content_type()
        text = raw.decode("utf-8", errors="replace")
        if content_type == "text/html":
            parser = _HTMLToTextParser()
            parser.feed(text)
            text = parser.text()
        return {"url": url, "content_type": content_type, "content": html.unescape(text[:_MAX_FETCH_CHARS])}

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

    def _fallback_html_search(self, query: str, top_k: int) -> list[dict[str, str]]:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        request = Request(url, headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urlopen(request, timeout=15) as response:
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
    def _load_json(url: str) -> Mapping[str, Any]:
        request = Request(url, headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urlopen(request, timeout=15) as response:
            payload = response.read(_MAX_SEARCH_BYTES)
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, Mapping):
            raise ValueError("search provider returned an invalid payload")
        return data
