from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.local_sandbox import LocalWorkspaceSandbox
from tsm_agt.adapters.posix_path import PosixWorkspacePath
from tsm_agt.adapters.windows_path import WindowsWorkspacePath
from tsm_agt.ports import AdapterContext, SandboxRequest


class LocalWorkspaceSandboxTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name).resolve()
        context = AdapterContext({}, lambda _type, _payload: None)
        self.path_service = (
            WindowsWorkspacePath() if sys.platform == "win32" else PosixWorkspacePath()
        )
        await self.path_service.start(context)
        self.sandbox = LocalWorkspaceSandbox(self.path_service)
        await self.sandbox.start(context)
        self.environment = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C.UTF-8",
        }

    async def asyncTearDown(self) -> None:
        await self.sandbox.stop(datetime.now())
        self.temporary.cleanup()

    async def test_allows_structured_engineering_executable(self) -> None:
        decision = await self.sandbox.authorize(SandboxRequest(
            (sys.executable, "-m", "unittest"),
            self.workspace, self.environment,
        ))
        self.assertTrue(decision.allowed)
        self.assertIn("not OS-isolated", decision.reason)

    async def test_denies_shells_even_when_shell_is_an_absolute_path(self) -> None:
        for executable in ("sh", "/bin/bash", "zsh", "pwsh"):
            with self.subTest(executable=executable):
                decision = await self.sandbox.authorize(SandboxRequest(
                    (executable, "-c", "echo unsafe"),
                    self.workspace, self.environment,
                ))
                self.assertFalse(decision.allowed)
                self.assertIn("shell", decision.reason)

    async def test_denies_unlisted_executable(self) -> None:
        decision = await self.sandbox.authorize(SandboxRequest(
            ("curl", "https://example.test"), self.workspace, self.environment
        ))
        self.assertFalse(decision.allowed)
        self.assertIn("allowlist", decision.reason)

    async def test_windows_suffixes_are_classified_without_open_shell(self) -> None:
        allowed = await self.sandbox.authorize(SandboxRequest(
            ("gradlew.bat", "test"), self.workspace, self.environment
        ))
        allowed_exe = await self.sandbox.authorize(SandboxRequest(
            ("git.exe", "status"), self.workspace, self.environment
        ))
        denied_shell = await self.sandbox.authorize(SandboxRequest(
            ("cmd.exe", "/c", "echo unsafe"),
            self.workspace, self.environment,
        ))
        denied_meta = await self.sandbox.authorize(SandboxRequest(
            ("gradlew.bat", "test&whoami"),
            self.workspace, self.environment,
        ))
        self.assertTrue(allowed.allowed)
        self.assertTrue(allowed_exe.allowed)
        self.assertFalse(denied_shell.allowed)
        self.assertFalse(denied_meta.allowed)

    async def test_denies_credential_like_environment_again(self) -> None:
        decision = await self.sandbox.authorize(SandboxRequest(
            (sys.executable, "-V"), self.workspace,
            {**self.environment, "ACCESS_TOKEN": "secret-value"},
        ))
        self.assertFalse(decision.allowed)
        self.assertIn("credential-like", decision.reason)


    async def test_environment_policy_allows_governed_host_variables(self) -> None:
        decision = await self.sandbox.authorize(SandboxRequest(
            (sys.executable, "-V"),
            self.workspace,
            {**self.environment, "SSH_AUTH_SOCK": "/tmp/agent.sock"},
        ))
        self.assertTrue(decision.allowed)

        decision = await self.sandbox.authorize(SandboxRequest(
            ("../gradlew", "test"), self.workspace, self.environment
        ))
        self.assertFalse(decision.allowed)
        self.assertIn("escapes", decision.reason)

    async def test_denies_untrusted_external_python_runtime(self) -> None:
        other_temporary = tempfile.TemporaryDirectory()

        async def cleanup_runtime() -> None:
            await asyncio.to_thread(other_temporary.cleanup)

        self.addAsyncCleanup(cleanup_runtime)
        external_runtime = Path(other_temporary.name) / "python3"
        external_runtime.write_text("#!/usr/bin/env python3\n")
        external_runtime.chmod(0o755)
        decision = await self.sandbox.authorize(SandboxRequest(
            (str(external_runtime), "-V"),
            self.workspace,
            self.environment,
        ))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.diagnostics["trust_mode"], "tofu")
        self.assertIn("fingerprint", decision.diagnostics)

        persisted = await self.sandbox.authorize(SandboxRequest(
            (str(external_runtime), "-V"),
            self.workspace,
            self.environment,
        ))
        self.assertTrue(persisted.allowed)
        self.assertEqual(
            persisted.diagnostics["trust_mode"],
            "persisted-tofu",
        )

    async def test_denies_runtime_when_fingerprint_changes(self) -> None:
        other_temporary = tempfile.TemporaryDirectory()

        async def cleanup_runtime() -> None:
            await asyncio.to_thread(other_temporary.cleanup)

        self.addAsyncCleanup(cleanup_runtime)
        external_runtime = Path(other_temporary.name) / "python3"
        external_runtime.write_text("#!/usr/bin/env python3\n")
        external_runtime.chmod(0o755)

        first = await self.sandbox.authorize(SandboxRequest(
            (str(external_runtime), "-V"),
            self.workspace,
            self.environment,
        ))
        self.assertTrue(first.allowed)

        external_runtime.write_text("#!/usr/bin/env python3\nprint('changed')\n")

        changed = await self.sandbox.authorize(SandboxRequest(
            (str(external_runtime), "-V"),
            self.workspace,
            self.environment,
        ))
        self.assertFalse(changed.allowed)
        self.assertIn("fingerprint changed", changed.reason)

        decision = await self.sandbox.authorize(SandboxRequest(
            (sys.executable, "-V"),
            self.workspace,
            self.environment,
        ))
        self.assertTrue(decision.allowed)
        self.assertIn("resolved_executable", decision.diagnostics)
        self.assertTrue(os.path.isabs(decision.diagnostics["resolved_executable"]))

    async def test_runtime_trust_cache_uses_structured_schema(self) -> None:
        other_temporary = tempfile.TemporaryDirectory()

        async def cleanup_runtime() -> None:
            await asyncio.to_thread(other_temporary.cleanup)

        self.addAsyncCleanup(cleanup_runtime)
        external_runtime = Path(other_temporary.name) / "python3"
        external_runtime.write_text("#!/usr/bin/env python3\n")
        external_runtime.chmod(0o755)

        decision = await self.sandbox.authorize(SandboxRequest(
            (str(external_runtime), "-V"),
            self.workspace,
            self.environment,
        ))
        self.assertTrue(decision.allowed)

        cache_path = self.workspace / ".tsm" / "runtime_trust.json"
        payload = __import__("json").loads(cache_path.read_text())
        self.assertEqual(payload["version"], 2)
        runtime_record = payload["runtimes"][str(external_runtime.resolve())]
        self.assertEqual(runtime_record["trust_source"], "tofu")
        self.assertIn("enrolled_at", runtime_record)

        health = await self.sandbox.health()
        self.assertEqual(health.state, "healthy")
        self.assertIn("no OS isolation", health.message)


if __name__ == "__main__":
    unittest.main()
