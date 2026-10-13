"""Read-only workspace tools with path containment and secret filtering."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from tsm_agt.core.configuration import ripgrep_binary
from tsm_agt.core.workspace import EXCLUDED_DIRECTORIES
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

#: Locating runs before reading, so it must not outlive the turn it serves.
_RIPGREP_TIMEOUT_SECONDS = 20.0
_MAX_FIND_LIMIT = 500
_MAX_FIND_ENTRIES = 50_000


class _WorkspaceAccessError(ValueError):
    pass


class _SensitivePathError(PermissionError):
    pass


class CoreReadOnlyToolProvider:
    """Dependency-free implementations of the core R0 discovery/read tools."""

    descriptor = AdapterDescriptor(
        adapter_id="builtin.core-readonly-tools",
        adapter_version="0.3.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset(
            {
                "core.list_files", "core.search_files",
                "core.find_files", "core.read_file", "core.search_text",
                "core.grep_search",
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
                    "limit": {
                        "type": "integer", "minimum": 1,
                        "maximum": _MAX_LIST_LIMIT,
                    },
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
            name="core.search_files",
            description=(
                "Primary repository search tool. Search file contents or file names with "
                "one explicit target. target defaults to 'content': use it for source "
                "symbols, UI labels, DOM ids/classes, functions, configuration keys, and "
                "error text; use target='files' only when a file name is already known. "
                "output_mode='paths'|'count' answers a survey cheaply, and offset pages "
                "past a capped result. Inspect hits with core.read_file before "
                "concluding."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "target": {
                        "type": "string",
                        "enum": ["content", "files"],
                        "default": "content",
                    },
                    "path": {"type": "string"},
                    "file_glob": {"type": "string"},
                    "regex": {"type": "boolean"},
                    "case_sensitive": {"type": "boolean"},
                    "limit": {
                        "type": "integer", "minimum": 1,
                        "maximum": _MAX_SEARCH_MATCHES,
                    },
                    "offset": {
                        "type": "integer", "minimum": 0,
                        "maximum": _MAX_SEARCH_MATCHES,
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["matches", "paths", "count"],
                        "default": "matches",
                    },
                    "context": {
                        "type": "integer", "minimum": 0, "maximum": 20,
                    },
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
            name="core.find_files",
            description=(
                "Legacy filename-search alias retained for checkpoint compatibility. "
                "For new work use core.search_files with target='files'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "case_sensitive": {"type": "boolean"},
                    "limit": {
                        "type": "integer", "minimum": 1,
                        "maximum": _MAX_FIND_LIMIT,
                    },
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
                    "start_line": {"type": "integer", "minimum": 1},
                    "max_lines": {
                        "type": "integer", "minimum": 1,
                        "maximum": _MAX_READ_LINES,
                    },
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
                "Legacy content-search alias retained for checkpoint compatibility. "
                "For new work use core.search_files with target='content'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "regex": {"type": "boolean"},
                    "case_sensitive": {"type": "boolean"},
                    "max_matches": {
                        "type": "integer", "minimum": 1,
                        "maximum": _MAX_SEARCH_MATCHES,
                    },
                    "include": {"type": "string"},
                    "exclude": {"type": "string"},
                    "before_context": {
                        "type": "integer", "minimum": 0, "maximum": 20,
                    },
                    "after_context": {
                        "type": "integer", "minimum": 0, "maximum": 20,
                    }
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
        ToolSpec(
            name="core.grep_search",
            description=(
                "Legacy grep-style content-search alias retained for checkpoint "
                "compatibility. For new work use core.search_files with target='content'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "regex": {"type": "boolean"},
                    "case_sensitive": {"type": "boolean"},
                    "max_matches": {
                        "type": "integer", "minimum": 1,
                        "maximum": _MAX_SEARCH_MATCHES,
                    },
                    "include": {"type": "string"},
                    "exclude": {"type": "string"},
                    "before_context": {
                        "type": "integer", "minimum": 0, "maximum": 20,
                    },
                    "after_context": {
                        "type": "integer", "minimum": 0, "maximum": 20,
                    }
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
            "core.search_files": self._search_files,
            "core.find_files": self._find_files,
            "core.read_file": self._read_file,
            "core.search_text": self._search_text,
            "core.grep_search": self._search_text,
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

        list_stats = {"generated_directories_skipped": 0}
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

    def _search_files(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        pattern = self._string_argument(call.arguments, "pattern")
        if not pattern:
            raise ValueError("pattern must not be empty")
        target = self._string_argument(
            call.arguments, "target", default="content"
        ).casefold()
        if target not in {"content", "files"}:
            raise ValueError("target must be 'content' or 'files'")

        if target == "files":
            arguments: dict[str, Any] = {"pattern": pattern}
            for key in ("path", "case_sensitive", "limit"):
                if key in call.arguments:
                    arguments[key] = call.arguments[key]
            result = self._find_files(
                replace(call, name="core.find_files", arguments=arguments),
                context,
            )
            if not result.ok:
                return result
            return replace(
                result,
                hint=(
                    "File-name matches are candidates only. Inspect a relevant hit with "
                    "core.read_file before concluding or editing. If the identifier is "
                    "inside source code, retry core.search_files with target='content'."
                ),
            )

        arguments = {"query": pattern}
        mappings = {
            "path": "path",
            "file_glob": "include",
            "regex": "regex",
            "case_sensitive": "case_sensitive",
            "limit": "max_matches",
            "offset": "offset",
            "output_mode": "output_mode",
        }
        for source, destination in mappings.items():
            if source in call.arguments:
                arguments[destination] = call.arguments[source]
        if "context" in call.arguments:
            arguments["before_context"] = call.arguments["context"]
            arguments["after_context"] = call.arguments["context"]
        result = self._search_text(
            replace(call, name="core.search_text", arguments=arguments),
            context,
        )
        if not result.ok:
            return result
        # A truncated result is a stopping cue, not a conclusion: say what to do
        # next instead of leaving "there were more matches" to the model's
        # imagination.
        hint = (
            "Inspect relevant content matches with core.read_file before concluding "
            "or editing. If no match answers the question, refine the pattern or "
            "search a broader in-scope path."
        )
        if result.truncated:
            hint = (
                "Truncated: these matches are not the whole answer. Narrow the "
                "pattern or path, or search past the last match shown, and read the "
                "hit with core.read_file before concluding."
            )
        return replace(result, hint=hint)

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
        walk_stats: dict[str, int] = {"generated_directories_skipped": 0}
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
                "search_target": "file_names_and_paths",
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
            truncated=truncated,
            hint=(
                "This result searched file names and paths only, not file contents. "
                "Read a likely match before relying on it. If the unknown is a source "
                "symbol, UI label, DOM id/class, function, configuration key, or error "
                "text, use core.search_files with target='content' instead."
            ),
            meta={"untrusted_data": True},
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
        # A partial read is the normal way to read a long file, so the next call
        # is named instead of leaving the model to guess that more exists.
        hint = (
            f"Truncated at line {actual_end} of {len(lines)}: continue with "
            f"core.read_file path={raw_path} start_line={actual_end + 1} before "
            "concluding from this file."
            if truncated else None
        )
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
                # A machine-readable continuation cursor, so paging a long file
                # does not depend on re-deriving it from end_line.
                "next_offset": actual_end + 1 if truncated else None,
                "content": content,
            },
            truncated=truncated,
            hint=hint,
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
        include_glob = self._optional_string_argument(call.arguments, "include")
        exclude_glob = self._optional_string_argument(call.arguments, "exclude")
        # A cheap shape answers a cheap question: "which files" and "how many"
        # do not need every matching line carried back to the model.
        output_mode = self._string_argument(
            call.arguments, "output_mode", default="matches"
        ).casefold()
        if output_mode not in {"matches", "paths", "count"}:
            raise ValueError("output_mode must be 'matches', 'paths' or 'count'")
        offset = self._bounded_int(
            call.arguments, "offset", default=0, minimum=0,
            maximum=_MAX_SEARCH_MATCHES,
        )
        before_context = self._bounded_int(
            call.arguments,
            "before_context",
            default=0,
            minimum=0,
            maximum=20,
        )
        after_context = self._bounded_int(
            call.arguments,
            "after_context",
            default=0,
            minimum=0,
            maximum=20,
        )
        resolved = self._resolve_path(context, raw_path)
        root = resolved.path
        if not root.exists():
            return self._error(call, "NOT_FOUND", f"path does not exist: {raw_path}")
        binary = ripgrep_binary()
        if binary is not None:
            located = self._locate_with_ripgrep(
                binary=binary, call=call, context=context, root=root,
                resolved=resolved, raw_path=raw_path, query=query,
                use_regex=use_regex, case_sensitive=case_sensitive,
                include_glob=include_glob, exclude_glob=exclude_glob,
                before_context=before_context, after_context=after_context,
                max_matches=max_matches, output_mode=output_mode, offset=offset,
            )
            if located is not None:
                return located
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(query if use_regex else re.escape(query), flags)

        walk_stats: dict[str, int] = {"generated_directories_skipped": 0}
        # An explicitly named file is searched directly rather than walked to.
        files: Iterable[Path] = (
            (root,) if root.is_file()
            else self._walk_searchable(root, walk_stats)
        )
        matches: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        paths_seen: dict[str, None] = {}
        scanned_files = 0
        skipped_files = 0
        truncated = False
        # Only the detailed mode has to be capped: counting and listing paths
        # keep no line text, so they can survey the whole walk.
        scan_cap = min(_MAX_SEARCH_MATCHES, offset + max_matches)
        for path in files:
            if not path.is_file():
                continue
            try:
                relative = self._safe_relative(context, path)
                if self._is_sensitive(relative):
                    skipped_files += 1
                    continue
                relative_path = relative.as_posix()
                if include_glob and not fnmatch.fnmatch(relative_path, include_glob):
                    continue
                if exclude_glob and fnmatch.fnmatch(relative_path, exclude_glob):
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
            lines = text.splitlines()
            if output_mode != "matches":
                file_hits = sum(
                    1 for line in lines if pattern.search(line) is not None
                )
                if file_hits:
                    counts[relative_path] = file_hits
                    paths_seen[relative_path] = None
                continue
            for line_number, line in enumerate(lines, start=1):
                if pattern.search(line) is None:
                    continue
                match = {
                    "path": relative_path,
                    **self._resource_data(context, path, resolved.workspace),
                    "line": line_number,
                    "text": line[:_MAX_MATCH_TEXT_CHARS],
                }
                if before_context:
                    start = max(0, line_number - before_context - 1)
                    match["before_context"] = lines[start:line_number - 1]
                if after_context:
                    end = min(len(lines), line_number + after_context)
                    match["after_context"] = lines[line_number:end]
                matches.append(match)
                if len(matches) >= scan_cap:
                    truncated = True
                    break
            if truncated and len(matches) >= scan_cap:
                break
        payload, page_truncated = self._search_payload(
            output_mode, matches, counts, list(paths_seen), offset, max_matches,
        )
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={
                "search_target": "content",
                "output_mode": output_mode,
                "query": query,
                "path": self._display_path(resolved.workspace, root),
                **self._resolved_path_data(context, raw_path, resolved),
                **payload,
                "scanned_files": scanned_files,
                "skipped_files": skipped_files,
                "generated_directories_skipped": walk_stats[
                    "generated_directories_skipped"
                ],
            },
            truncated=truncated or page_truncated,
            meta={"untrusted_data": True},
        )

    @staticmethod
    def _search_payload(
        output_mode: str, matches: list[dict[str, Any]],
        counts: dict[str, int], paths: list[str], offset: int, limit: int,
    ) -> tuple[dict[str, Any], bool]:
        """Project the gathered hits into the requested answer shape.

        ``truncated`` is about the *page*, not the scan: more items exist past
        what this call returns, so the caller can continue with ``offset``.
        """

        if output_mode == "count":
            ordered = sorted(counts.items())
            page = ordered[offset:offset + limit]
            return ({
                "counts": dict(page),
                "files_matched": len(ordered),
                "total_matches": sum(counts.values()),
            }, offset + limit < len(ordered))
        if output_mode == "paths":
            page = paths[offset:offset + limit]
            return ({
                "paths": page,
                "files_matched": len(paths),
            }, offset + limit < len(paths))
        page = matches[offset:offset + limit]
        # ``match_count`` counts this page only: the scan stops at the page, so
        # asserting a total would claim a completeness this call never had.
        # ``truncated`` is what says more results exist.
        return ({"matches": page, "match_count": len(page)},
                offset + limit < len(matches))

    def _locate_with_ripgrep(
        self, *, binary: str, call: ToolCall, context: ToolInvocationContext,
        root: Path, resolved: ResolvedWorkspacePath, raw_path: str,
        query: str, use_regex: bool, case_sensitive: bool,
        include_glob: str | None, exclude_glob: str | None,
        before_context: int, after_context: int, max_matches: int,
        output_mode: str = "matches", offset: int = 0,
    ) -> ToolResult | None:
        """Locate candidate lines with ripgrep and answer in the walker's shape.

        ripgrep is what a coding agent's ``rg`` habit is built on: it honours the
        project's own ignore files, skips binaries without reading them, and
        stays fast on a large tree. It is a *locator* here, not a second result
        format -- the payload is identical to the in-tree walker's, so evidence
        accounting cannot tell the two apart. ``None`` means "not trustworthy
        for this search" and the walker answers instead.
        """

        command = [
            binary, "--null", "--no-heading", "--color", "never",
            "--with-filename", "--no-messages",
            f"--max-filesize={_MAX_SEARCH_FILE_BYTES}",
            "--case-sensitive" if case_sensitive else "--ignore-case",
        ]
        if output_mode == "count":
            # Native counting: ripgrep reports per-file totals without any line
            # text, so this shape never has to carry matches at all.
            command.append("--count")
        elif output_mode == "paths":
            command.append("--files-with-matches")
        else:
            command.extend([
                "--line-number", f"--max-count={offset + max_matches}",
            ])
        if not use_regex:
            command.append("--fixed-strings")
        if include_glob:
            command.extend(["--glob", include_glob])
        if exclude_glob:
            command.extend(["--glob", f"!{exclude_glob}"])
        # ripgrep knows the project's ignore files, not this Runtime's fixed
        # pruned set, so the walker's exclusions are restated here.
        for name in sorted(EXCLUDED_DIRECTORIES):
            command.extend(["--glob", f"!{name}/**"])
        command.extend(["--", query, str(root)])
        try:
            completed = subprocess.run(
                command, capture_output=True, check=False,
                cwd=str(root), timeout=_RIPGREP_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode not in (0, 1):
            return None

        matches: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        paths_seen: dict[str, None] = {}
        counted_paths: set[str] = set()
        line_cache: dict[Path, list[str] | None] = {}
        skipped_files = 0
        truncated = False
        if output_mode == "paths":
            # ``--files-with-matches`` emits NUL-separated paths and no newlines.
            entries = [
                part for part in
                completed.stdout.decode("utf-8", errors="replace").split("\x00")
                if part
            ]
        else:
            entries = completed.stdout.decode(
                "utf-8", errors="replace"
            ).splitlines()
        for entry in entries:
            # ``--null`` separates the path from ``line:text``/``count`` so a
            # path that itself contains a colon cannot be misread.
            if output_mode == "paths":
                path_text, separator, rest = entry, "\x00", ""
            else:
                path_text, separator, rest = entry.partition("\x00")
            if not separator:
                continue
            if output_mode == "count":
                line_text, _, _content = rest.partition(":")
            else:
                line_text, _, content = rest.partition(":")
            line_number = 0
            if output_mode != "paths":
                try:
                    line_number = int(line_text)
                except ValueError:
                    continue
            path = Path(path_text)
            if not path.is_absolute():
                # ripgrep prints a path the way it was given; a relative one is
                # relative to the directory it searched.
                path = root / path
            try:
                relative = self._safe_relative(context, path)
                if self._is_sensitive(relative):
                    skipped_files += 1
                    continue
                relative_path = relative.as_posix()
            except (_WorkspaceAccessError, _SensitivePathError):
                skipped_files += 1
                continue
            if output_mode == "paths":
                paths_seen.setdefault(relative_path, None)
                continue
            if output_mode == "count":
                counts[relative_path] = line_number
                continue
            if len(matches) >= offset + max_matches:
                truncated = True
                break
            counted_paths.add(relative_path)
            match: dict[str, Any] = {
                "path": relative_path,
                **self._resource_data(context, path, resolved.workspace),
                "line": line_number,
                "text": content[:_MAX_MATCH_TEXT_CHARS],
            }
            if before_context or after_context:
                if path not in line_cache:
                    line_cache[path] = self._cached_lines(path)
                lines = line_cache[path]
                if lines is not None and before_context:
                    start = max(0, line_number - before_context - 1)
                    match["before_context"] = lines[start:line_number - 1]
                if lines is not None and after_context:
                    end = min(len(lines), line_number + after_context)
                    match["after_context"] = lines[line_number:end]
            matches.append(match)

        payload, page_truncated = self._search_payload(
            output_mode, matches, counts, list(paths_seen), offset, max_matches,
        )
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={
                "search_target": "content",
                "search_engine": "ripgrep",
                "output_mode": output_mode,
                "query": query,
                "path": self._display_path(resolved.workspace, root),
                **self._resolved_path_data(context, raw_path, resolved),
                **payload,
                # ripgrep reports matches, not how much it walked, so this
                # counts the files that matched rather than files read.
                "scanned_files": (
                    len(paths_seen) or len(counts) or len(counted_paths)
                ),
                "skipped_files": skipped_files,
                "generated_directories_skipped": 0,
            },
            truncated=truncated or page_truncated,
            meta={"untrusted_data": True},
        )

    @staticmethod
    def _cached_lines(path: Path) -> list[str] | None:
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in raw:
            return None
        try:
            return raw.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _walk(root: Path) -> Iterable[Path]:
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            # Prune state and generated directories before descending. Without
            # this, ``list_files(recursive=True)`` returned ``.agent/`` -- the
            # Runtime's own SQLite state -- and ``node_modules/`` as project
            # entries, so the model read its own ledger and reported it as
            # project content. The filter prunes *recursion only*: an explicit
            # path into one of these still lists, which keeps targeted access.
            directory_names[:] = sorted(
                name for name in directory_names
                if name.casefold() not in EXCLUDED_DIRECTORIES
            )
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
                if child.name.casefold() in EXCLUDED_DIRECTORIES:
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
                if name.casefold() in EXCLUDED_DIRECTORIES:
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
    def _optional_string_argument(
        arguments: Mapping[str, Any], name: str,
    ) -> str | None:
        value = arguments.get(name)
        if value is None:
            return None
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
