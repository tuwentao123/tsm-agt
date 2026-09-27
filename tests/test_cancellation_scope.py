"""Phase 2: in-process CancellationScope (task-cancellation-spec §5.1)."""

from __future__ import annotations

import asyncio
import threading
import unittest

from tsm_agt.core import (
    CANCELLATION_METRICS,
    CancellationScope,
    note_scope_closed,
)


class CancellationScopeTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_all_runs_each_cleanup_once(self) -> None:
        scope = CancellationScope()
        calls: list[str] = []

        async def one() -> None:
            calls.append("one")

        scope.register_cleanup("one", one)
        first = await scope.close_all()
        second = await scope.close_all()
        self.assertEqual(first.closed, ["one"])
        self.assertEqual(calls, ["one"])
        self.assertIs(first, second)  # idempotent report

    async def test_timeout_is_recorded_not_raised(self) -> None:
        scope = CancellationScope()

        async def slow() -> None:
            await asyncio.sleep(5)

        scope.register_cleanup("slow", slow)
        report = await scope.close_all(timeout_seconds=0.05)
        self.assertEqual(report.timed_out, ["slow"])
        self.assertEqual(report.closed, [])

    async def test_failure_is_recorded_not_raised(self) -> None:
        scope = CancellationScope()

        async def boom() -> None:
            raise RuntimeError("boom")

        scope.register_cleanup("boom", boom)
        report = await scope.close_all()
        self.assertEqual(report.failed, [("boom", "RuntimeError")])

    async def test_thread_only_signals_never_joins(self) -> None:
        scope = CancellationScope()
        stopped = threading.Event()
        scope.register_thread(stopped, name="worker")
        await scope.close_all()
        self.assertTrue(stopped.is_set())

    async def test_stream_and_generator_are_closed(self) -> None:
        scope = CancellationScope()
        closed: list[str] = []

        class _Stream:
            async def aclose(self) -> None:
                closed.append("stream")

        class _Generator:
            async def aclose(self) -> None:
                closed.append("gen")

        scope.register_stream(_Stream(), name="stream")
        scope.register_generator(_Generator(), name="gen")
        report = await scope.close_all()
        self.assertEqual(set(closed), {"stream", "gen"})
        self.assertEqual(set(report.closed), {"stream", "gen"})

    async def test_late_registration_is_rejected_and_cleaned(self) -> None:
        scope = CancellationScope()
        await scope.close_all()
        cleaned: list[str] = []

        async def late() -> None:
            cleaned.append("late")

        self.assertFalse(scope.register_cleanup("late", late))
        await asyncio.sleep(0)
        self.assertEqual(cleaned, ["late"])


    async def test_close_all_logs_and_counts_cleanup(self) -> None:
        scope = CancellationScope()

        async def one() -> None:
            return None

        scope.register_cleanup("one", one)
        before = CANCELLATION_METRICS.cleanup_completed
        with self.assertLogs("tsm_agt.cancellation", level="INFO") as captured:
            report = await scope.close_all()
        self.assertEqual(report.closed, ["one"])
        self.assertEqual(CANCELLATION_METRICS.cleanup_completed, before + 1)
        self.assertTrue(any(
            "cleanup_completed" in line for line in captured.output
        ))


    async def test_scope_close_logs_timeout_and_tracks_duration(self) -> None:
        scope = CancellationScope()

        async def slow() -> None:
            await asyncio.sleep(5)

        scope.register_cleanup("slow", slow)
        with self.assertLogs("tsm_agt.cancellation", level="WARNING") as captured:
            report = await scope.close_all(timeout_seconds=0.05)
            note_scope_closed(report)
        self.assertEqual(report.timed_out, ["slow"])
        self.assertTrue(any(
            "cleanup_timeout" in line for line in captured.output
        ))
        self.assertGreaterEqual(
            CANCELLATION_METRICS.cleanup_duration_seconds_total, 0.0,
        )


if __name__ == "__main__":
    unittest.main()
