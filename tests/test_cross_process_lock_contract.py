from __future__ import annotations

import asyncio
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.posix_lock import PosixCrossProcessLock
from tsm_agt.adapters.windows_lock import WindowsCrossProcessLock
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.ports import (
    AdapterContext, CrossProcessLockPort, HealthState,
)


def native_lock_adapter() -> CrossProcessLockPort:
    return (
        WindowsCrossProcessLock()
        if os.name == "nt"
        else PosixCrossProcessLock()
    )


def _child_hold_native_lock(lock_file: str, ready, release) -> None:
    async def run() -> None:
        adapter = native_lock_adapter()
        context = AdapterContext(
            config={}, emit_event=lambda _type, _payload: None
        )
        await adapter.start(context)
        lease = await adapter.acquire(Path(lock_file))
        ready.set()
        try:
            await asyncio.to_thread(release.wait, 10)
        finally:
            await adapter.release(lease)
            await adapter.stop(None)  # type: ignore[arg-type]

    asyncio.run(run())


class NativeCrossProcessLockContractTest(unittest.IsolatedAsyncioTestCase):
    """The same behavioral contract runs natively on macOS and Windows."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.lock_file = Path(self.temporary.name) / "contract.lock"
        self.first = native_lock_adapter()
        self.second = native_lock_adapter()
        context = AdapterContext(config={}, emit_event=lambda _type, _payload: None)
        await self.first.start(context)
        await self.second.start(context)

    async def asyncTearDown(self) -> None:
        await self.first.stop(None)  # type: ignore[arg-type]
        await self.second.stop(None)  # type: ignore[arg-type]
        self.temporary.cleanup()

    async def test_lifecycle_and_exclusive_lease(self) -> None:
        self.assertEqual((await self.first.health()).state, HealthState.HEALTHY)
        first_lease = await self.first.acquire(self.lock_file)
        waiting = asyncio.create_task(self.second.acquire(self.lock_file))
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())

        await self.first.release(first_lease)
        second_lease = await asyncio.wait_for(waiting, timeout=2)
        await self.second.release(second_lease)
        self.assertTrue(self.lock_file.exists())

    async def test_cancelled_wait_does_not_leak_handle_or_lock(self) -> None:
        first_lease = await self.first.acquire(self.lock_file)
        waiting = asyncio.create_task(self.second.acquire(self.lock_file))
        await asyncio.sleep(0.05)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        await self.first.release(first_lease)
        lease = await asyncio.wait_for(
            self.second.acquire(self.lock_file), timeout=2
        )
        await self.second.release(lease)

    async def test_unknown_or_double_release_is_rejected(self) -> None:
        with self.assertRaises(LookupError):
            await self.first.release("missing-lease")
        lease = await self.first.acquire(self.lock_file)
        await self.first.release(lease)
        with self.assertRaises(LookupError):
            await self.first.release(lease)

    async def test_independent_process_holds_the_same_native_lock(self) -> None:
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_child_hold_native_lock,
            args=(str(self.lock_file), ready, release),
        )
        process.start()
        try:
            self.assertTrue(await asyncio.to_thread(ready.wait, 8))
            waiting = asyncio.create_task(self.first.acquire(self.lock_file))
            await asyncio.sleep(0.08)
            self.assertFalse(waiting.done())
            release.set()
            lease = await asyncio.wait_for(waiting, timeout=5)
            await self.first.release(lease)
        finally:
            release.set()
            await asyncio.to_thread(process.join, 5)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
        self.assertEqual(process.exitcode, 0)

    async def test_process_crash_releases_native_os_lock(self) -> None:
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        never_release = context.Event()
        process = context.Process(
            target=_child_hold_native_lock,
            args=(str(self.lock_file), ready, never_release),
        )
        process.start()
        self.assertTrue(await asyncio.to_thread(ready.wait, 8))
        process.terminate()
        await asyncio.to_thread(process.join, 5)
        self.assertFalse(process.is_alive())
        lease = await asyncio.wait_for(
            self.first.acquire(self.lock_file), timeout=5
        )
        await self.first.release(lease)

    async def test_windows_case_alias_uses_same_lock_file(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows case-insensitive path contract")
        lease = await self.first.acquire(self.lock_file)
        alias = Path(str(self.lock_file).swapcase())
        waiting = asyncio.create_task(self.second.acquire(alias))
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())
        await self.first.release(lease)
        alias_lease = await asyncio.wait_for(waiting, timeout=2)
        await self.second.release(alias_lease)


class PlatformCrossProcessLockSelectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_composition_registers_native_platform_adapter(self) -> None:
        application = compose_fixture_application()
        adapter = application.registry.require(CrossProcessLockPort)
        expected = (
            "builtin.windows-cross-process-lock"
            if os.name == "nt"
            else "builtin.posix-cross-process-lock"
        )
        self.assertEqual(adapter.descriptor.adapter_id, expected)
        await application.registry.start_all()
        try:
            self.assertEqual((await adapter.health()).state, HealthState.HEALTHY)
        finally:
            await application.registry.stop_all()

    async def test_non_native_adapter_fails_closed(self) -> None:
        adapter = (
            PosixCrossProcessLock()
            if os.name == "nt"
            else WindowsCrossProcessLock()
        )
        expected = "fcntl.flock" if os.name == "nt" else "only on Windows"
        with self.assertRaisesRegex(RuntimeError, expected):
            await adapter.start(
                AdapterContext(config={}, emit_event=lambda _type, _payload: None)
            )
        self.assertEqual((await adapter.health()).state, HealthState.UNHEALTHY)


if __name__ == "__main__":
    unittest.main()
