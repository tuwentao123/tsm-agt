"""Structured, read-only Git inspection tools.

The interface intentionally follows Codex's dedicated ``git`` tool direction:
Git inspection should not require a model to synthesize shell argv.  Unlike the
general Codex example, this provider exposes only fixed read-only subcommands.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus, ToolCall,
    ToolEffect, ToolIdempotency, ToolInvocationContext, ToolResult,
    ToolResultAuthority, ToolRisk, ToolSpec,
)

_MAX_OUTPUT_BYTES = 80_000
_MAX_LOG_ENTRIES = 100


class GitReadOnlyToolProvider:
    """Run a small allowlist of Git reads within the task workspace."""

    descriptor = AdapterDescriptor(
        "builtin.git-readonly-tools", "0.1.0", "ToolProviderPort", "1.0",
        frozenset({"git.status", "git.diff", "git.log", "git.show", "git.blame", "git.branches"}),
    )

    @staticmethod
    def _spec(name: str, description: str, properties: Mapping[str, Any], required: tuple[str, ...] = ()) -> ToolSpec:
        schema: dict[str, Any] = {"type": "object", "properties": dict(properties), "additionalProperties": False}
        if required:
            schema["required"] = list(required)
        return ToolSpec(
            name, description, schema, ToolRisk.R0, is_read_only=True,
            is_concurrency_safe=True, idempotency=ToolIdempotency.IDEMPOTENT,
            effect=ToolEffect.OBSERVE, result_authority=ToolResultAuthority.WORKSPACE_FACT,
        )

    _tools = (
        _spec.__func__("git.status", "Read repository branch and working-tree status. Never stages, restores, or changes files.", {"include_untracked": {"type": "boolean"}}),
        _spec.__func__("git.diff", "Read a bounded unified diff for the working tree, staged index, or an optional base revision. Use git.show for one historical file.", {"base": {"type": "string"}, "path": {"type": "string"}, "staged": {"type": "boolean"}}),
        _spec.__func__("git.log", "Read recent commit metadata for an optional revision or path. Returns structured commit fields, not a shell transcript.", {"ref": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LOG_ENTRIES}}),
        _spec.__func__("git.show", "Read one revision's metadata and optionally one file's content/diff. This is read-only; revision must be explicit.", {"revision": {"type": "string"}, "path": {"type": "string"}}, ("revision",)),
        _spec.__func__("git.blame", "Read bounded line attribution for one tracked file. Use it to locate the commit that introduced a line.", {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}}, ("path",)),
        _spec.__func__("git.branches", "List local and remote branch refs with their upstream and HEAD marker. Never switches branches.", {"include_remote": {"type": "boolean"}}),
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY if self._started else HealthState.UNHEALTHY, "git read-only tools ready" if self._started else "not started")

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return self._tools

    async def invoke(self, call: ToolCall, context: ToolInvocationContext) -> ToolResult:
        if not self._started:
            raise RuntimeError("adapter is not started")
        handlers = {"git.status": self._status, "git.diff": self._diff, "git.log": self._log, "git.show": self._show, "git.blame": self._blame, "git.branches": self._branches}
        handler = handlers.get(call.name)
        if handler is None:
            return self._error(call, "NOT_FOUND", f"unknown git tool: {call.name}")
        try:
            return handler(call, context.workspace)
        except (TypeError, ValueError) as error:
            return self._error(call, "INVALID_PARAM", str(error))
        except OSError as error:
            return self._error(call, "TOOL_FAILED", str(error), retryable=True)

    def _status(self, call: ToolCall, workspace: Path) -> ToolResult:
        args = ["status", "--short", "--branch", "--untracked-files=" + ("all" if call.arguments.get("include_untracked", True) else "no")]
        return self._command(call, workspace, args, lambda text: {"status": text.splitlines()})

    def _diff(self, call: ToolCall, workspace: Path) -> ToolResult:
        args = ["diff", "--no-ext-diff", "--unified=3"]
        if call.arguments.get("staged"):
            args.append("--cached")
        if base := self._optional_revision(call.arguments, "base"):
            args.append(base)
        if path := self._optional_path(call.arguments, "path"):
            args.extend(["--", path])
        return self._command(call, workspace, args, lambda text: {"diff": text})

    def _log(self, call: ToolCall, workspace: Path) -> ToolResult:
        limit = self._bounded_int(call.arguments, "limit", 20, 1, _MAX_LOG_ENTRIES)
        args = ["log", f"--max-count={limit}", "--format=%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s"]
        if ref := self._optional_revision(call.arguments, "ref"):
            args.append(ref)
        if path := self._optional_path(call.arguments, "path"):
            args.extend(["--", path])
        def parse(text: str) -> dict[str, Any]:
            commits = []
            for line in text.splitlines():
                fields = line.split("\x1f", 5)
                if len(fields) == 6:
                    commits.append(dict(zip(("hash", "short_hash", "author", "author_email", "authored_at", "subject"), fields, strict=True)))
            return {"commits": commits}
        return self._command(call, workspace, args, parse)

    def _show(self, call: ToolCall, workspace: Path) -> ToolResult:
        revision = self._required_revision(call.arguments, "revision")
        args = ["show", "--no-ext-diff", "--format=fuller", "--end-of-options", revision]
        if path := self._optional_path(call.arguments, "path"):
            args.extend(["--", path])
        return self._command(call, workspace, args, lambda text: {"revision": revision, "content": text})

    def _blame(self, call: ToolCall, workspace: Path) -> ToolResult:
        path = self._optional_path(call.arguments, "path", required=True)
        start = self._bounded_int(call.arguments, "start_line", 1, 1, 100_000)
        end = self._bounded_int(call.arguments, "end_line", start + 199, start, 100_000)
        return self._command(call, workspace, ["blame", "--porcelain", f"-L{start},{end}", "--", path], lambda text: {"path": path, "start_line": start, "end_line": end, "porcelain": text})

    def _branches(self, call: ToolCall, workspace: Path) -> ToolResult:
        args = ["branch", "--format=%(HEAD)%x1f%(refname:short)%x1f%(upstream:short)%x1f%(objectname:short)"]
        if call.arguments.get("include_remote", True):
            args.append("--all")
        def parse(text: str) -> dict[str, Any]:
            return {"branches": [dict(zip(("head", "name", "upstream", "commit"), line.split("\x1f", 3), strict=True)) for line in text.splitlines() if line]}
        return self._command(call, workspace, args, parse)

    def _command(self, call: ToolCall, workspace: Path, args: list[str], parse) -> ToolResult:
        completed = subprocess.run(["git", *args], cwd=workspace, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
        stdout = completed.stdout[:_MAX_OUTPUT_BYTES].decode("utf-8", "replace")
        stderr = completed.stderr[:4_000].decode("utf-8", "replace").strip()
        if completed.returncode:
            code = "NOT_REPOSITORY" if "not a git repository" in stderr.lower() else "GIT_FAILED"
            return self._error(call, code, stderr or f"git exited {completed.returncode}")
        data = parse(stdout)
        data.update({"workspace": str(workspace), "truncated": len(completed.stdout) > _MAX_OUTPUT_BYTES})
        return ToolResult(call.call_id, True, data=data)

    @staticmethod
    def _required_text(arguments: Mapping[str, Any], key: str) -> str:
        value = GitReadOnlyToolProvider._optional_text(arguments, key)
        if not value:
            raise ValueError(f"{key} is required")
        return value

    @staticmethod
    def _required_revision(arguments: Mapping[str, Any], key: str) -> str:
        value = GitReadOnlyToolProvider._optional_revision(arguments, key)
        if not value:
            raise ValueError(f"{key} is required")
        return value

    @staticmethod
    def _optional_revision(arguments: Mapping[str, Any], key: str) -> str | None:
        value = GitReadOnlyToolProvider._optional_text(arguments, key)
        if value is not None and value.startswith("-"):
            raise ValueError(f"{key} must not start with '-'")
        return value

    @staticmethod
    def _optional_text(arguments: Mapping[str, Any], key: str) -> str | None:
        value = arguments.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError(f"{key} must be a non-empty text value")
        return value.strip()

    @staticmethod
    def _optional_path(arguments: Mapping[str, Any], key: str, required: bool = False) -> str:
        value = GitReadOnlyToolProvider._optional_text(arguments, key)
        if value is None:
            if required:
                raise ValueError(f"{key} is required")
            return ""
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"{key} must be workspace-relative")
        return value

    @staticmethod
    def _bounded_int(arguments: Mapping[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
        value = arguments.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be an integer in {minimum}..{maximum}")
        return value

    @staticmethod
    def _error(call: ToolCall, code: str, message: str, retryable: bool = False) -> ToolResult:
        return ToolResult(call.call_id, False, error_code=code, message=message, retryable=retryable)
