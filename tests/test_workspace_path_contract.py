from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.posix_path import PosixWorkspacePath
from tsm_agt.adapters.windows_path import WindowsWorkspacePath
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.ports import (
    AdapterContext, HealthState, WorkspacePathPort,
)


def native_path_service() -> WorkspacePathPort:
    if os.name == "nt":
        return WindowsWorkspacePath()
    return PosixWorkspacePath()


class NativeWorkspacePathContractTest(unittest.IsolatedAsyncioTestCase):
    """The same mutation-path contract runs natively on macOS and Windows."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        self.outside = Path(self.temporary.name) / "outside"
        self.outside.mkdir()
        self.path_service = native_path_service()
        await self.path_service.start(AdapterContext(
            config={}, emit_event=lambda _type, _payload: None
        ))

    async def asyncTearDown(self) -> None:
        await self.path_service.stop(None)  # type: ignore[arg-type]
        self.temporary.cleanup()

    async def test_lifecycle_and_canonical_target(self) -> None:
        self.assertEqual(
            (await self.path_service.health()).state, HealthState.HEALTHY
        )
        source = self.workspace / "src"
        source.mkdir()
        resolved = self.path_service.resolve_mutation_path(
            self.workspace, "src/../src/app.py"
        )
        self.assertEqual(resolved.workspace, self.workspace.resolve())
        self.assertEqual(resolved.path, source.resolve() / "app.py")
        self.assertEqual(resolved.relative_path, "src/app.py")
        self.assertTrue(resolved.canonical_key)

        await self.path_service.stop(None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(RuntimeError, "not started"):
            self.path_service.resolve_mutation_path(self.workspace, "app.py")

    async def test_absolute_traversal_and_missing_parent_are_rejected(self) -> None:
        cases = (
            str((self.workspace / "absolute.txt").resolve()),
            "../outside/escape.txt",
            "missing-parent/file.txt",
        )
        for path in cases:
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.path_service.resolve_mutation_path(self.workspace, path)

    async def test_access_path_allows_missing_target_but_rejects_escape(self) -> None:
        missing = self.path_service.resolve_access_path(
            self.workspace, "missing.txt"
        )
        self.assertEqual(missing.path, self.workspace.resolve() / "missing.txt")
        self.assertEqual(missing.relative_path, "missing.txt")
        with self.assertRaisesRegex(ValueError, "escapes the workspace"):
            self.path_service.resolve_access_path(
                self.workspace, "../outside/secret.txt"
            )

    async def test_linked_parent_cannot_escape_workspace(self) -> None:
        link = self.workspace / "linked-parent"
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(self.outside)],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                self.skipTest(f"cannot create Windows Junction: {result.stderr}")
        else:
            os.symlink(self.outside, link)

        with self.assertRaisesRegex(ValueError, "escapes the workspace"):
            self.path_service.resolve_mutation_path(
                self.workspace, "linked-parent/escape.txt"
            )
        with self.assertRaisesRegex(ValueError, "escapes the workspace"):
            self.path_service.resolve_access_path(
                self.workspace, "linked-parent/escape.txt"
            )

    async def test_windows_ads_is_rejected(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows alternate data stream contract")
        with self.assertRaisesRegex(PermissionError, "alternate data stream"):
            self.path_service.resolve_mutation_path(
                self.workspace, "ordinary.txt:hidden-stream"
            )
        with self.assertRaisesRegex(PermissionError, "alternate data stream"):
            self.path_service.resolve_access_path(
                self.workspace, "ordinary.txt:hidden-stream"
            )

    async def test_windows_case_alias_has_one_canonical_key(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows case-insensitive identity contract")
        directory = self.workspace / "MixedCase"
        directory.mkdir()
        first = self.path_service.resolve_mutation_path(
            self.workspace, "MixedCase/Target.txt"
        )
        second = self.path_service.resolve_mutation_path(
            self.workspace, "mixedcase/target.TXT"
        )
        self.assertEqual(first.canonical_key, second.canonical_key)
        self.assertEqual(
            self.path_service.workspace_key(self.workspace),
            self.path_service.workspace_key(Path(str(self.workspace).swapcase())),
        )

    async def test_windows_long_path_and_unc_workspace_identity(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows UNC and long-path identity contract")
        nested = self.workspace
        while len(str(nested)) <= 280:
            nested = nested / "long-segment-0123456789"
        nested.mkdir(parents=True)
        resolved = self.path_service.resolve_access_path(nested, "target.txt")
        self.assertEqual(resolved.workspace, nested.resolve())
        self.assertTrue(resolved.canonical_key)

        # A local path can be expressed through the loopback administrative share.
        drive = self.workspace.drive.rstrip(":")
        if not drive:
            self.skipTest("workspace has no drive for UNC identity test")
        unc = Path(
            rf"\\localhost\{drive}$\{str(self.workspace)[3:]}"
        )
        try:
            key = self.path_service.workspace_key(unc)
        except OSError as error:
            self.skipTest(f"administrative UNC share unavailable: {error}")
        self.assertEqual(key, self.path_service.workspace_key(self.workspace))


class PlatformWorkspacePathSelectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_composition_registers_native_platform_adapter(self) -> None:
        application = compose_fixture_application()
        adapter = application.registry.require(WorkspacePathPort)
        expected = (
            "builtin.windows-workspace-path"
            if os.name == "nt"
            else "builtin.posix-workspace-path"
        )
        self.assertEqual(adapter.descriptor.adapter_id, expected)
        await application.registry.start_all()
        try:
            self.assertEqual((await adapter.health()).state, HealthState.HEALTHY)
        finally:
            await application.registry.stop_all()

    async def test_non_native_adapter_fails_closed(self) -> None:
        adapter = (
            PosixWorkspacePath() if os.name == "nt" else WindowsWorkspacePath()
        )
        expected = "cannot start on Windows" if os.name == "nt" else "only on Windows"
        with self.assertRaisesRegex(RuntimeError, expected):
            await adapter.start(AdapterContext(
                config={}, emit_event=lambda _type, _payload: None
            ))
        self.assertEqual((await adapter.health()).state, HealthState.UNHEALTHY)


if __name__ == "__main__":
    unittest.main()
