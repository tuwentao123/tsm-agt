from __future__ import annotations

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

    async def test_workspace_relative_executable_cannot_escape_cwd(self) -> None:
        decision = await self.sandbox.authorize(SandboxRequest(
            ("../gradlew", "test"), self.workspace, self.environment
        ))
        self.assertFalse(decision.allowed)
        self.assertIn("escapes", decision.reason)

    async def test_health_does_not_claim_os_isolation(self) -> None:
        health = await self.sandbox.health()
        self.assertEqual(health.state, "healthy")
        self.assertIn("no OS isolation", health.message)


if __name__ == "__main__":
    unittest.main()
