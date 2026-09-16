from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.builtin import NetworkToolProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import CoreToolPolicy, PolicyAction, ProjectTrustLevel, TaskState
from tsm_agt.ports import (
    AdapterContext, EvidenceQuestion, ToolCall, ToolEffect,
)


class NetworkToolProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = NetworkToolProvider()
        await self.provider.start(AdapterContext({}, lambda *_: None))

    async def asyncTearDown(self) -> None:
        from datetime import datetime
        await self.provider.stop(datetime.now())

    async def test_default_contract_exposes_safe_search_not_arbitrary_fetch(self):
        tools = await self.provider.list_tools()
        self.assertEqual(
            [tool.name for tool in tools],
            ["web.search", "content.summarize"],
        )
        search = tools[0]
        self.assertTrue(search.requires_network)
        self.assertEqual(search.effect, ToolEffect.OBSERVE)
        self.assertIn("search query", search.data_transmission)

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
        self.assertEqual(result.data["provider"], "duckduckgo-html")
        self.assertEqual(
            result.data["results"][0]["url"], "https://example.com/nba"
        )
        self.assertEqual(
            result.data["results"][0]["snippet"],
            "Latest playoff and trade updates from around the league.",
        )
        self.assertTrue(result.meta["untrusted_data"])

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
