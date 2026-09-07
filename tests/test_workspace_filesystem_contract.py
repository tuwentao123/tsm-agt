from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.posix_filesystem import PosixWorkspaceFilesystem
from tsm_agt.adapters.windows_filesystem import WindowsWorkspaceFilesystem
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.ports import (
    AdapterContext, HealthState, WorkspaceFilesystemPort,
)


def native_filesystem() -> WorkspaceFilesystemPort:
    if os.name == "nt":
        return WindowsWorkspaceFilesystem()
    return PosixWorkspaceFilesystem()


class NativeWorkspaceFilesystemContractTest(unittest.IsolatedAsyncioTestCase):
    """The same durable-file contract runs natively on macOS and Windows."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.filesystem = native_filesystem()
        await self.filesystem.start(AdapterContext(
            config={}, emit_event=lambda _type, _payload: None
        ))

    async def asyncTearDown(self) -> None:
        await self.filesystem.stop(None)  # type: ignore[arg-type]
        self.temporary.cleanup()

    async def test_lifecycle_is_healthy_and_stop_closes_operations(self) -> None:
        self.assertEqual(
            (await self.filesystem.health()).state, HealthState.HEALTHY
        )
        await self.filesystem.stop(None)  # type: ignore[arg-type]
        self.assertEqual(
            (await self.filesystem.health()).state, HealthState.UNHEALTHY
        )
        with self.assertRaisesRegex(RuntimeError, "not started"):
            self.filesystem.sync_directory(self.directory)

    async def test_replace_preserves_exact_binary_content(self) -> None:
        source = self.directory / ".target.txt.transaction.tmp"
        target = self.directory / "target.txt"
        target.write_bytes(b"old\x00content")
        expected = b"new\x00binary\xffcontent"
        source.write_bytes(expected)

        self.filesystem.replace(source, target)
        self.filesystem.sync_directory(self.directory)

        self.assertEqual(target.read_bytes(), expected)
        self.assertFalse(source.exists())
        self.assertEqual(target.parent, source.parent)

    async def test_unlink_removes_file_and_directory_sync_succeeds(self) -> None:
        target = self.directory / "delete-me.txt"
        target.write_text("delete me\n", encoding="utf-8")

        self.filesystem.unlink(target)
        self.filesystem.sync_directory(self.directory)

        self.assertFalse(target.exists())

    async def test_windows_share_blocked_target_fails_then_can_be_replaced(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows file-sharing contract")
        import ctypes
        from ctypes import wintypes

        target = self.directory / "shared.txt"
        source = self.directory / "shared.tmp"
        target.write_bytes(b"old")
        source.write_bytes(b"new")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        # Allow reads/writes but deliberately deny FILE_SHARE_DELETE.
        handle = create_file(str(target), 0x80000000, 0x1 | 0x2, None, 3, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            with self.assertRaises(OSError):
                self.filesystem.replace(source, target)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(source.read_bytes(), b"new")
        finally:
            close_handle(handle)

        self.filesystem.replace(source, target)
        self.filesystem.sync_directory(self.directory)
        self.assertEqual(target.read_bytes(), b"new")

    async def test_private_path_protection_and_replace_acl_contract(self) -> None:
        private_directory = self.directory / "private-directory"
        private_directory.mkdir()
        self.filesystem.protect_private_path(private_directory)
        private = self.directory / "private.bin"
        private.write_bytes(b"private")
        self.filesystem.protect_private_path(private)
        if os.name != "nt":
            self.assertEqual(private_directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(private.stat().st_mode & 0o777, 0o600)
            return

        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        get_control = advapi32.GetSecurityDescriptorControl
        get_control.argtypes = (
            wintypes.LPVOID, ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        )
        get_control.restype = wintypes.BOOL

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        convert_to_text = (
            advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
        )
        convert_to_text.argtypes = (
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD),
        )
        convert_to_text.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
        kernel32.LocalFree.restype = wintypes.HLOCAL

        def descriptor(path: Path) -> tuple[bytes, str]:
            get_security = advapi32.GetFileSecurityW
            get_security.argtypes = (
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID,
                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            )
            get_security.restype = wintypes.BOOL
            needed = wintypes.DWORD()
            get_security(str(path), 0x00000004, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            self.assertTrue(get_security(
                str(path), 0x00000004, buffer, needed, ctypes.byref(needed)
            ))
            text = wintypes.LPWSTR()
            self.assertTrue(convert_to_text(
                buffer, 1, 0x00000004, ctypes.byref(text), None
            ))
            try:
                return bytes(buffer.raw[:needed.value]), text.value
            finally:
                kernel32.LocalFree(text)

        private_descriptor, private_sddl = descriptor(private)
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(private_descriptor)
        self.assertTrue(get_control(buffer, ctypes.byref(control), ctypes.byref(revision)))
        self.assertTrue(control.value & 0x1000)  # SE_DACL_PROTECTED
        self.assertIn("D:P", private_sddl)
        _, directory_sddl = descriptor(private_directory)
        self.assertIn("OICI", directory_sddl)

        target = self.directory / "acl-target.txt"
        temporary = self.directory / "acl-source.tmp"
        target.write_bytes(b"old")
        self.filesystem.protect_private_path(target)
        _, before = descriptor(target)
        temporary.write_bytes(b"new")
        self.filesystem.replace(temporary, target)
        _, after = descriptor(target)
        self.assertEqual(after, before)


class PlatformWorkspaceFilesystemSelectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_composition_registers_native_platform_adapter(self) -> None:
        application = compose_fixture_application()
        adapter = application.registry.require(WorkspaceFilesystemPort)
        expected = (
            "builtin.windows-workspace-filesystem"
            if os.name == "nt"
            else "builtin.posix-workspace-filesystem"
        )
        self.assertEqual(adapter.descriptor.adapter_id, expected)
        await application.registry.start_all()
        try:
            self.assertEqual((await adapter.health()).state, HealthState.HEALTHY)
        finally:
            await application.registry.stop_all()

    async def test_non_native_adapter_fails_closed(self) -> None:
        adapter = (
            PosixWorkspaceFilesystem()
            if os.name == "nt"
            else WindowsWorkspaceFilesystem()
        )
        expected = "cannot start on Windows" if os.name == "nt" else "only on Windows"
        with self.assertRaisesRegex(RuntimeError, expected):
            await adapter.start(AdapterContext(
                config={}, emit_event=lambda _type, _payload: None
            ))
        self.assertEqual((await adapter.health()).state, HealthState.UNHEALTHY)


if __name__ == "__main__":
    unittest.main()
