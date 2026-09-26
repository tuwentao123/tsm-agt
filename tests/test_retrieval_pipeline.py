from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.builtin import NetworkToolProvider
from tsm_agt.ports import AdapterContext, ToolCall, ToolInvocationContext
from tsm_agt.retrieval.pipeline import RetrievalPipeline
from tsm_agt.retrieval.providers import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS, SequentialProviderRunner,
)
from tsm_agt.retrieval.schemas import (
    ProviderTimeoutError,
    RetrievalResult,
    freshness_cutoff,
    normalize_published_at,
    recency_sort_key,
)


def _result(url: str = "https://example.com/a") -> RetrievalResult:
    return RetrievalResult(title="t", url=url, snippet="s", provider="fake")


class _FakeProvider:
    def __init__(
        self, name: str, *, results: tuple[RetrievalResult, ...] = (),
        delay: float = 0.0, error: BaseException | None = None,
    ) -> None:
        self.name = name
        self._results = list(results)
        self._delay = delay
        self._error = error
        self.calls: list[float] = []
        self.freshness_calls: list[str] = []

    def search(
        self, query: str, top_k: int, *,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        freshness: str = "any",
    ) -> list[RetrievalResult]:
        self.calls.append(timeout_seconds)
        self.freshness_calls.append(freshness)
        if self._delay:
            time.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._results[:top_k]


class SequentialProviderRunnerTest(unittest.TestCase):
    def test_provider_timeout_is_recorded_as_timeout_status(self):
        provider = _FakeProvider(
            "slow", error=ProviderTimeoutError("slow", 6.0)
        )

        execution = SequentialProviderRunner().execute([provider], "q", 3)

        self.assertEqual(execution.results, [])
        self.assertEqual(execution.provider, "degraded-no-results")
        self.assertEqual(
            [entry.status for entry in execution.telemetry], ["timeout"]
        )

    def test_generic_provider_failure_stays_an_error(self):
        provider = _FakeProvider("broken", error=RuntimeError("boom"))

        execution = SequentialProviderRunner().execute([provider], "q", 3)

        self.assertEqual(
            [entry.status for entry in execution.telemetry], ["error"]
        )

    def test_provider_timeout_is_capped_by_remaining_budget(self):
        provider = _FakeProvider("fast", results=(_result(),))
        deadline = time.monotonic() + 0.75

        SequentialProviderRunner(provider_timeout_seconds=10.0).execute(
            [provider], "q", 3, deadline=deadline,
        )

        self.assertEqual(len(provider.calls), 1)
        self.assertGreater(provider.calls[0], 0.0)
        self.assertLessEqual(provider.calls[0], 0.75)

    def test_exhausted_budget_skips_later_providers(self):
        slow = _FakeProvider("slow", delay=0.7)
        later = _FakeProvider("later", results=(_result(),))
        deadline = time.monotonic() + 1.0

        execution = SequentialProviderRunner(
            provider_timeout_seconds=1.0
        ).execute([slow, later], "q", 3, deadline=deadline)

        self.assertEqual(later.calls, [])
        self.assertEqual(
            [entry.status for entry in execution.telemetry],
            ["empty", "skipped"],
        )

    def test_without_deadline_the_configured_provider_timeout_applies(self):
        provider = _FakeProvider("fast", results=(_result(),))

        SequentialProviderRunner(provider_timeout_seconds=4.0).execute(
            [provider], "q", 3,
        )

        self.assertEqual(provider.calls, [4.0])


class RetrievalPipelineTest(unittest.TestCase):
    def test_cache_short_circuits_before_any_provider_budget(self):
        provider = _FakeProvider("fast", results=(_result(),))
        pipeline = RetrievalPipeline([provider])

        first = pipeline.search("nba", 3, deadline=time.monotonic() + 5)
        second = pipeline.search("nba", 3, deadline=time.monotonic() + 5)

        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(len(provider.calls), 1)

    def test_freshness_is_threaded_to_providers(self):
        provider = _FakeProvider("fast", results=(_result(),))

        SequentialProviderRunner().execute(
            [provider], "q", 3, freshness="day",
        )

        self.assertEqual(provider.freshness_calls, ["day"])

    def test_freshness_participates_in_the_cache_key(self):
        provider = _FakeProvider("fast", results=(_result(),))
        pipeline = RetrievalPipeline([provider])

        pipeline.search("nba", 3, freshness="day")
        pipeline.search("nba", 3, freshness="week")

        self.assertEqual(len(provider.calls), 2)

    def test_unknown_freshness_value_reaches_provider_unchanged(self):
        # Validation lives at the Tool boundary; the pipeline must not silently
        # coerce an unknown window into "any".
        provider = _FakeProvider("fast", results=(_result(),))

        SequentialProviderRunner().execute([provider], "q", 3, freshness="year")

        self.assertEqual(provider.freshness_calls, ["year"])


class FreshnessHelperTest(unittest.TestCase):
    def test_normalize_accepts_rfc822_and_iso(self):
        rfc = normalize_published_at("Fri, 25 Sep 2026 15:40:00 GMT")
        iso = normalize_published_at("2026-09-25T15:40:00Z")
        self.assertIsNotNone(rfc)
        self.assertEqual(rfc, iso)
        self.assertTrue(rfc.startswith("2026-09-25T15:40:00"))

    def test_normalize_rejects_missing_or_junk(self):
        self.assertIsNone(normalize_published_at(None))
        self.assertIsNone(normalize_published_at(""))
        self.assertIsNone(normalize_published_at("not a date"))

    def test_recency_sort_prefers_newest_and_keeps_undated_last(self):
        older = RetrievalResult(
            "old", "https://e/old", "s", "p",
            published_at="2026-09-01T00:00:00+00:00",
        )
        newer = RetrievalResult(
            "new", "https://e/new", "s", "p",
            published_at="2026-09-25T00:00:00+00:00",
        )
        undated = RetrievalResult("x", "https://e/x", "s", "p")

        ordered = sorted([older, undated, newer], key=recency_sort_key)

        self.assertEqual([r.title for r in ordered], ["new", "old", "x"])

    def test_freshness_cutoff_only_for_known_windows(self):
        now = datetime(2026, 9, 26, tzinfo=timezone.utc)
        self.assertEqual(
            freshness_cutoff("day", now=now),
            datetime(2026, 9, 25, tzinfo=timezone.utc),
        )
        self.assertIsNone(freshness_cutoff("any", now=now))
        self.assertIsNone(freshness_cutoff("year", now=now))


class NetworkToolSearchBudgetTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = NetworkToolProvider()
        await self.provider.start(AdapterContext({}, lambda *_: None))

    async def asyncTearDown(self) -> None:
        await self.provider.stop(datetime.now(timezone.utc))

    def test_default_provider_timeout_is_configurable(self):
        self.assertEqual(
            self.provider._pipeline._runner._provider_timeout_seconds,
            DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        )
        custom = NetworkToolProvider(search_provider_timeout_seconds=2.5)
        self.assertEqual(
            custom._pipeline._runner._provider_timeout_seconds, 2.5
        )

    def test_expired_deadline_skips_providers_without_network(self):
        data = self.provider._web_search(
            {"query": "nba", "top_k": 3}, time.monotonic(),
        )

        self.assertEqual(data["provider"], "degraded-no-results")
        self.assertEqual(data["results"], [])
        self.assertEqual(
            {entry["status"] for entry in data["telemetry"]}, {"skipped"},
        )

    async def test_invoke_derives_search_deadline_from_context(self):
        captured: dict[str, float] = {}

        def fake_web_search(_arguments, deadline=None):
            captured["deadline"] = deadline
            return {
                "query": "q", "provider": "none", "result_count": 0,
                "results": [], "telemetry": [], "cache_hit": False,
            }

        context = ToolInvocationContext(
            invocation_id="inv-1", task_id="task-1", turn_id="turn-1",
            workspace=Path("."),
            deadline=datetime.now(timezone.utc) + timedelta(seconds=3),
        )
        with patch.object(self.provider, "_web_search", side_effect=fake_web_search):
            result = await self.provider.invoke(
                ToolCall("c1", "web.search", {"query": "q"}), context,
            )

        self.assertTrue(result.ok, result.to_data())
        remaining = captured["deadline"] - time.monotonic()
        self.assertGreater(remaining, 0.0)
        self.assertLessEqual(remaining, 3.0)


if __name__ == "__main__":
    unittest.main()
