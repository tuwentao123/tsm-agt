"""Read-only workspace tools with path containment and secret filtering."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    HealthState,
    HealthStatus,
    ResolvedWorkspacePath,
    ToolCall,
    ToolEffect,
    ToolIdempotency,
    ToolInvocationContext,
    ToolResult,
    ToolResultAuthority,
    ToolRisk,
    ToolSpec,
    is_sensitive_read_path,
)

_MAX_LIST_LIMIT = 1000
_MAX_READ_LINES = 1000
_MAX_READ_CHARS = 8000
_MAX_SEARCH_MATCHES = 500
_MAX_SEARCH_FILES = 5000
_MAX_SEARCH_FILE_BYTES = 1_000_000
_MAX_MATCH_TEXT_CHARS = 500
_MAX_FIND_LIMIT = 500
_MAX_FIND_ENTRIES = 50_000

_GENERATED_DIRECTORY_NAMES = frozenset({
    ".agent", ".gradle", ".idea", ".venv", ".cxx",
    "build", "dist", "node_modules", "out", "target", "vendor",
    "__pycache__",
})

class _WorkspaceAccessError(ValueError):
    pass


class _SensitivePathError(PermissionError):
    pass


class CoreReadOnlyToolProvider:
    """Dependency-free implementations of the core R0 discovery/read tools."""

    descriptor = AdapterDescriptor(
        adapter_id="builtin.core-readonly-tools",
        adapter_version="0.2.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset(
            {
                "core.list_files", "core.find_files",
                "core.read_file", "core.search_text",
            }
        ),
    )

    _tools = (
        ToolSpec(
            name="core.list_files",
            description=(
                "List files and directories under one workspace-relative path. Use it "
                "to discover project structure; do not use it to read file contents. "
                "An absolute outside path pauses for explicit Task-scoped directory read "
                "approval. Escaped symlinks, .git, credentials, environment files, and "
                "key material remain unavailable. Parameters: path "
                "defaults to '.', recursive defaults to false, and limit is 1..1000. "
                "Success returns the requested path, authoritative resolved path/root, "
                "ordered path/type/size entries, and a truncation flag. "
                "Example: {\"path\": \"src\", \"recursive\": true, \"limit\": 200}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "recursive": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.WORKSPACE_FACT,
        ),
        ToolSpec(
            name="core.find_files",
            description=(
                "Find files by name or workspace-relative path pattern without reading "
                "their contents. Prefer this when the request already names a file, such "
                "as settings.gradle, UserService.swift, or **/package.json; do not walk "
                "the directory tree one level at a time with core.list_files. pattern is "
                "required and supports *, ?, and **. A pattern without / matches the base "
                "file name; a pattern containing / matches the relative path. path defaults "
                "to '.', case_sensitive defaults to false, and limit is 1..500. Generated, "
                "sensitive, escaped-symlink, and credential paths are skipped. An absolute "
                "outside path requires explicit Task-scoped directory read approval. Success "
                "returns the authoritative resolved path/root, ordered path/size matches, "
                "and scan metadata. Example: "
                "{\"pattern\": \"dialog_call_confirm.xml\", \"path\": \"modules\"}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "case_sensitive": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.WORKSPACE_FACT,
        ),
        ToolSpec(
            name="core.read_file",
            description=(
                "Read a UTF-8 text file by path or by a resource_ref returned from a "
                "prior list/find/search result in this Task. Use it after "
                "locating a specific source or config file; do not use it for binary or "
                "secret files. An absolute outside path pauses for explicit Task-scoped "
                "directory read approval. Escaped symlinks, .git, environment files, "
                "credentials, and key material remain unavailable. "
                "start_line defaults to 1 and max_lines defaults to 200 (maximum 1000). "
                "Success returns the authoritative resolved path/root, whole-file sha256, "
                "content, actual line range, total lines, and truncation. "
                "Example: {\"path\": \"src/app.py\", \"start_line\": 1, \"max_lines\": 120}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "resource_ref": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "max_lines": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.WORKSPACE_FACT,
        ),
        ToolSpec(
            name="core.search_text",
            description=(
                "Search UTF-8 text files below a workspace-relative path. Use literal "
                "search by default and enable regex only when pattern matching is required; "
                "an absolute outside path requires explicit Task-scoped directory read "
                "approval and never permits inspecting secrets. Binary, "
                "oversized, .git, environment, credential, and key files are skipped. "
                "query is required; path defaults to '.', regex and case_sensitive default "
                "to false, and max_matches is 1..500. Success returns ordered path, line, "
                "and text matches plus authoritative resolved path/root and "
                "scan/truncation metadata. Example: "
                "{\"query\": \"class Kernel\", \"path\": \"src\"}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "regex": {"type": "boolean"},
                    "case_sensitive": {"type": "boolean"},
                    "max_matches": {"type": "integer"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            risk=ToolRisk.R0,
            is_read_only=True,
            is_concurrency_safe=True,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=ToolEffect.OBSERVE,
            result_authority=ToolResultAuthority.WORKSPACE_FACT,
        ),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        state = HealthState.HEALTHY if self._started else HealthState.UNHEALTHY
        message = "core read-only tools ready" if self._started else "not started"
        return HealthStatus(state, message)

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return self._tools

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        if not self._started:
            raise RuntimeError("adapter is not started")
        handlers = {
            "core.list_files": self._list_files,
            "core.find_files": self._find_files,
            "core.read_file": self._read_file,
            "core.search_text": self._search_text,
        }
        handler = handlers.get(call.name)
        if handler is None:
            return self._error(call, "NOT_FOUND", f"unknown core tool: {call.name}")
        try:
            return handler(call, context)
        except _SensitivePathError as error:
            return self._error(call, "PERMISSION_DENIED", str(error))
        except _WorkspaceAccessError as error:
            return self._error(call, "PERMISSION_DENIED", str(error))
        except (KeyError, TypeError, ValueError, re.error) as error:
            return self._error(
                call,
                "INVALID_PARAM",
                str(error),
                hint="Correct the arguments using the advertised tool schema and limits.",
            )
        except OSError as error:
            return self._error(call, "TOOL_FAILED", str(error), retryable=True)

    def _list_files(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        raw_path = self._string_argument(call.arguments, "path", default=".")
        recursive = self._bool_argument(call.arguments, "recursive", default=False)
        limit = self._bounded_int(
            call.arguments, "limit", default=200, minimum=1, maximum=_MAX_LIST_LIMIT
        )
        resolved = self._resolve_path(context, raw_path)
        root = resolved.path
        if not root.exists():
            return self._error(call, "NOT_FOUND", f"path does not exist: {raw_path}")
        if not root.is_dir():
            return self._error(call, "INVALID_PARAM", f"path is not a directory: {raw_path}")

        candidates = self._walk(root) if recursive else root.iterdir()
        entries: list[dict[str, Any]] = []
        omitted_sensitive = 0
        truncated = False
        for path in candidates:
            try:
                relative = self._safe_relative(context, path)
                if self._is_sensitive(relative):
                    omitted_sensitive += 1
                    continue
            except (_WorkspaceAccessError, _SensitivePathError):
                omitted_sensitive += 1
                continue
            if len(entries) >= limit:
                truncated = True
                break
            stat = path.stat()
            entries.append(
                {
                    "path": relative.as_posix(),
                    **self._resource_data(context, path, resolved.workspace),
                    "type": "directory" if path.is_dir() else "file",
                    "size": stat.st_size,
                }
            )
        entries.sort(key=lambda entry: str(entry["path"]))
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={
                "path": self._display_path(resolved.workspace, root),
                **self._resolved_path_data(context, raw_path, resolved),
                "entries": entries,
                "omitted_sensitive": omitted_sensitive,
            },
            truncated=truncated,
            meta={"untrusted_data": True},
        )

    def _find_files(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        pattern = self._string_argument(call.arguments, "pattern")
        if not pattern:
            raise ValueError("pattern must not be empty")
        normalized_pattern = pattern.replace("\\", "/")
        if normalized_pattern.startswith("/") or ".." in Path(
            normalized_pattern
        ).parts:
            raise ValueError("pattern must stay within the selected path")
        raw_path = self._string_argument(call.arguments, "path", default=".")
        case_sensitive = self._bool_argument(
            call.arguments, "case_sensitive", default=False
        )
        limit = self._bounded_int(
            call.arguments, "limit", default=100, minimum=1,
            maximum=_MAX_FIND_LIMIT,
        )
        resolved = self._resolve_path(context, raw_path)
        root = resolved.path
        if not root.exists():
            return self._error(call, "NOT_FOUND", f"path does not exist: {raw_path}")
        if not root.is_dir():
            return self._error(call, "INVALID_PARAM", f"path is not a directory: {raw_path}")

        match_path = "/" in normalized_pattern
        expected = normalized_pattern if case_sensitive else normalized_pattern.casefold()
        walk_stats = {"generated_directories_skipped": 0}
        matches: list[dict[str, Any]] = []
        scanned_entries = 0
        skipped_entries = 0
        truncated = False
        for path in self._walk_searchable(root, walk_stats):
            if not path.is_file():
                continue
            if scanned_entries >= _MAX_FIND_ENTRIES:
                truncated = True
                break
            scanned_entries += 1
            try:
                relative = self._safe_relative(context, path)
                if self._is_sensitive(relative):
                    skipped_entries += 1
                    continue
            except (_WorkspaceAccessError, _SensitivePathError):
                skipped_entries += 1
                continue
            relative_text = relative.as_posix()
            candidate = relative_text if match_path else path.name
            comparable = candidate if case_sensitive else candidate.casefold()
            if not fnmatch.fnmatchcase(comparable, expected):
                continue
            matches.append({
                "path": relative_text,
                **self._resource_data(context, path, resolved.workspace),
                "size": path.stat().st_size,
            })
            if len(matches) >= limit:
                truncated = True
                break
        matches.sort(key=lambda item: str(item["path"]).casefold())
        return ToolResult(
            call_id=call.call_id, ok=True,
            data={
                "pattern": pattern,
                "path": self._display_path(resolved.workspace, root),
                **self._resolved_path_data(context, raw_path, resolved),
                "matches": matches,
                "scanned_entries": scanned_entries,
                "skipped_entries": skipped_entries,
                "generated_directories_skipped": walk_stats[
                    "generated_directories_skipped"
                ],
            },
            truncated=truncated, meta={"untrusted_data": True},
        )

    def _read_file(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        raw_path = self._read_target(call, context)
        start_line = self._bounded_int(
            call.arguments, "start_line", default=1, minimum=1
        )
        max_lines = self._bounded_int(
            call.arguments,
            "max_lines",
            default=200,
            minimum=1,
            maximum=_MAX_READ_LINES,
        )
        resolved = self._resolve_path(context, raw_path)
        path = resolved.path
        if not path.exists():
            candidates = context.resource_candidates.get(raw_path, ())
            if candidates and not Path(raw_path).is_absolute():
                return ToolResult(
                    call_id=call.call_id, ok=False,
                    error_code="PATH_CONTEXT_REQUIRED",
                    message=(
                        "The relative path does not exist under the primary workspace, "
                        "but the current Task previously discovered matching resources "
                        "under another approved read root."
                    ),
                    hint="Retry core.read_file with one candidate resource_ref.",
                    retryable=True,
                    data={"requested_path": raw_path, "candidates": list(candidates)},
                    meta={"untrusted_data": True, "recoverable_input": True},
                )
            return self._error(call, "NOT_FOUND", f"file does not exist: {raw_path}")
        if not path.is_file():
            return self._error(call, "INVALID_PARAM", f"path is not a file: {raw_path}")
        raw = path.read_bytes()
        if b"\x00" in raw:
            return self._error(call, "INVALID_PARAM", f"file is binary: {raw_path}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return self._error(call, "INVALID_PARAM", f"file is not UTF-8 text: {raw_path}")
        lines = text.splitlines(keepends=True)
        selected = lines[start_line - 1 : start_line - 1 + max_lines]
        content = "".join(selected)
        char_truncated = len(content) > _MAX_READ_CHARS
        if char_truncated:
            content = content[:_MAX_READ_CHARS]
        actual_end = start_line + len(selected) - 1 if selected else start_line - 1
        truncated = char_truncated or actual_end < len(lines)
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={
                "path": self._display_path(resolved.workspace, path),
                **self._resolved_path_data(context, raw_path, resolved),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "start_line": start_line,
                "end_line": actual_end,
                "total_lines": len(lines),
                "content": content,
            },
            truncated=truncated,
            meta={"untrusted_data": True},
        )

    def _search_text(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        query = self._string_argument(call.arguments, "query")
        if not query:
            raise ValueError("query must not be empty")
        raw_path = self._string_argument(call.arguments, "path", default=".")
        use_regex = self._bool_argument(call.arguments, "regex", default=False)
        case_sensitive = self._bool_argument(
            call.arguments, "case_sensitive", default=False
        )
        max_matches = self._bounded_int(
            call.arguments,
            "max_matches",
            default=100,
            minimum=1,
            maximum=_MAX_SEARCH_MATCHES,
        )
        resolved = self._resolve_path(context, raw_path)
        root = resolved.path
        if not root.exists():
            return self._error(call, "NOT_FOUND", f"path does not exist: {raw_path}")
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(query if use_regex else re.escape(query), flags)

        walk_stats = {"generated_directories_skipped": 0}
        if root.is_file():
            files: Iterable[Path] = (root,)
        else:
            files = self._walk_searchable(root, walk_stats)
        matches: list[dict[str, Any]] = []
        scanned_files = 0
        skipped_files = 0
        truncated = False
        for path in files:
            if not path.is_file():
                continue
            try:
                relative = self._safe_relative(context, path)
                if self._is_sensitive(relative):
                    skipped_files += 1
                    continue
            except (_WorkspaceAccessError, _SensitivePathError):
                skipped_files += 1
                continue
            if scanned_files >= _MAX_SEARCH_FILES:
                truncated = True
                break
            if path.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                skipped_files += 1
                continue
            raw = path.read_bytes()
            if b"\x00" in raw:
                skipped_files += 1
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped_files += 1
                continue
            scanned_files += 1
            for line_number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line) is None:
                    continue
                matches.append(
                    {
                        "path": relative.as_posix(),
                        **self._resource_data(context, path, resolved.workspace),
                        "line": line_number,
                        "text": line[:_MAX_MATCH_TEXT_CHARS],
                    }
                )
                if len(matches) >= max_matches:
                    truncated = True
                    break
            if truncated and len(matches) >= max_matches:
                break
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={
                "query": query,
                "path": self._display_path(resolved.workspace, root),
                **self._resolved_path_data(context, raw_path, resolved),
                "matches": matches,
                "scanned_files": scanned_files,
                "skipped_files": skipped_files,
                "generated_directories_skipped": walk_stats[
                    "generated_directories_skipped"
                ],
            },
            truncated=truncated,
            meta={"untrusted_data": True},
        )

    @staticmethod
    def _walk(root: Path) -> Iterable[Path]:
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_names.sort()
            file_names.sort()
            current = Path(directory)
            for name in directory_names:
                yield current / name
            for name in file_names:
                yield current / name

    @staticmethod
    def _walk_searchable(root: Path, stats: dict[str, int]) -> Iterable[Path]:
        if not root.is_dir():
            return
        root_files: list[Path] = []
        grouped_directories: dict[int, list[Path]] = {0: [], 1: [], 2: []}
        for child in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if child.is_dir():
                if child.name.casefold() in _GENERATED_DIRECTORY_NAMES:
                    stats["generated_directories_skipped"] += 1
                    continue
                priority, _ = CoreReadOnlyToolProvider._search_directory_priority(
                    child.name
                )
                grouped_directories[priority].append(child)
            else:
                root_files.append(child)
        for path in sorted(
            root_files,
            key=lambda item: CoreReadOnlyToolProvider._search_file_priority(
                item.name
            ),
        ):
            yield path
        # Do not exhaust the alphabetically first source root in a monorepo.
        # Round-robin lets codeBase/modules/packages all contribute candidates.
        for priority in (0, 1, 2):
            iterators = [
                iter(CoreReadOnlyToolProvider._walk_searchable_tree(path, stats))
                for path in grouped_directories[priority]
            ]
            while iterators:
                active: list[Iterable[Path]] = []
                for iterator in iterators:
                    try:
                        yield next(iterator)
                        active.append(iterator)
                    except StopIteration:
                        continue
                iterators = active

    @staticmethod
    def _walk_searchable_tree(
        root: Path, stats: dict[str, int],
    ) -> Iterable[Path]:
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            retained = []
            for name in sorted(directory_names):
                if name.casefold() in _GENERATED_DIRECTORY_NAMES:
                    stats["generated_directories_skipped"] += 1
                else:
                    retained.append(name)
            directory_names[:] = sorted(
                retained, key=CoreReadOnlyToolProvider._search_directory_priority
            )
            file_names.sort(key=CoreReadOnlyToolProvider._search_file_priority)
            current = Path(directory)
            for name in file_names:
                yield current / name

    @staticmethod
    def _search_directory_priority(name: str) -> tuple[int, str]:
        """Scan likely source roots before documentation/log collections."""

        normalized = name.casefold().replace("_", "").replace("-", "")
        source_roots = {
            "app", "apps", "code", "codebase", "lib", "libs",
            "module", "modules", "package", "packages", "src", "source",
        }
        reference_roots = {
            "aicodingdoc", "doc", "docs", "documentation", "example",
            "examples", "fixture", "fixtures", "log", "logs", "report",
            "reports", "sample", "samples", "testdata",
        }
        if normalized in source_roots:
            return (0, normalized)
        if normalized in reference_roots:
            return (2, normalized)
        return (1, normalized)

    @staticmethod
    def _search_file_priority(name: str) -> tuple[int, str]:
        suffix = Path(name).suffix.casefold()
        source_suffixes = {
            ".c", ".cc", ".cpp", ".cs", ".dart", ".go", ".h", ".hpp",
            ".java", ".js", ".jsx", ".kt", ".kts", ".m", ".mm",
            ".php", ".py", ".rb", ".rs", ".scala", ".swift", ".ts",
            ".tsx", ".vue", ".xml",
        }
        reference_suffixes = {".log", ".md", ".rst", ".txt"}
        if suffix in source_suffixes:
            return (0, name.casefold())
        if suffix in reference_suffixes:
            return (2, name.casefold())
        return (1, name.casefold())

    @classmethod
    def _resolve_path(
        cls, context: ToolInvocationContext, raw_path: str,
    ) -> ResolvedWorkspacePath:
        if context.workspace_path is None:
            raise RuntimeError("workspace path capability is unavailable")
        try:
            resolved = context.workspace_path.resolve_read_path(
                context.workspace, raw_path, context.additional_read_roots
            )
        except PermissionError as error:
            raise _SensitivePathError(str(error)) from error
        except ValueError as error:
            raise _WorkspaceAccessError(str(error)) from error
        relative = Path(resolved.relative_path)
        if cls._is_sensitive(relative):
            raise _SensitivePathError("sensitive paths are not available to core tools")
        return resolved

    @staticmethod
    def _resolved_path_data(
        context: ToolInvocationContext, requested_path: str,
        resolved: ResolvedWorkspacePath,
    ) -> dict[str, str]:
        """Return explicit location facts so relative paths cannot be mistaken.

        These fields describe what the path adapter actually resolved. They do not
        infer the user's intent and do not grant access to another directory.
        """
        if context.workspace_path is None:
            raise RuntimeError("workspace path capability is unavailable")
        primary = context.workspace_path.normalize_workspace(context.workspace)
        root_kind = (
            "PRIMARY_WORKSPACE"
            if resolved.workspace == primary
            else "TASK_APPROVED_READ_ROOT"
        )
        return {
            "requested_path": requested_path,
            "resolved_path": str(resolved.path),
            "resolved_root": str(resolved.workspace),
            "root_alias": resolved.workspace.name or str(resolved.workspace),
            "root_kind": root_kind,
        }

    @classmethod
    def _resource_data(
        cls, context: ToolInvocationContext, path: Path, root: Path,
    ) -> dict[str, str]:
        root_kind = cls._resolved_path_data(
            context, str(path), ResolvedWorkspacePath(
                root, path, path.relative_to(root).as_posix(), str(path)
            ),
        )["root_kind"]
        raw = f"{context.task_id}\0{root}\0{path}".encode("utf-8")
        return {
            "resource_ref": "resource-" + hashlib.sha256(raw).hexdigest(),
            "resolved_path": str(path),
            "resolved_root": str(root),
            "root_kind": root_kind,
        }

    @staticmethod
    def _read_target(call: ToolCall, context: ToolInvocationContext) -> str:
        raw_path = call.arguments.get("path")
        resource_ref = call.arguments.get("resource_ref")
        if raw_path is not None and resource_ref is not None:
            raise ValueError("provide exactly one of path or resource_ref")
        if resource_ref is not None:
            if not isinstance(resource_ref, str) or not resource_ref.strip():
                raise TypeError("resource_ref must be a non-empty string")
            resolved = context.resource_paths.get(resource_ref)
            if resolved is None:
                raise ValueError(
                    "resource_ref is unknown, expired, or belongs to another Task"
                )
            return resolved
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("provide exactly one of path or resource_ref")
        return raw_path

    @classmethod
    def _safe_relative(
        cls, context: ToolInvocationContext, path: Path,
    ) -> Path:
        if context.workspace_path is None:
            raise RuntimeError("workspace path capability is unavailable")
        try:
            resolved = context.workspace_path.resolve_read_path(
                context.workspace, str(path), context.additional_read_roots
            )
            relative = Path(resolved.relative_path)
        except PermissionError as error:
            raise _SensitivePathError(str(error)) from error
        except ValueError as error:
            raise _WorkspaceAccessError(str(error)) from error
        if cls._is_sensitive(relative):
            raise _SensitivePathError("sensitive paths are not available to core tools")
        return relative

    @staticmethod
    def _is_sensitive(relative: Path) -> bool:
        return is_sensitive_read_path(relative)

    @staticmethod
    def _display_path(workspace: Path, path: Path) -> str:
        relative = path.relative_to(workspace)
        return relative.as_posix() or "."

    @staticmethod
    def _string_argument(
        arguments: Mapping[str, Any], name: str, default: str | None = None
    ) -> str:
        value = arguments.get(name, default)
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        return value

    @staticmethod
    def _bool_argument(
        arguments: Mapping[str, Any], name: str, default: bool
    ) -> bool:
        value = arguments.get(name, default)
        if not isinstance(value, bool):
            raise TypeError(f"{name} must be a boolean")
        return value

    @staticmethod
    def _bounded_int(
        arguments: Mapping[str, Any],
        name: str,
        default: int,
        minimum: int,
        maximum: int | None = None,
    ) -> int:
        value = arguments.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{name} must be an integer")
        if value < minimum or (maximum is not None and value > maximum):
            upper = f"..{maximum}" if maximum is not None else " or greater"
            raise ValueError(f"{name} must be {minimum}{upper}")
        return value

    @staticmethod
    def _error(
        call: ToolCall,
        code: str,
        message: str,
        hint: str | None = None,
        retryable: bool = False,
    ) -> ToolResult:
        return ToolResult(
            call_id=call.call_id,
            ok=False,
            error_code=code,
            message=message,
            hint=hint,
            retryable=retryable,
            meta={"untrusted_data": True},
        )
