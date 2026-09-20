from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tsm_agt.adapters.builtin import NetworkToolProvider
from tsm_agt.adapters.builtin.network_tools import (
    FetchResponse, _BingNewsRssProvider, _DuckDuckGoLiteProvider,
    _RssNewsProvider, _WikipediaProvider,
)
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import CoreToolPolicy, PolicyAction, ProjectTrustLevel, TaskState
from tsm_agt.ports import (
    AdapterContext, EvidenceQuestion, ToolCall, ToolEffect,
)


class _FakeFetchTransport:
    def __init__(self, responses: dict[str, FetchResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.pinned_addresses: list[str | None] = []

    def fetch(
        self, url, *, headers, timeout_seconds, max_bytes, pinned_address=None,
    ):
        self.calls.append(url)
        self.pinned_addresses.append(pinned_address)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def _public_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("8.8.8.8",)


class NetworkToolProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = NetworkToolProvider()
        await self.provider.start(AdapterContext({}, lambda *_: None))

    async def asyncTearDown(self) -> None:
        from datetime import datetime
        await self.provider.stop(datetime.now())

    async def test_default_contract_advertises_safe_document_fetch(self):
        tools = await self.provider.list_tools()
        self.assertEqual(
            [tool.name for tool in tools],
            ["web.search", "web.fetch_markdown", "content.summarize"],
        )
        fetch = tools[1]
        self.assertTrue(fetch.requires_network)
        self.assertTrue(fetch.is_read_only)
        self.assertEqual(fetch.effect, ToolEffect.OBSERVE)
        self.assertIn("HTTP(S) URL", fetch.data_transmission)

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

        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(result.data["provider"], "degraded-no-results")
        self.assertEqual(result.data["results"], [])
        statuses = {entry["status"] for entry in result.data["telemetry"]}
        # Every configured provider has to report, and none may raise out.
        self.assertEqual(len(result.data["telemetry"]), 6)
        self.assertTrue(statuses <= {"empty", "error"}, statuses)

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
