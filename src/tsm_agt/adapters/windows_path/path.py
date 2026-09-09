"""Windows workspace path identity and containment."""

from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
    ResolvedWorkspacePath,
)


class WindowsWorkspacePath:
    descriptor = AdapterDescriptor(
        adapter_id="builtin.windows-workspace-path",
        adapter_version="0.1.0", port_name="WorkspacePathPort",
        port_version="1.0",
        capabilities=frozenset({
            "canonical-path-key", "workspace-containment",
            "case-insensitive-identity", "ads-deny",
            "reparse-aware-parent",
        }),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows workspace path can start only on Windows")
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "Windows workspace path ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    def normalize_workspace(self, workspace: Path) -> Path:
        self._require_started()
        root = workspace.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"workspace is not a directory: {root}")
        return root

    def workspace_key(self, workspace: Path) -> str:
        root = self.normalize_workspace(workspace)
        return self._directory_identity(root)

    def is_link_like(self, path: Path) -> bool:
        self._require_started()
        try:
            attributes = path.lstat().st_file_attributes
        except FileNotFoundError:
            return False
        return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)

    def is_same_or_descendant(self, path: Path, root: Path) -> bool:
        """Compare canonical Windows paths across case and separator aliases."""
        self._require_started()
        candidate = os.path.normcase(str(path.expanduser().resolve(strict=False)))
        ancestor = os.path.normcase(str(root.expanduser().resolve(strict=False)))
        try:
            return os.path.commonpath((candidate, ancestor)) == ancestor
        except ValueError:
            # Different drives or incompatible UNC roots cannot contain each other.
            return False

    def resolve_mutation_path(
        self, workspace: Path, relative_path: str,
    ) -> ResolvedWorkspacePath:
        self._require_started()
        candidate = Path(relative_path)
        if candidate.is_absolute() or candidate.drive or candidate.root:
            raise ValueError("absolute workspace mutation paths are not allowed")
        # A colon in a relative Windows component selects an NTFS Alternate Data
        # Stream. Treating it as an ordinary file would bypass file identity.
        if any(":" in part for part in candidate.parts):
            raise PermissionError(
                "Windows alternate data stream paths are not allowed"
            )
        root = self.normalize_workspace(workspace)
        path = root / candidate
        try:
            parent = path.parent.resolve(strict=True)
        except FileNotFoundError as error:
            raise ValueError(
                "workspace mutation parent directory does not exist"
            ) from error
        try:
            parent.relative_to(root)
        except ValueError as error:
            raise ValueError("workspace mutation path escapes the workspace") from error
        if not parent.is_dir():
            raise ValueError("workspace mutation parent is not a directory")
        canonical = parent / path.name
        relative = canonical.relative_to(root).as_posix()
        key = f"{self._directory_identity(parent)}/{path.name.casefold()}"
        return ResolvedWorkspacePath(root, canonical, relative, key)

    def resolve_access_path(
        self, workspace: Path, relative_path: str,
    ) -> ResolvedWorkspacePath:
        self._require_started()
        candidate = Path(relative_path)
        if candidate.is_absolute() or candidate.drive or candidate.root:
            raise ValueError("absolute workspace paths are not allowed")
        if any(":" in part for part in candidate.parts):
            raise PermissionError(
                "Windows alternate data stream paths are not allowed"
            )
        root = self.normalize_workspace(workspace)
        resolved = (root / candidate).resolve(strict=False)
        try:
            relative = resolved.relative_to(root)
        except ValueError as error:
            raise ValueError("path escapes the workspace") from error
        key = self._path_identity(resolved)
        return ResolvedWorkspacePath(root, resolved, relative.as_posix(), key)

    def resolve_read_path(
        self, workspace: Path, requested_path: str,
        additional_roots: tuple[Path, ...] = (),
    ) -> ResolvedWorkspacePath:
        self._require_started()
        candidate = Path(requested_path).expanduser()
        if any(
            ":" in part for part in candidate.parts
            if part != candidate.anchor
        ):
            raise PermissionError(
                "Windows alternate data stream paths are not allowed"
            )
        workspace_root = self.normalize_workspace(workspace)
        resolved = (
            candidate.resolve(strict=False)
            if candidate.is_absolute() or candidate.drive or candidate.root
            else (workspace_root / candidate).resolve(strict=False)
        )
        roots = (workspace_root,) + tuple(
            self.normalize_workspace(root) for root in additional_roots
        )
        for root in roots:
            try:
                relative = resolved.relative_to(root)
            except ValueError:
                continue
            return ResolvedWorkspacePath(
                root, resolved, relative.as_posix(), self._path_identity(resolved)
            )
        raise ValueError("path escapes the approved read roots")

    def external_read_approval_root(
        self, workspace: Path, requested_path: str,
    ) -> Path | None:
        self._require_started()
        candidate = Path(requested_path).expanduser()
        if any(
            ":" in part for part in candidate.parts
            if part != candidate.anchor
        ):
            return None
        workspace_root = self.normalize_workspace(workspace)
        candidate = (
            candidate.resolve(strict=False)
            if candidate.is_absolute() or candidate.drive or candidate.root
            else (workspace_root / candidate).resolve(strict=False)
        )
        if not candidate.exists():
            return None
        root = candidate if candidate.is_dir() else candidate.parent
        root = root.resolve(strict=True)
        home = Path.home().resolve(strict=True)
        if root == home or root == Path(root.anchor):
            return None
        return root

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("Windows workspace path is not started")

    @classmethod
    def _path_identity(cls, path: Path) -> str:
        """Identify existing paths and missing descendants consistently."""
        if path.exists() and not path.is_dir():
            return f"{cls._directory_identity(path.parent)}/{path.name.casefold()}"
        missing: list[str] = []
        existing = path
        while not existing.exists():
            if existing.parent == existing:
                raise FileNotFoundError(path)
            missing.append(existing.name.casefold())
            existing = existing.parent
        identity = cls._directory_identity(existing)
        if missing:
            identity += "/" + "/".join(reversed(missing))
        return identity

    @staticmethod
    def _directory_identity(path: Path) -> str:
        """Return one identity for drive-letter and UNC aliases."""
        import ctypes
        from ctypes import wintypes

        class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("FileAttributes", wintypes.DWORD),
                ("CreationTime", wintypes.FILETIME),
                ("LastAccessTime", wintypes.FILETIME),
                ("LastWriteTime", wintypes.FILETIME),
                ("VolumeSerialNumber", wintypes.DWORD),
                ("FileSizeHigh", wintypes.DWORD),
                ("FileSizeLow", wintypes.DWORD),
                ("NumberOfLinks", wintypes.DWORD),
                ("FileIndexHigh", wintypes.DWORD),
                ("FileIndexLow", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        get_info = kernel32.GetFileInformationByHandle
        get_info.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
        )
        get_info.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = create_file(
            str(path), 0, 0x1 | 0x2 | 0x4, None, 3, 0x02000000, None
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = BY_HANDLE_FILE_INFORMATION()
            if not get_info(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            file_index = (info.FileIndexHigh << 32) | info.FileIndexLow
            return f"winfile:{info.VolumeSerialNumber:08x}:{file_index:016x}"
        finally:
            close_handle(handle)
