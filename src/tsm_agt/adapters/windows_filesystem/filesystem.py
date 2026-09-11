"""Windows durable workspace filesystem operations."""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
)


class WindowsWorkspaceFilesystem:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.windows-workspace-filesystem",
        adapter_version="0.1.0", port_name="WorkspaceFilesystemPort",
        port_version="1.0",
        capabilities=frozenset({
            "atomic-replace", "durable-delete", "directory-flush",
            "sharing-violation-retry",
        }),
    )

    _TRANSIENT_WINERRORS = frozenset({5, 32, 33})

    def __init__(
        self, retry_attempts: int = 5, retry_delay_seconds: float = 0.02,
    ) -> None:
        if retry_attempts < 1 or retry_delay_seconds < 0:
            raise ValueError("invalid Windows filesystem retry settings")
        self._retry_attempts = retry_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows workspace filesystem can start only on Windows")
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "Windows workspace filesystem ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    def replace(self, source: Path, target: Path) -> None:
        self._require_started()
        if target.exists():
            self._copy_dacl(target, source)
        self._retry(lambda: os.replace(source, target))

    def unlink(self, path: Path) -> None:
        self._require_started()
        self._retry(path.unlink)

    def make_directory(self, path: Path) -> None:
        """Create exactly one directory; the core validates its location."""
        self._require_started()
        self._retry(path.mkdir)

    def remove_directory(self, path: Path) -> None:
        """Remove exactly one empty directory."""
        self._require_started()
        self._retry(path.rmdir)

    def sync_directory(self, directory: Path) -> None:
        self._require_started()
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        flush_file_buffers = kernel32.FlushFileBuffers
        flush_file_buffers.argtypes = (wintypes.HANDLE,)
        flush_file_buffers.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = create_file(
            str(directory), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x02000000, None
        )
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not flush_file_buffers(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            close_handle(handle)

    def protect_private_path(self, path: Path) -> None:
        self._require_started()
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD),
        )
        convert.restype = wintypes.BOOL
        set_security = advapi32.SetFileSecurityW
        set_security.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
        set_security.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
        kernel32.LocalFree.restype = wintypes.HLOCAL
        descriptor = ctypes.c_void_p()
        # Protected DACL: SYSTEM and the object's owner receive full access.
        # Directories propagate the same protected access to runtime children.
        sddl = (
            "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;OW)"
            if path.is_dir()
            else "D:P(A;;FA;;;SY)(A;;FA;;;OW)"
        )
        if not convert(
            sddl, 1,
            ctypes.byref(descriptor), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not set_security(str(path), 0x00000004 | 0x80000000, descriptor):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.LocalFree(descriptor)

    @staticmethod
    def _copy_dacl(source: Path, target: Path) -> None:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        get_security = advapi32.GetFileSecurityW
        get_security.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        get_security.restype = wintypes.BOOL
        set_security = advapi32.SetFileSecurityW
        set_security.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID)
        set_security.restype = wintypes.BOOL
        get_control = advapi32.GetSecurityDescriptorControl
        get_control.argtypes = (
            wintypes.LPVOID, ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        )
        get_control.restype = wintypes.BOOL
        needed = wintypes.DWORD()
        get_security(str(source), 0x00000004, None, 0, ctypes.byref(needed))
        error = ctypes.get_last_error()
        if error != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise ctypes.WinError(error)
        descriptor = ctypes.create_string_buffer(needed.value)
        if not get_security(
            str(source), 0x00000004, descriptor, needed, ctypes.byref(needed)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        # SetFileSecurity requires an explicit protection flag; the control bit
        # embedded in a self-relative descriptor is not sufficient by itself.
        protection = 0x80000000 if control.value & 0x1000 else 0x20000000
        if not set_security(str(target), 0x00000004 | protection, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())

    def _retry(self, action) -> None:
        for attempt in range(self._retry_attempts):
            try:
                action()
                return
            except OSError as error:
                if (
                    getattr(error, "winerror", None) not in self._TRANSIENT_WINERRORS
                    or attempt + 1 == self._retry_attempts
                ):
                    raise
                time.sleep(self._retry_delay_seconds)

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("Windows workspace filesystem is not started")
