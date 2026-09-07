from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.local_process import LocalProcessExecutor
from tsm_agt.adapters.windows_process import WindowsProcessExecutor
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.ports import (
    AdapterContext, ProcessExecutorPort, ProcessExitStatus, ProcessStartRequest,
)


def native_executor() -> ProcessExecutorPort:
    return WindowsProcessExecutor() if os.name == "nt" else LocalProcessExecutor()


class NativeProcessExecutorPlatformContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_composition_selects_native_process_executor(self) -> None:
        application = compose_fixture_application()
        executor = application.registry.require(ProcessExecutorPort)
        expected = (
            "builtin.windows-process-executor"
            if os.name == "nt" else "builtin.local-process-executor"
        )
        self.assertEqual(executor.descriptor.adapter_id, expected)

    async def test_process_lifecycle_and_environment_contract(self) -> None:
        executor = native_executor()
        await executor.start(AdapterContext({}, lambda _type, _payload: None))
        with tempfile.TemporaryDirectory() as directory:
            environment = executor.prepare_environment({"TSM_SAFE_FLAG": "yes"})
            handle = await executor.start_process(ProcessStartRequest(
                "process-platform-contract",
                (sys.executable, "-c", "print('platform-ok')"),
                Path(directory).resolve(), environment,
            ))
            result = await executor.wait_process(handle)
            self.assertEqual(result.status, ProcessExitStatus.EXITED)
            self.assertEqual(result.stdout.text, "platform-ok\n")
        await executor.stop(datetime.now())

    async def test_windows_environment_names_are_case_insensitive(self) -> None:
        executor = WindowsProcessExecutor()
        environment = executor.prepare_environment({
            "Path": "first", "PATH": "second", "tsm_flag": "yes",
        })
        folded = [name.casefold() for name in environment]
        self.assertEqual(folded.count("path"), 1)
        self.assertEqual(environment["PATH"], "second")
        self.assertIn("tsm_flag", environment)

    async def test_windows_controlled_batch_launcher_executes(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows controlled batch launcher contract")
        executor = native_executor()
        await executor.start(AdapterContext({}, lambda _type, _payload: None))
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                launcher = root / "gradlew.bat"
                launcher.write_text("@echo off\r\necho batch-ok\r\n")
                handle = await executor.start_process(ProcessStartRequest(
                    "process-batch-contract", (str(launcher),), root,
                    executor.prepare_environment({}),
                ))
                result = await executor.wait_process(handle)
                self.assertEqual(result.status, ProcessExitStatus.EXITED)
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(result.stdout.text.strip(), "batch-ok")
        finally:
            await executor.stop(datetime.now())

    async def test_windows_job_object_kills_spawned_child_tree(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows Job Object process-tree contract")
        executor = native_executor()
        await executor.start(AdapterContext({}, lambda _type, _payload: None))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "child.pid"
            code = (
                "import pathlib,subprocess,sys,time;"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid));"
                "time.sleep(30)"
            )
            handle = await executor.start_process(ProcessStartRequest(
                "process-job-tree", (sys.executable, "-c", code), root,
                executor.prepare_environment({}),
            ))
            for _ in range(200):
                if pid_file.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(pid_file.exists())
            child_pid = int(pid_file.read_text())
            await executor.stop_process(handle, 0.05, ProcessExitStatus.CANCELLED)
            await asyncio.sleep(0.05)
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = (
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
            )
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            process = kernel32.OpenProcess(0x1000, False, child_pid)
            if process:
                exit_code = wintypes.DWORD()
                self.assertTrue(kernel32.GetExitCodeProcess(
                    process, ctypes.byref(exit_code)
                ))
                kernel32.CloseHandle(process)
                self.assertNotEqual(
                    exit_code.value, 259,
                    "Job Object left a spawned child running",
                )
        await executor.stop(datetime.now())


if __name__ == "__main__":
    unittest.main()
