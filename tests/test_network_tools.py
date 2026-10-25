from __future__ import annotations

import gzip
import json
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

from tsm_agt.adapters.builtin import NetworkToolProvider
from tsm_agt.adapters.builtin.network_tools import (
    _MAX_DECOMPRESSED_FETCH_BYTES, _search_outage_failure,
    FetchResponse, _BingNewsRssProvider, _DuckDuckGoLiteProvider,
    _RssNewsProvider, _TavilyProvider, _WikipediaProvider,
)
from tsm_agt.retrieval.schemas import RetrievalResult, RetrievalSearchResponse
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import CoreToolPolicy, PolicyAction, ProjectTrustLevel, TaskState
from tsm_agt.ports import (
    AdapterContext, ToolCall, ToolEffect, ToolFactCapability,
    ToolResultMaterialization,
)


class _FakeFetchTransport:
    def __init__(self, responses: dict[str, FetchResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.pinned_addresses: list[str | None] = []

    def fetch(
        self, url, *, headers, timeout_seconds, max_bytes, pinned_address=None,
    ):
        self.calls.append(url)
        self.headers.append(dict(headers))
        self.pinned_addresses.append(pinned_address)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def _public_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("8.8.8.8",)


class NetworkToolProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # These tests exercise the provider chain, which only runs when egress is
        # permitted. The default is ``live`` (see WebSearchModeTest).
        self.provider = NetworkToolProvider(search_mode="live")
        await self.provider.start(AdapterContext({}, lambda *_: None))

    async def asyncTearDown(self) -> None:
        from datetime import datetime
        await self.provider.stop(datetime.now())

    async def test_default_contract_advertises_safe_document_fetch(self):
        tools = await self.provider.list_tools()
        self.assertEqual(
            [tool.name for tool in tools],
            [
                "web.search", "web.fetch_markdown", "web.open",
                "web.find_in_page", "web.read", "content.summarize",
            ],
        )
        fetch = tools[1]
        self.assertTrue(fetch.requires_network)
        self.assertTrue(fetch.is_read_only)
        self.assertEqual(fetch.effect, ToolEffect.OBSERVE)
        self.assertIn("HTTP(S) URL", fetch.data_transmission)

    async def test_github_resolve_is_advertised_only_with_the_github_adapter(self) -> None:
        # ``source.github.resolve`` only names targets inside the GitHub source
        # adapter. Without that adapter it routes the model to tools that do not
        # exist, so it must stay out of the advertised toolset.
        self.assertNotIn(
            "source.github.resolve",
            [tool.name for tool in await self.provider.list_tools()],
        )

        with_adapter = NetworkToolProvider(
            search_mode="live", github_source_tools=True
        )
        await with_adapter.start(AdapterContext({}, lambda *_: None))
        self.addAsyncCleanup(with_adapter.stop, datetime.now())
        self.assertIn(
            "source.github.resolve",
            [tool.name for tool in await with_adapter.list_tools()],
        )

    async def test_fetch_returns_markdown_without_transforming_it(self):
        transport = _FakeFetchTransport({
            "https://docs.example.test/spec.md": FetchResponse(
                200, "https://docs.example.test/spec.md",
                {"Content-Type": "text/markdown; charset=utf-8"},
                b"# Spec\\n\\n- Keep this Markdown intact.\\n",
            )
        })
        provider = NetworkToolProvider(
            fetch_transport=transport, address_resolver=_public_resolver
        )
        await provider.start(AdapterContext({}, lambda *_: None))
        try:
            result = await provider.invoke(
                ToolCall("fetch-markdown", "web.fetch_markdown", {
                    "url": "https://docs.example.test/spec.md"
                }), None,
            )
        finally:
            await provider.stop(__import__("datetime").datetime.now())

        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(result.data["content"], "# Spec\\n\\n- Keep this Markdown intact.\\n")
        self.assertEqual(result.data["content_type"], "text/markdown")
        self.assertEqual(transport.calls, ["https://docs.example.test/spec.md"])
        self.assertTrue(result.meta["untrusted_data"])

    async def test_fetch_converts_html_without_script_or_style_content(self):
        transport = _FakeFetchTransport({
            "https://docs.example.test/page": FetchResponse(
                200, "https://docs.example.test/page",
                {"Content-Type": "text/html"},
                b"<main><h1>Guide</h1><p>Visible text.</p><ul><li>First</li></ul>"
                b"<script>steal()</script><style>.hidden{}</style></main>",
            )
        })
        provider = NetworkToolProvider(
            fetch_transport=transport, address_resolver=_public_resolver
        )
        await provider.start(AdapterContext({}, lambda *_: None))
        try:
            result = await provider.invoke(
                ToolCall("fetch-html", "web.fetch_markdown", {
                    "url": "https://docs.example.test/page"
                }), None,
            )
        finally:
            await provider.stop(__import__("datetime").datetime.now())

        self.assertTrue(result.ok, result.to_data())
        self.assertIn("Guide", result.data["content"])
        self.assertIn("Visible text.", result.data["content"])
        self.assertIn("- First", result.data["content"])
        self.assertNotIn("steal", result.data["content"])
        self.assertNotIn("hidden", result.data["content"])

    async def test_fetch_handles_redirect_and_remote_errors(self):
        transport = _FakeFetchTransport({
            "https://docs.example.test/start": FetchResponse(
                302, "https://docs.example.test/start",
                {"Location": "/final"}, b"",
            ),
            "https://docs.example.test/final": FetchResponse(
                503, "https://docs.example.test/final",
                {"Content-Type": "text/plain"}, b"unavailable",
            ),
        })
        provider = NetworkToolProvider(
            fetch_transport=transport, address_resolver=_public_resolver
        )
        await provider.start(AdapterContext({}, lambda *_: None))
        try:
            result = await provider.invoke(
                ToolCall("fetch-error", "web.fetch_markdown", {
                    "url": "https://docs.example.test/start"
                }), None,
            )
        finally:
            await provider.stop(__import__("datetime").datetime.now())

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "HTTP_STATUS")
        self.assertTrue(result.retryable)
        self.assertEqual(transport.calls, [
            "https://docs.example.test/start", "https://docs.example.test/final",
        ])

    async def test_empty_instant_answer_falls_back_to_html_results(self):
        html = (
            ("<!-- search page chrome -->" * 1200) +
            '<article class="result">'
            '<a class="result__a" '
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnba">'
            'NBA latest news</a>'
            '<div class="result__snippet">'
            'Latest playoff and trade updates from around the league.'
            '</div>'
            '</article>'
        )
        with patch.object(self.provider, "_load_json", return_value={}), \
             patch(
                 "tsm_agt.adapters.builtin.network_tools.urlopen",
                 return_value=_Response(html.encode()),
             ):
            result = await self.provider.invoke(
                ToolCall("search-1", "web.search", {
                    "query": "today NBA news", "top_k": 3,
                }),
                None,  # The built-in provider does not consume invocation authority.
            )
        self.assertTrue(result.ok, result.to_data())
        self.assertIn("duckduckgo-html", result.data["provider"])
        self.assertIn("telemetry", result.data)
        urls = [item["url"] for item in result.data["results"]]
        self.assertIn("https://example.com/nba", urls)
        matched = next(
            item for item in result.data["results"]
            if item["url"] == "https://example.com/nba"
        )
        self.assertEqual(
            matched["snippet"],
            "Latest playoff and trade updates from around the league.",
        )
        self.assertTrue(result.meta["untrusted_data"])
        telemetry = {
            entry["provider"]: entry["status"]
            for entry in result.data["telemetry"]
        }
        self.assertEqual(telemetry["duckduckgo-instant-answer"], "empty")
        self.assertEqual(telemetry["duckduckgo-html"], "success")
        self.assertFalse(result.data["cache_hit"])
        self.assertIn("fetched_at", matched)

    async def test_cached_search_response_is_reused(self):
        html = (
            '<article class="result">'
            '<a class="result__a" href="https://example.com/cache">'
            'Cache result</a>'
            '<div class="result__snippet">cached snippet</div>'
            '</article>'
        )
        with patch.object(self.provider, "_load_json", return_value={}), \
             patch(
                 "tsm_agt.adapters.builtin.network_tools.urlopen",
                 return_value=_Response(html.encode()),
             ):
            first = await self.provider.invoke(
                ToolCall("search-cache-1", "web.search", {
                    "query": "cache me", "top_k": 2,
                }),
                None,
            )
            second = await self.provider.invoke(
                ToolCall("search-cache-2", "web.search", {
                    "query": "cache me", "top_k": 2,
                }),
                None,
            )

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertFalse(first.data["cache_hit"])
        self.assertTrue(second.data["cache_hit"])
        self.assertEqual(first.data["results"], second.data["results"])

    async def test_provider_failures_return_degraded_response(self):
        failing_response = Mock()
        failing_response.read.side_effect = TimeoutError("provider timeout")
        failing_response.__enter__ = Mock(return_value=failing_response)
        failing_response.__exit__ = Mock(return_value=False)

        with patch.object(self.provider, "_load_json", side_effect=TimeoutError("instant timeout")), \
             patch(
                 "tsm_agt.adapters.builtin.network_tools.urlopen",
                 return_value=failing_response,
             ):
            result = await self.provider.invoke(
                ToolCall("search-timeout", "web.search", {
                    "query": "today NBA news", "top_k": 3,
                }),
                None,
            )

        # Every provider failed, so this is an outage and must not be reported as
        # a successful search with zero results: a model reading "ok, no results"
        # concludes the fact does not exist. The reference implementation reports the same case as
        # ``success: false``.
        self.assertFalse(result.ok, result.to_data())
        self.assertEqual(result.error_code, "SEARCH_UNAVAILABLE")
        self.assertTrue(result.retryable)
        self.assertEqual(result.data["provider"], "degraded-no-results")
        self.assertEqual(result.data["results"], [])
        # A timeout must stay visible in telemetry instead of being silently
        # downgraded to an empty result list.
        self.assertEqual(len(result.data["telemetry"]), 6)
        statuses = {entry["status"] for entry in result.data["telemetry"]}
        self.assertEqual(statuses, {"timeout"}, statuses)
        # ...and the failing providers must be named in the message.
        self.assertIn("wikipedia", result.message)

    async def test_all_providers_empty_stays_a_success(self):
        """Contrast with the outage above: answering "nothing matched" is fine."""

        with patch.object(self.provider, "_load_json", return_value={}), \
             patch(
                 "tsm_agt.adapters.builtin.network_tools.urlopen",
                 return_value=_Response(b""),
             ):
            result = await self.provider.invoke(
                ToolCall("search-empty", "web.search", {
                    "query": "today NBA news", "top_k": 3,
                }),
                None,
            )

        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(result.data["results"], [])
        self.assertEqual(result.data["result_count"], 0)

    async def test_pipeline_covers_the_additional_keyless_providers(self):
        names = [provider.name for provider in self.provider._pipeline._providers]
        self.assertEqual(names, [
            "duckduckgo-lite",
            "duckduckgo-html",
            "bing-news-rss",
            "rss-news",
            "duckduckgo-instant-answer",
            "wikipedia",
        ])

    async def test_lite_provider_resolves_redirect_links(self):
        lite_html = (
            '<a class="result-link" '
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnba">'
            'NBA today</a>'
            '<td class="result-snippet">Scores and trades</td>'
        )
        with patch(
            "tsm_agt.adapters.builtin.network_tools.urlopen",
            return_value=_Response(lite_html.encode()),
        ):
            results = _DuckDuckGoLiteProvider(self.provider).search("nba", 3)

        self.assertEqual(
            [(item.url, item.snippet) for item in results],
            [("https://example.com/nba", "Scores and trades")],
        )

    async def test_bing_news_provider_reads_keyword_feed(self):
        feed = (
            "<rss><channel>"
            "<item><title>NBA 交易</title>"
            "<link>https://example.com/bing-nba</link>"
            "<description>&lt;p&gt;今日交易汇总&lt;/p&gt;</description></item>"
            "</channel></rss>"
        )
        with patch(
            "tsm_agt.adapters.builtin.network_tools.urlopen",
            return_value=_Response(feed.encode()),
        ):
            results = _BingNewsRssProvider(self.provider).search("NBA", 3)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/bing-nba")
        self.assertEqual(results[0].snippet, "今日交易汇总")
        self.assertEqual(results[0].provider, "bing-news-rss")

    async def test_wikipedia_provider_returns_citable_article_urls(self):
        payload = {"query": {"search": [
            {"title": "勒布朗·詹姆斯", "snippet": "<span>美國職業籃球員</span>"},
        ]}}
        with patch.object(self.provider, "_load_json", return_value=payload):
            results = _WikipediaProvider(self.provider).search("詹姆斯", 2)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].url.startswith("https://zh.wikipedia.org/wiki/"))
        self.assertEqual(results[0].snippet, "美國職業籃球員")

    async def test_google_news_snippet_names_the_real_publisher(self):
        feed = (
            "<rss><channel><item>"
            "<title>Trade grades</title>"
            "<link>https://news.google.com/rss/articles/CBMiOPAQUE</link>"
            "<description>&lt;a&gt;Trade grades&lt;/a&gt;</description>"
            "<source>ESPN</source>"
            "</item></channel></rss>"
        )
        with patch(
            "tsm_agt.adapters.builtin.network_tools.urlopen",
            return_value=_Response(feed.encode()),
        ):
            results = _RssNewsProvider(self.provider).search("nba", 2)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].snippet.startswith("[ESPN]"))

    async def test_untrusted_workspace_can_use_dedicated_search(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(tool_adapters=(self.provider,))
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("search public news", root)
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                search = next(
                    tool for tool in await app.kernel.list_tools()
                    if tool.name == "web.search"
                )
                decision = CoreToolPolicy().evaluate(
                    search, ToolCall("search-policy", "web.search", {
                        "query": "public news"
                    }), ProjectTrustLevel.UNTRUSTED,
                )
                self.assertEqual(decision.action, PolicyAction.ALLOW)
                self.assertTrue(decision.requires_network)
            finally:
                await app.registry.stop_all()

    async def test_search_contract_routes_local_engineering_work_to_local_evidence(self):
        """The model contract expresses routing; policy still permits web search."""
        search = next(
            tool for tool in await self.provider.list_tools()
            if tool.name == "web.search"
        )

        assert search.model_contract is not None
        self.assertIn(
            "task and repository evidence cannot answer",
            search.model_contract.use_when[0],
        )
        self.assertIn(
            "implementation, validation, diagnosis, or recovery",
            search.model_contract.avoid_when[0],
        )


def _rss_item(title, link, pub_date):
    return (
        f"<item><title>{title}</title><link>{link}</link>"
        f"<description>summary</description><pubDate>{pub_date}</pubDate></item>"
    )


class SearchRecencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = NetworkToolProvider()
        self.now = datetime.now(timezone.utc)

    def _feed(self, *items: str) -> bytes:
        return (
            "<rss><channel>" + "".join(items) + "</channel></rss>"
        ).encode()

    def test_google_news_sorts_newest_first_and_keeps_published_at(self):
        feed = self._feed(
            _rss_item("old", "https://e/old", format_datetime(
                self.now - timedelta(days=20)
            )),
            _rss_item("new", "https://e/new", format_datetime(
                self.now - timedelta(hours=2)
            )),
        )
        with patch(
            "tsm_agt.adapters.builtin.network_tools.urlopen",
            return_value=_Response(feed),
        ):
            results = _RssNewsProvider(self.provider).search("nba", 5)

        self.assertEqual([item.url for item in results],
                         ["https://e/new", "https://e/old"])
        self.assertIsNotNone(results[0].published_at)

    def test_google_news_adds_the_when_operator_for_freshness(self):
        captured: dict[str, str] = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            return _Response(self._feed(
                _rss_item("n", "https://e/n", format_datetime(self.now))
            ))

        with patch(
            "tsm_agt.adapters.builtin.network_tools.urlopen",
            side_effect=fake_urlopen,
        ):
            _RssNewsProvider(self.provider).search(
                "nba", 5, freshness="week",
            )

        self.assertIn("when%3A7d", captured["url"])

    def test_bing_news_drops_items_outside_the_freshness_window(self):
        feed = self._feed(
            _rss_item("stale", "https://e/stale", format_datetime(
                self.now - timedelta(days=10)
            )),
            _rss_item("fresh", "https://e/fresh", format_datetime(
                self.now - timedelta(hours=3)
            )),
        )
        with patch(
            "tsm_agt.adapters.builtin.network_tools.urlopen",
            return_value=_Response(feed),
        ):
            results = _BingNewsRssProvider(self.provider).search(
                "NBA", 5, freshness="day",
            )

        self.assertEqual([item.url for item in results], ["https://e/fresh"])

    def test_tavily_maps_freshness_and_parses_published_date(self):
        captured: dict[str, object] = {}
        # Keep the fixture inside the "day" window regardless of wall clock.
        published = datetime.now(timezone.utc) - timedelta(hours=1)
        published_text = published.strftime("%Y-%m-%dT%H:%M:%SZ")

        def fake_urlopen(request, timeout=None):
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Response(json.dumps({"results": [{
                "title": "NBA news",
                "url": "https://example.com/nba",
                "content": "fresh snippet",
                "published_date": published_text,
            }]}).encode())

        with patch(
            "tsm_agt.adapters.builtin.network_tools.urlopen",
            side_effect=fake_urlopen,
        ):
            results = _TavilyProvider(
                self.provider, "tvly-secret"
            ).search("nba", 3, freshness="day")

        body = captured["body"]
        self.assertEqual(body["topic"], "news")
        self.assertEqual(body["days"], 1)
        self.assertEqual(
            captured["headers"].get("Authorization"), "Bearer tvly-secret"
        )
        self.assertEqual(results[0].url, "https://example.com/nba")
        self.assertTrue(results[0].published_at.startswith(published_text[:10]))

    def test_tavily_keyless_sends_access_mode_header(self):
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout=None):
            captured["headers"] = dict(request.headers)
            return _Response(json.dumps({"results": []}).encode())

        with patch(
            "tsm_agt.adapters.builtin.network_tools.urlopen",
            side_effect=fake_urlopen,
        ):
            _TavilyProvider(
                self.provider, "", keyless=True
            ).search("nba", 3)

        self.assertEqual(
            captured["headers"].get("X-tavily-access-mode"), "keyless"
        )
        self.assertNotIn("Authorization", captured["headers"])

    def test_tavily_is_prepended_only_when_enabled(self):
        default = NetworkToolProvider()
        self.assertNotIn(
            "tavily", [p.name for p in default._pipeline._providers]
        )
        keyless = NetworkToolProvider(search_tavily_mode="keyless")
        self.assertEqual(keyless._pipeline._providers[0].name, "tavily")
        keyed = NetworkToolProvider(
            search_tavily_mode="key", search_tavily_api_key="tvly-x"
        )
        self.assertEqual(keyed._pipeline._providers[0].name, "tavily")

    def test_tavily_key_mode_requires_a_key(self):
        with self.assertRaises(ValueError):
            NetworkToolProvider(search_tavily_mode="key")

    def test_web_search_rejects_unknown_freshness(self):
        with self.assertRaises(ValueError):
            self.provider._freshness({"freshness": "year"})
        self.assertEqual(self.provider._freshness({}), "any")
        self.assertEqual(self.provider._freshness({"freshness": " WEEK "}), "week")


class _Headers:
    @staticmethod
    def get_content_type() -> str:
        return "text/html"


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.headers = _Headers()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int | None = None) -> bytes:
        return self.payload if _limit is None else self.payload[:_limit]


if __name__ == "__main__":
    unittest.main()


class WebSearchModeTest(unittest.IsolatedAsyncioTestCase):
    """``web.search`` must not reach the network unless the host said it may.

    Mirrors Codex's ``WebSearchMode`` with one deliberate difference: the default
    is ``live``, because unlike Codex there is no provider-side index for
    ``cached`` to serve from. ``cached`` (no egress) and ``off`` remain available
    for hosts that want the stricter posture. The failure mapping mirrors the reference implementation's
    ``success: false`` -- an outage reported as ``ok=True`` with an empty list
    would let a model conclude that a fact does not exist when in truth nothing
    answered.
    """

    async def test_default_mode_queries_the_network(self) -> None:
        """The default is ``live``: search works out of the box.

        Proven by the failure *code*. Every provider is made to fail, so a
        successful search is impossible either way -- but a ``cached`` default
        would have refused with ``SEARCH_NOT_CACHED`` and opened nothing, while
        ``SEARCH_UNAVAILABLE`` is only reachable after the providers actually ran.
        """

        provider = NetworkToolProvider()
        await provider.start(AdapterContext({}, lambda *_: None))
        self.addAsyncCleanup(provider.stop, datetime.now(timezone.utc))

        with patch(
            "tsm_agt.adapters.builtin.network_tools.urlopen",
            side_effect=OSError("no route to host"),
        ):
            result = await provider.invoke(
                ToolCall("default-1", "web.search", {"query": "today NBA news"}),
                None,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "SEARCH_UNAVAILABLE")

    async def test_cached_mode_refuses_without_egress(self) -> None:
        provider = NetworkToolProvider(search_mode="cached")
        await provider.start(AdapterContext({}, lambda *_: None))
        self.addAsyncCleanup(provider.stop, datetime.now(timezone.utc))

        def _explode(*_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("cached mode opened a connection")

        with patch(
            "tsm_agt.adapters.builtin.network_tools.urlopen", _explode,
        ):
            result = await provider.invoke(
                ToolCall("cached-1", "web.search", {"query": "today NBA news"}),
                None,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "SEARCH_NOT_CACHED")
        self.assertIn("TSM_AGT_SEARCH_MODE=live", result.message)

    async def test_off_mode_refuses_and_names_the_setting(self) -> None:
        provider = NetworkToolProvider(search_mode="off")
        await provider.start(AdapterContext({}, lambda *_: None))
        self.addAsyncCleanup(provider.stop, datetime.now(timezone.utc))

        result = await provider.invoke(
            ToolCall("off-1", "web.search", {"query": "anything"}), None,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "SEARCH_DISABLED")

    async def test_an_unknown_mode_is_rejected_at_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "search_mode must be"):
            NetworkToolProvider(search_mode="sometimes")

    def test_a_total_outage_is_not_a_success(self) -> None:
        failure = _search_outage_failure(
            "c1",
            {
                "provider": "degraded-no-results",
                "results": [],
                "telemetry": [
                    {"provider": "duckduckgo-lite", "status": "error"},
                    {"provider": "wikipedia", "status": "timeout"},
                ],
            },
            "live",
        )

        self.assertIsNotNone(failure)
        self.assertEqual(failure.error_code, "SEARCH_UNAVAILABLE")
        self.assertTrue(failure.retryable)
        # The telemetry still rides along so a caller can see who failed.
        self.assertIn("duckduckgo-lite", str(failure.data))

    def test_a_genuine_empty_result_stays_a_success(self) -> None:
        """Every provider answered and matched nothing: that is a real answer."""

        failure = _search_outage_failure(
            "c1",
            {
                "provider": "degraded-no-results",
                "results": [],
                "telemetry": [{"provider": "bing-news-rss", "status": "empty"}],
            },
            "live",
        )

        self.assertIsNone(failure)

    def test_results_are_never_reported_as_a_failure(self) -> None:
        failure = _search_outage_failure(
            "c1",
            {
                "provider": "bing-news-rss",
                "results": [{"title": "t", "url": "https://e.test"}],
                "telemetry": [],
            },
            "live",
        )

        self.assertIsNone(failure)

    def test_a_cache_miss_names_the_setting_that_would_fix_it(self) -> None:
        failure = _search_outage_failure(
            "c1",
            {"provider": "cache-miss", "results": [], "telemetry": []},
            "cached",
        )

        self.assertIsNotNone(failure)
        self.assertEqual(failure.error_code, "SEARCH_NOT_CACHED")
        self.assertIn("TSM_AGT_SEARCH_MODE=live", failure.message)


class _RenderResponse:
    """Minimal stand-in for a render service's HTTP response."""

    def __init__(self, payload) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self, _limit=None) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class PageRenderingTest(unittest.IsolatedAsyncioTestCase):
    """A configured render service rescues content that needs JavaScript.

    Three guards matter and are each pinned below: only HTML is retried, only
    when the converted text is thin, and any service failure keeps the original
    document. With no render URL configured the old behavior is untouched --
    which is what keeps this an opt-in rather than a new dependency.
    """

    URL = "https://app.example.test/"
    SHELL = (
        b"<html><body><div id=root></div>"
        b"<script src=/app.js></script></body></html>"
    )

    def _provider(self, body: bytes = SHELL, **kwargs) -> NetworkToolProvider:
        transport = _FakeFetchTransport({
            self.URL: FetchResponse(
                200, self.URL,
                {"Content-Type": "text/html; charset=utf-8"}, body,
            )
        })
        return NetworkToolProvider(
            fetch_transport=transport, address_resolver=_public_resolver, **kwargs
        )

    async def _fetch(self, provider) -> object:
        await provider.start(AdapterContext({}, lambda *_: None))
        self.addAsyncCleanup(provider.stop, datetime.now(timezone.utc))
        return await provider.invoke(
            ToolCall("fetch", "web.fetch_markdown", {"url": self.URL}), None,
        )

    async def test_unconfigured_never_contacts_a_render_service(self) -> None:
        def _explode(*_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("render service contacted without configuration")

        with patch("tsm_agt.adapters.builtin.network_tools.urlopen", _explode):
            result = await self._fetch(self._provider())

        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(result.data["rendered_by"], "")

    async def test_a_configured_service_replaces_thin_content(self) -> None:
        provider = self._provider(render_url="https://render.example.test/")
        rendered = "Rendered body text. " * 40

        with patch(
            "tsm_agt.adapters.builtin.network_tools.urlopen",
            return_value=_RenderResponse({"content": rendered}),
        ):
            result = await self._fetch(provider)

        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(result.data["rendered_by"], "service")
        self.assertIn("Rendered body text.", result.data["content"])

    async def test_a_service_returning_html_is_converted_like_a_direct_fetch(self) -> None:
        provider = self._provider(render_url="https://render.example.test/")

        with patch(
            "tsm_agt.adapters.builtin.network_tools.urlopen",
            return_value=_RenderResponse({
                "html": "<html><body><p>Rendered paragraph.</p></body></html>",
            }),
        ):
            result = await self._fetch(provider)

        self.assertEqual(result.data["rendered_by"], "service")
        self.assertIn("Rendered paragraph.", result.data["content"])
        self.assertNotIn("<p>", result.data["content"])

    async def test_a_failing_service_keeps_the_original_document(self) -> None:
        provider = self._provider(render_url="https://render.example.test/")

        with patch(
            "tsm_agt.adapters.builtin.network_tools.urlopen",
            side_effect=OSError("renderer down"),
        ):
            result = await self._fetch(provider)

        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(result.data["rendered_by"], "")
        self.assertEqual(result.data["content"].strip(), "")

    async def test_a_rich_html_page_is_not_sent_to_the_service(self) -> None:
        rich = ("<html><body>" + "<p>Plenty of real text.</p>" * 40 + "</body></html>")

        def _explode(*_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("a page with content was sent for rendering")

        provider = self._provider(
            body=rich.encode("utf-8"), render_url="https://render.example.test/",
        )
        with patch("tsm_agt.adapters.builtin.network_tools.urlopen", _explode):
            result = await self._fetch(provider)

        self.assertEqual(result.data["rendered_by"], "")
        self.assertIn("Plenty of real text.", result.data["content"])


class WebOpenAndFindTest(unittest.IsolatedAsyncioTestCase):
    URL = "https://docs.example.test/spec.md"

    @staticmethod
    def _context(task_id: str, turn_id: str):
        return type("_Context", (), {"task_id": task_id, "turn_id": turn_id})()

    async def _provider(self, responses, **kwargs):
        provider = NetworkToolProvider(
            fetch_transport=_FakeFetchTransport(responses),
            address_resolver=_public_resolver,
            **kwargs,
        )
        await provider.start(AdapterContext({}, lambda *_: None))
        self.addAsyncCleanup(provider.stop, datetime.now(timezone.utc))
        return provider

    async def test_open_from_search_ref_reuses_bounded_fetch_and_returns_provenance(self):
        provider = await self._provider({
            self.URL: FetchResponse(
                200, self.URL, {"Content-Type": "text/markdown"},
                b"# Spec\n\nSearchable content.\n",
            ),
        })
        context = self._context("task-a", "turn-a")
        ref_id = provider._store_reference(("task-a", "turn-a"), self.URL)

        result = await provider.invoke(
            ToolCall("open-ref", "web.open", {"ref_id": ref_id}), context,
        )

        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(result.data["canonical_url"], self.URL)
        self.assertTrue(result.data["document_ref"].startswith("webdoc:"))
        self.assertTrue(result.data["content_hash"].startswith("sha256:"))
        self.assertEqual(len(result.data["content_hash"]), 71)
        self.assertEqual(result.data["redirect_chain"], [self.URL])
        self.assertTrue(result.data["fetched_at"])
        self.assertEqual(result.data["provenance"]["source"], "web.fetch_markdown")
        self.assertEqual(result.fact_descriptor.authority, "external_web_document")
        self.assertEqual(result.fact_descriptor.scope, ("task:task-a", "turn:turn-a"))
        self.assertEqual(
            result.fact_descriptor.materialization,
            ToolResultMaterialization.CONTENT,
        )
        self.assertEqual(
            result.fact_descriptor.capabilities,
            (ToolFactCapability.DESCRIBE_CONTENT,),
        )

    async def test_open_previews_large_documents_and_keeps_full_text_searchable(self):
        content = "HEAD\n" + ("a" * 12_000) + "\nMIDDLE_TOKEN\n" + ("z" * 12_000) + "\nTAIL"
        provider = await self._provider({
            self.URL: FetchResponse(
                200, self.URL, {"Content-Type": "text/plain"}, content.encode(),
            ),
        })
        context = self._context("task-a", "turn-a")

        opened = await provider.invoke(
            ToolCall("open-large", "web.open", {"url": self.URL}), context,
        )
        self.assertTrue(opened.ok, opened.to_data())
        self.assertTrue(opened.truncated)
        self.assertTrue(opened.data["preview_truncated"])
        self.assertEqual(opened.data["total_chars"], len(content))
        self.assertIn("HEAD", opened.data["content"])
        self.assertIn("TAIL", opened.data["content"])
        self.assertIn("web.find_in_page", opened.data["content"])
        self.assertNotIn("MIDDLE_TOKEN", opened.data["content"])

        found = await provider.invoke(
            ToolCall("find-middle", "web.find_in_page", {
                "document_ref": opened.data["document_ref"], "pattern": "MIDDLE_TOKEN",
            }),
            context,
        )
        self.assertTrue(found.ok, found.to_data())
        self.assertEqual(found.data["match_count"], 1)
        self.assertGreater(found.data["matches"][0]["offset"], 12_000)
        self.assertEqual(provider._fetch_transport.calls, [self.URL])

    async def test_read_pages_an_opened_document_without_a_network_request(self):
        content = "HEAD\n" + ("a" * 12_000) + "\nMIDDLE_TOKEN\n" + ("z" * 12_000) + "\nTAIL"
        provider = await self._provider({
            self.URL: FetchResponse(
                200, self.URL, {"Content-Type": "text/plain"}, content.encode(),
            ),
        })
        context = self._context("task-a", "turn-a")
        opened = await provider.invoke(
            ToolCall("open-large", "web.open", {"url": self.URL}), context,
        )
        self.assertTrue(opened.ok, opened.to_data())
        document_ref = opened.data["document_ref"]

        start = await provider.invoke(
            ToolCall("read-head", "web.read", {
                "document_ref": document_ref, "offset": 0, "max_chars": 4,
            }),
            context,
        )
        self.assertTrue(start.ok, start.to_data())
        self.assertEqual(start.data["content"], "HEAD")
        self.assertEqual(start.data["offset"], 0)
        self.assertEqual(start.data["returned_chars"], 4)
        self.assertEqual(start.data["total_chars"], len(content))
        self.assertEqual(start.data["next_offset"], 4)
        self.assertTrue(start.data["truncated"])

        middle_offset = content.index("MIDDLE_TOKEN")
        middle = await provider.invoke(
            ToolCall("read-middle", "web.read", {
                "document_ref": document_ref,
                "offset": middle_offset,
                "max_chars": 12,
            }),
            context,
        )
        self.assertTrue(middle.ok, middle.to_data())
        self.assertEqual(middle.data["content"], "MIDDLE_TOKEN")

        tail = await provider.invoke(
            ToolCall("read-tail", "web.read", {
                "document_ref": document_ref,
                "offset": len(content) - 4,
            }),
            context,
        )
        self.assertTrue(tail.ok, tail.to_data())
        self.assertEqual(tail.data["content"], "TAIL")
        self.assertIsNone(tail.data["next_offset"])
        self.assertFalse(tail.data["truncated"])

        # Paging never refetches: the document was read once, from the cache.
        self.assertEqual(provider._fetch_transport.calls, [self.URL])

    async def test_read_rejects_unknown_refs_foreign_scopes_and_bad_offsets(self):
        content = "one two three"
        provider = await self._provider({
            self.URL: FetchResponse(
                200, self.URL, {"Content-Type": "text/plain"}, content.encode(),
            ),
        })
        context = self._context("task-a", "turn-a")
        opened = await provider.invoke(
            ToolCall("open", "web.open", {"url": self.URL}), context,
        )
        document_ref = opened.data["document_ref"]

        unknown = await provider.invoke(
            ToolCall("read-unknown", "web.read", {"document_ref": "webdoc:missing"}),
            context,
        )
        self.assertFalse(unknown.ok)
        self.assertEqual(unknown.error_code, "DOCUMENT_REF_NOT_FOUND")

        beyond = await provider.invoke(
            ToolCall("read-beyond", "web.read", {
                "document_ref": document_ref, "offset": len(content) + 5,
            }),
            context,
        )
        self.assertFalse(beyond.ok)

        other_scope = await provider.invoke(
            ToolCall("read-other", "web.read", {"document_ref": document_ref}),
            self._context("task-a", "turn-b"),
        )
        self.assertFalse(other_scope.ok)
        self.assertEqual(other_scope.error_code, "DOCUMENT_REF_NOT_FOUND")

    async def test_open_decodes_gzip_and_deflate_documents_with_bounded_output(self):
        content = b"# Spec\n\nCompressed but searchable content.\n"
        raw_deflater = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        encodings = (
            ("gzip", "gzip", gzip.compress(content)),
            ("deflate", "deflate", zlib.compress(content)),
            (
                "raw-deflate", "deflate",
                raw_deflater.compress(content) + raw_deflater.flush(),
            ),
        )
        context = self._context("task-a", "turn-a")

        for name, encoding, payload in encodings:
            with self.subTest(encoding=name):
                provider = await self._provider({
                    self.URL: FetchResponse(
                        200, self.URL,
                        {
                            "Content-Type": "text/markdown",
                            "Content-Encoding": encoding,
                        },
                        payload,
                    ),
                })
                result = await provider.invoke(
                    ToolCall(f"open-{name}", "web.open", {"url": self.URL}),
                    context,
                )

                self.assertTrue(result.ok, result.to_data())
                self.assertEqual(result.data["content"], content.decode())
                self.assertEqual(
                    provider._fetch_transport.headers[-1]["Accept-Encoding"],
                    "gzip, deflate, identity",
                )

    async def test_open_rejects_unsafe_or_unsupported_compressed_documents(self):
        context = self._context("task-a", "turn-a")
        oversized = gzip.compress(b"x" * (_MAX_DECOMPRESSED_FETCH_BYTES + 1))
        cases = {
            "br": (
                FetchResponse(
                    200, self.URL,
                    {"Content-Type": "text/markdown", "Content-Encoding": "br"},
                    b"opaque",
                ),
                "UNSUPPORTED_CONTENT_ENCODING",
            ),
            "oversized-gzip": (
                FetchResponse(
                    200, self.URL,
                    {"Content-Type": "text/markdown", "Content-Encoding": "gzip"},
                    oversized,
                ),
                "DECOMPRESSED_RESPONSE_TOO_LARGE",
            ),
            "truncated-gzip": (
                FetchResponse(
                    200, self.URL,
                    {"Content-Type": "text/markdown", "Content-Encoding": "gzip"},
                    gzip.compress(b"incomplete"),
                    truncated=True,
                ),
                "COMPRESSED_RESPONSE_TRUNCATED",
            ),
        }

        for name, (response, error_code) in cases.items():
            with self.subTest(case=name):
                provider = await self._provider({self.URL: response})
                result = await provider.invoke(
                    ToolCall(f"open-{name}", "web.open", {"url": self.URL}),
                    context,
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, error_code)

    async def test_open_accepts_the_document_ref_from_an_earlier_fetch(self):
        provider = await self._provider({
            self.URL: FetchResponse(
                200, self.URL, {"Content-Type": "text/markdown"},
                b"# Spec\n\nSearchable content.\n",
            ),
        })
        context = self._context("task-a", "turn-a")
        fetched = await provider.invoke(
            ToolCall("fetch", "web.fetch_markdown", {"url": self.URL}), context,
        )
        self.assertTrue(fetched.ok, fetched.to_data())

        # Models routinely feed web.fetch_markdown's document_ref back into
        # web.open as ref_id. That means "re-open this document", so it must
        # succeed instead of reporting an unknown search ref.
        result = await provider.invoke(
            ToolCall("reopen", "web.open", {"ref_id": fetched.data["document_ref"]}),
            context,
        )

        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(result.data["canonical_url"], self.URL)

    async def test_find_only_reads_the_opened_document_and_bounds_matches(self):
        provider = await self._provider({
            self.URL: FetchResponse(
                200, self.URL, {"Content-Type": "text/plain"},
                b"Alpha one\nalpha two\nalpha three\n",
            ),
        })
        context = self._context("task-a", "turn-a")
        opened = await provider.invoke(
            ToolCall("open-url", "web.open", {"url": self.URL}), context,
        )
        self.assertTrue(opened.ok, opened.to_data())

        found = await provider.invoke(
            ToolCall("find", "web.find_in_page", {
                "document_ref": opened.data["document_ref"],
                "pattern": "alpha",
                "max_matches": 2,
            }), context,
        )

        self.assertTrue(found.ok, found.to_data())
        self.assertEqual(found.data["match_count"], 3)
        self.assertEqual(found.data["returned_count"], 2)
        self.assertTrue(found.data["truncated"])
        self.assertEqual([item["line"] for item in found.data["matches"]], [1, 2])
        self.assertEqual([item["ordinal"] for item in found.data["matches"]], [1, 2])
        self.assertEqual([item["offset"] for item in found.data["matches"]], [0, 10])
        self.assertEqual([item["snippet"] for item in found.data["matches"]], [
            "Alpha one", "alpha two",
        ])
        self.assertEqual(provider._fetch_transport.calls, [self.URL])
        self.assertEqual(found.data["content_hash"], opened.data["content_hash"])
        self.assertEqual(found.fact_descriptor.artifact_refs[0], opened.data["document_ref"])
        self.assertEqual(
            found.fact_descriptor.materialization,
            ToolResultMaterialization.LOCATOR,
        )

    async def test_refs_and_documents_do_not_cross_task_or_turn_scopes(self):
        provider = await self._provider({
            self.URL: FetchResponse(
                200, self.URL, {"Content-Type": "text/plain"}, b"isolated\n",
            ),
        })
        first_context = self._context("task-a", "turn-a")
        second_context = self._context("task-a", "turn-b")
        ref_id = provider._store_reference(("task-a", "turn-a"), self.URL)
        opened = await provider.invoke(
            ToolCall("open", "web.open", {"ref_id": ref_id}), first_context,
        )
        self.assertTrue(opened.ok, opened.to_data())

        wrong_ref = await provider.invoke(
            ToolCall("open-other", "web.open", {"ref_id": ref_id}), second_context,
        )
        wrong_document = await provider.invoke(
            ToolCall("find-other", "web.find_in_page", {
                "document_ref": opened.data["document_ref"], "pattern": "isolated",
            }), second_context,
        )

        self.assertFalse(wrong_ref.ok)
        self.assertEqual(wrong_ref.error_code, "SEARCH_REF_NOT_FOUND")
        self.assertFalse(wrong_document.ok)
        self.assertEqual(wrong_document.error_code, "DOCUMENT_REF_NOT_FOUND")

    async def test_document_cache_evicts_by_entry_bound_and_stop_clears_state(self):
        first = "https://docs.example.test/first"
        second = "https://docs.example.test/second"
        provider = await self._provider({
            first: FetchResponse(200, first, {"Content-Type": "text/plain"}, b"first"),
            second: FetchResponse(200, second, {"Content-Type": "text/plain"}, b"second"),
        }, document_cache_max_entries=1)
        context = self._context("task-a", "turn-a")
        opened_first = await provider.invoke(
            ToolCall("open-first", "web.open", {"url": first}), context,
        )
        await provider.invoke(ToolCall("open-second", "web.open", {"url": second}), context)
        evicted = await provider.invoke(
            ToolCall("find-first", "web.find_in_page", {
                "document_ref": opened_first.data["document_ref"], "pattern": "first",
            }), context,
        )

        self.assertFalse(evicted.ok)
        self.assertEqual(evicted.error_code, "DOCUMENT_REF_NOT_FOUND")
        await provider.stop(datetime.now(timezone.utc))
        self.assertEqual(provider._document_cache, {})
        self.assertEqual(provider._reference_cache, {})

    async def test_document_cache_ttl_expires_without_a_network_refetch(self):
        provider = await self._provider({
            self.URL: FetchResponse(
                200, self.URL, {"Content-Type": "text/plain"}, b"expired",
            ),
        }, document_cache_ttl_seconds=0.5)
        context = self._context("task-a", "turn-a")
        opened = await provider.invoke(
            ToolCall("open", "web.open", {"url": self.URL}), context,
        )
        key = (("task-a", "turn-a"), opened.data["document_ref"])
        object.__setattr__(provider._document_cache[key], "stored_at", 0.0)
        expired = await provider.invoke(
            ToolCall("find", "web.find_in_page", {
                "document_ref": opened.data["document_ref"],
                "pattern": "expired",
            }), context,
        )

        self.assertTrue(opened.ok, opened.to_data())
        self.assertFalse(expired.ok)
        self.assertEqual(expired.error_code, "DOCUMENT_REF_NOT_FOUND")
        self.assertEqual(provider._fetch_transport.calls, [self.URL])

    async def test_search_results_receive_stable_scoped_refs(self):
        provider = await self._provider({})
        provider._pipeline.search = Mock(return_value=RetrievalSearchResponse(
            query="spec", provider="fixture", telemetry=[], cache_hit=False,
            results=[RetrievalResult(
                title="Spec", url=self.URL, snippet="result", provider="fixture",
            )],
        ))
        context = self._context("task-a", "turn-a")
        first = await provider.invoke(
            ToolCall("search-1", "web.search", {"query": "spec"}), context,
        )
        second = await provider.invoke(
            ToolCall("search-2", "web.search", {"query": "spec"}), context,
        )

        self.assertTrue(first.ok, first.to_data())
        self.assertEqual(
            first.data["results"][0]["ref_id"], second.data["results"][0]["ref_id"],
        )

class GitHubSourceResolveTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _context(task_id: str, turn_id: str):
        return type("_Context", (), {"task_id": task_id, "turn_id": turn_id})()

    async def _provider(self, **kwargs):
        provider = NetworkToolProvider(
            fetch_transport=_FakeFetchTransport({}),
            address_resolver=_public_resolver,
            **kwargs,
        )
        await provider.start(AdapterContext({}, lambda *_: None))
        self.addAsyncCleanup(provider.stop, datetime.now(timezone.utc))
        return provider

    async def test_resolves_supported_github_urls_to_canonical_targets(self):
        provider = await self._provider()
        context = self._context("task-a", "turn-a")
        cases = [
            (
                "https://github.com/owner/repo/",
                "repository", None, None, None,
                "source.github.list_tree", {"repo": "owner/repo", "ref": "HEAD"},
            ),
            (
                "https://github.com/owner/repo/blob/" + "A" * 40 + "/src/a.py",
                "blob", "a" * 40, "src/a.py", None,
                "source.github.get_file",
                {"repo": "owner/repo", "ref": "a" * 40, "path": "src/a.py"},
            ),
            (
                "https://github.com/owner/repo/tree/main/src",
                "tree", "main", "src", None,
                "source.github.list_tree",
                {"repo": "owner/repo", "ref": "main", "path": "src"},
            ),
            (
                "https://github.com/owner/repo/issues/42",
                "issue", None, None, 42, "source.github.get_issue",
                {"repo": "owner/repo", "number": 42},
            ),
            (
                "https://github.com/owner/repo/pull/7",
                "pull", None, None, 7, "source.github.get_pull_request",
                {"repo": "owner/repo", "number": 7},
            ),
        ]

        for index, (
            url, kind, ref, path, number, target_tool, target_arguments,
        ) in enumerate(cases):
            with self.subTest(url=url):
                result = await provider.invoke(
                    ToolCall(f"resolve-{index}", "source.github.resolve", {"url": url}),
                    context,
                )
                self.assertTrue(result.ok, result.to_data())
                self.assertEqual(result.data["repo"], "owner/repo")
                self.assertEqual(result.data["kind"], kind)
                self.assertEqual(result.data["ref"], ref)
                self.assertEqual(result.data["path"], path)
                self.assertEqual(result.data["number"], number)
                self.assertEqual(result.data["target_tool"], target_tool)
                self.assertEqual(result.data["target_arguments"], target_arguments)
                self.assertEqual(
                    result.data["ref_is_pinned"], bool(ref and len(ref) == 40)
                )
                self.assertEqual(
                    result.data["provenance"]["source"], "source.github.resolve"
                )
                self.assertFalse(result.data["provenance"]["network_egress"])
                self.assertEqual(
                    result.fact_descriptor.authority, "derived_github_url"
                )
                self.assertEqual(
                    result.fact_descriptor.scope[:2],
                    ("task:task-a", "turn:turn-a"),
                )
                self.assertEqual(
                    result.fact_descriptor.artifact_refs,
                    (result.data["canonical_url"],),
                )

        self.assertEqual(provider._fetch_transport.calls, [])

    async def test_search_ref_uses_current_scope_and_ttl_without_egress(self):
        provider = await self._provider(document_cache_ttl_seconds=0.5)
        url = "https://github.com/owner/repo/blob/main/src/a.py"
        first_context = self._context("task-a", "turn-a")
        ref_id = provider._store_reference(("task-a", "turn-a"), url)

        resolved = await provider.invoke(
            ToolCall("resolve-ref", "source.github.resolve", {"search_ref": ref_id}),
            first_context,
        )
        wrong_scope = await provider.invoke(
            ToolCall("resolve-other", "source.github.resolve", {"search_ref": ref_id}),
            self._context("task-a", "turn-b"),
        )
        key = (("task-a", "turn-a"), ref_id)
        object.__setattr__(provider._reference_cache[key], "stored_at", 0.0)
        expired = await provider.invoke(
            ToolCall("resolve-expired", "source.github.resolve", {"search_ref": ref_id}),
            first_context,
        )

        self.assertTrue(resolved.ok, resolved.to_data())
        self.assertEqual(resolved.data["provenance"]["input_kind"], "search_ref")
        self.assertEqual(resolved.data["provenance"]["search_ref"], ref_id)
        self.assertFalse(wrong_scope.ok)
        self.assertEqual(wrong_scope.error_code, "SEARCH_REF_NOT_FOUND")
        self.assertFalse(expired.ok)
        self.assertEqual(expired.error_code, "SEARCH_REF_NOT_FOUND")
        self.assertEqual(provider._fetch_transport.calls, [])

    async def test_rejects_invalid_urls_and_never_egresses(self):
        provider = await self._provider()
        context = self._context("task-a", "turn-a")
        invalid_urls = [
            "http://github.com/owner/repo",
            "https://evil.example/owner/repo",
            "https://user@github.com/owner/repo",
            "https://github.com:443/owner/repo",
            "https://github.com/owner/repo?tab=readme",
            "https://github.com/owner/repo#readme",
            "https://github.com/owner/repo/blob/main/%2e%2e/secret.py",
            "https://github.com/owner/../repo",
            "https://github.com/owner/repo/blob/main",
            "https://github.com/owner/repo/commit/abc",
        ]
        with patch("tsm_agt.adapters.builtin.network_tools.urlopen", side_effect=AssertionError):
            for index, url in enumerate(invalid_urls):
                with self.subTest(url=url):
                    result = await provider.invoke(
                        ToolCall(f"bad-{index}", "source.github.resolve", {"url": url}),
                        context,
                    )
                    self.assertFalse(result.ok)
                    self.assertEqual(result.error_code, "INVALID_GITHUB_URL")
            ambiguous = await provider.invoke(
                ToolCall(
                    "ambiguous", "source.github.resolve",
                    {"url": "https://github.com/owner/repo/tree/refs/heads/main"},
                ),
                context,
            )
            both = await provider.invoke(
                ToolCall(
                    "both", "source.github.resolve",
                    {"url": "https://github.com/owner/repo", "search_ref": "search:x"},
                ),
                context,
            )

        self.assertFalse(ambiguous.ok)
        self.assertEqual(ambiguous.error_code, "AMBIGUOUS_GITHUB_REF_PATH")
        self.assertFalse(both.ok)
        self.assertEqual(both.error_code, "INVALID_PARAM")
        self.assertEqual(provider._fetch_transport.calls, [])
