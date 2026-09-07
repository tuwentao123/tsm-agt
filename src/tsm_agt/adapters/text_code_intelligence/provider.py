"""Dependency-free, bounded code index used as the built-in fallback."""

from __future__ import annotations

import ast
import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus, WorkspacePathPort,
)

_LANGUAGES = {
    ".py": "python", ".kt": "kotlin", ".kts": "kotlin",
    ".java": "java", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".go": "go",
    ".rs": "rust", ".swift": "swift", ".c": "c",
    ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
}
_EXCLUDED_DIRS = frozenset({
    ".agent", ".git", ".gradle", ".idea", ".venv", "build",
    "dist", "node_modules", "target", "vendor", "__pycache__",
})
_SENSITIVE_NAMES = frozenset({
    ".npmrc", ".pypirc", "credentials", "credentials.json",
    "id_ed25519", "id_rsa",
})
_SENSITIVE_SUFFIXES = frozenset({
    ".jks", ".key", ".keystore", ".p12", ".pem", ".pfx",
})
_MAX_FILES = 5000
_MAX_FILE_BYTES = 1_000_000
_MAX_TOTAL_BYTES = 30_000_000
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_SYMBOL_PATTERNS = (
    re.compile(r"^\s*(?:export\s+)?(?:public\s+|private\s+|protected\s+|internal\s+|abstract\s+|final\s+|open\s+|sealed\s+|data\s+|static\s+)*(?P<kind>class|interface|enum|object|trait|struct)\s+(?P<name>[A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:public\s+|private\s+|protected\s+|internal\s+|static\s+|final\s+|open\s+|override\s+|async\s+)*(?P<kind>def|fun|fn|func|function)\s+(?P<name>[A-Za-z_$][\w$]*)"),
    # Java/C-family method with an explicit return type. Requiring parentheses
    # and excluding control keywords keeps the fallback conservative.
    re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|final\s+|abstract\s+|synchronized\s+|native\s+)*(?P<return>[A-Za-z_$][\w$<>,.?\[\] ]*)\s+(?P<name>[A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?:\{|throws\b|$)", re.ASCII),
    re.compile(r"^\s*(?:export\s+)?(?P<kind>type)\s+(?P<name>[A-Za-z_$][\w$]*)"),
)


@dataclass(frozen=True, slots=True)
class _SourceFile:
    path: str
    language: str
    text: str
    sha256: str


@dataclass(frozen=True, slots=True)
class _Symbol:
    name: str
    kind: str
    path: str
    line: int
    column: int
    language: str
    signature: str

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "path": self.path,
            "line": self.line, "column": self.column,
            "language": self.language, "signature": self.signature,
        }


@dataclass(frozen=True, slots=True)
class _Index:
    workspace_key: str
    fingerprint: str
    version: str
    files: tuple[_SourceFile, ...]
    symbols: tuple[_Symbol, ...]
    skipped_files: int
    truncated: bool


class TextCodeIntelligenceProvider:
    descriptor = AdapterDescriptor(
        "builtin.text-code-intelligence", "0.1.0",
        "CodeIntelligencePort", "1.0",
        frozenset({
            "definition", "diagnostics", "implementations", "index-refresh",
            "references", "rename-preview", "symbol-overview",
            "workspace-symbols",
        }),
    )

    def __init__(self, workspace_path: WorkspacePathPort) -> None:
        self._workspace_path = workspace_path
        self._started = False
        self._cache: dict[str, _Index] = {}

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "text code intelligence ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._cache.clear()
        self._started = False

    async def symbol_overview(
        self, workspace: Path, path: str, *, limit: int = 200,
    ) -> Mapping[str, Any]:
        index, refreshed = self._index(workspace)
        normalized = self._scope_path(workspace, path)
        symbols = [item.to_data() for item in index.symbols if item.path == normalized]
        return self._result(index, refreshed, "symbols", symbols[:limit],
                            truncated=len(symbols) > limit, path=normalized)

    async def definition(
        self, workspace: Path, symbol: str, *, path: str | None = None,
        limit: int = 50,
    ) -> Mapping[str, Any]:
        self._validate_symbol(symbol)
        index, refreshed = self._index(workspace)
        scope = self._optional_scope(workspace, path)
        matches = [item.to_data() for item in index.symbols
                   if item.name == symbol and self._in_scope(item.path, scope)]
        return self._result(index, refreshed, "definitions", matches[:limit],
                            truncated=len(matches) > limit, symbol=symbol)

    async def references(
        self, workspace: Path, symbol: str, *, path: str | None = None,
        include_declaration: bool = True, limit: int = 200,
    ) -> Mapping[str, Any]:
        self._validate_symbol(symbol)
        index, refreshed = self._index(workspace)
        scope = self._optional_scope(workspace, path)
        declaration_locations = {
            (item.path, item.line, item.column) for item in index.symbols
            if item.name == symbol
        }
        pattern = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(symbol)}(?![A-Za-z0-9_$])")
        matches: list[dict[str, Any]] = []
        truncated = False
        for source in index.files:
            if not self._in_scope(source.path, scope):
                continue
            for line_number, line in enumerate(source.text.splitlines(), start=1):
                for match in pattern.finditer(line):
                    location = (source.path, line_number, match.start() + 1)
                    declaration = location in declaration_locations
                    if declaration and not include_declaration:
                        continue
                    if len(matches) >= limit:
                        truncated = True
                        break
                    matches.append({
                        "path": source.path, "line": line_number,
                        "column": match.start() + 1,
                        "declaration": declaration, "text": line[:500],
                    })
                if truncated:
                    break
            if truncated:
                break
        return self._result(index, refreshed, "references", matches,
                            truncated=truncated, symbol=symbol)

    async def implementations(
        self, workspace: Path, symbol: str, *, limit: int = 100,
    ) -> Mapping[str, Any]:
        self._validate_symbol(symbol)
        index, refreshed = self._index(workspace)
        word = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(symbol)}(?![A-Za-z0-9_$])")
        results = []
        for item in index.symbols:
            if item.kind not in {"class", "interface", "object", "struct", "impl"}:
                continue
            suffix = item.signature[item.signature.find(item.name) + len(item.name):]
            if word.search(suffix) and item.name != symbol:
                results.append(item.to_data())
        return self._result(index, refreshed, "implementations", results[:limit],
                            truncated=len(results) > limit, symbol=symbol)

    async def workspace_symbols(
        self, workspace: Path, query: str, *, limit: int = 200,
    ) -> Mapping[str, Any]:
        index, refreshed = self._index(workspace)
        normalized = query.strip().casefold()
        results = [item.to_data() for item in index.symbols
                   if not normalized or normalized in item.name.casefold()]
        return self._result(index, refreshed, "symbols", results[:limit],
                            truncated=len(results) > limit, query=query)

    async def diagnostics(
        self, workspace: Path, *, path: str | None = None, limit: int = 200,
    ) -> Mapping[str, Any]:
        index, refreshed = self._index(workspace)
        scope = self._optional_scope(workspace, path)
        results: list[dict[str, Any]] = []
        checked = 0
        for source in index.files:
            if source.language != "python" or not self._in_scope(source.path, scope):
                continue
            checked += 1
            try:
                ast.parse(source.text, filename=source.path)
            except SyntaxError as error:
                results.append({
                    "path": source.path, "line": error.lineno or 1,
                    "column": error.offset or 1, "severity": "error",
                    "code": "python.syntax", "message": error.msg,
                })
        return self._result(index, refreshed, "diagnostics", results[:limit],
                            truncated=len(results) > limit, checked_files=checked,
                            coverage="python-syntax")

    async def rename_preview(
        self, workspace: Path, symbol: str, new_name: str, *,
        path: str | None = None, limit: int = 200,
    ) -> Mapping[str, Any]:
        self._validate_symbol(symbol)
        self._validate_symbol(new_name)
        index, refreshed = self._index(workspace)
        scope = self._optional_scope(workspace, path)
        conflicts = [item.to_data() for item in index.symbols
                     if item.name == new_name and self._in_scope(item.path, scope)]
        references = await self.references(
            workspace, symbol, path=path, include_declaration=True, limit=limit
        )
        edits = []
        for item in references["references"]:
            before = str(item["text"])
            column = int(item["column"]) - 1
            edits.append({
                "path": item["path"], "line": item["line"],
                "column": item["column"], "old_text": symbol,
                "new_text": new_name,
                "line_preview": before[:column] + new_name + before[column + len(symbol):],
            })
        data = self._result(
            index, refreshed or bool(references["index"]["refreshed"]),
            "edits", edits, truncated=bool(references["truncated"]),
            symbol=symbol, new_name=new_name, conflicts=conflicts,
            preview_complete=not references["truncated"],
            safe_to_apply=False,
            safety_note=(
                "lexical fallback preview requires semantic review before applying"
            ),
            applied=False,
        )
        return data

    def _index(self, workspace: Path) -> tuple[_Index, bool]:
        if not self._started:
            raise RuntimeError("code intelligence adapter is not started")
        root = self._workspace_path.normalize_workspace(workspace)
        key = self._workspace_path.workspace_key(root)
        files: list[_SourceFile] = []
        skipped = 0
        total_bytes = 0
        truncated = False
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_names[:] = sorted(
                name for name in directory_names
                if name.casefold() not in _EXCLUDED_DIRS
            )
            for name in sorted(file_names):
                path = Path(directory) / name
                lowered = name.casefold()
                if (
                    lowered in _SENSITIVE_NAMES
                    or lowered == ".env" or lowered.startswith(".env.")
                    or path.suffix.casefold() in _SENSITIVE_SUFFIXES
                ):
                    skipped += 1
                    continue
                language = _LANGUAGES.get(path.suffix.casefold())
                if language is None:
                    continue
                if len(files) >= _MAX_FILES or total_bytes >= _MAX_TOTAL_BYTES:
                    truncated = True
                    break
                try:
                    relative = path.relative_to(root).as_posix()
                    resolved = self._workspace_path.resolve_access_path(root, relative)
                    if self._workspace_path.is_link_like(resolved.path):
                        skipped += 1
                        continue
                    size = resolved.path.stat().st_size
                    if size > _MAX_FILE_BYTES or total_bytes + size > _MAX_TOTAL_BYTES:
                        skipped += 1
                        continue
                    raw = resolved.path.read_bytes()
                    if b"\x00" in raw:
                        skipped += 1
                        continue
                    text = raw.decode("utf-8")
                except (OSError, UnicodeDecodeError, PermissionError, ValueError):
                    skipped += 1
                    continue
                total_bytes += len(raw)
                files.append(_SourceFile(
                    resolved.relative_path, language, text, hashlib.sha256(raw).hexdigest()
                ))
            if truncated:
                break
        files.sort(key=lambda item: item.path)
        fingerprint_body = "\n".join(
            f"{item.path}\0{item.sha256}" for item in files
        ) + f"\nskipped={skipped};truncated={int(truncated)}"
        fingerprint = hashlib.sha256(fingerprint_body.encode("utf-8")).hexdigest()
        cached = self._cache.get(key)
        if cached is not None and cached.fingerprint == fingerprint:
            return cached, False
        symbols = tuple(
            symbol for source in files for symbol in self._symbols(source)
        )
        index = _Index(
            key, fingerprint, f"text-v1-{fingerprint[:16]}", tuple(files),
            symbols, skipped, truncated,
        )
        self._cache[key] = index
        return index, True

    @staticmethod
    def _symbols(source: _SourceFile) -> tuple[_Symbol, ...]:
        results = []
        for line_number, line in enumerate(source.text.splitlines(), start=1):
            for pattern in _SYMBOL_PATTERNS:
                match = pattern.search(line)
                if match is None:
                    continue
                name = match.group("name")
                kind = match.groupdict().get("kind") or "method"
                if name in {"if", "for", "while", "switch", "catch", "return"}:
                    continue
                if kind == "method" and line.lstrip().startswith((
                    "return ", "throw ", "new ", "if ", "for ", "while ",
                )):
                    continue
                results.append(_Symbol(
                    name, kind, source.path, line_number,
                    match.start("name") + 1, source.language, line.strip()[:500],
                ))
                break
        return tuple(results)

    def _scope_path(self, workspace: Path, path: str) -> str:
        resolved = self._workspace_path.resolve_access_path(workspace, path)
        if not resolved.path.is_file():
            raise ValueError(f"code path is not a file: {path}")
        return resolved.relative_path

    def _optional_scope(self, workspace: Path, path: str | None) -> str | None:
        if path is None:
            return None
        return self._workspace_path.resolve_access_path(workspace, path).relative_path

    @staticmethod
    def _in_scope(path: str, scope: str | None) -> bool:
        return scope is None or path == scope or path.startswith(scope.rstrip("/") + "/")

    @staticmethod
    def _validate_symbol(symbol: str) -> None:
        if _IDENTIFIER.fullmatch(symbol) is None:
            raise ValueError("symbol must be one identifier")

    @staticmethod
    def _result(
        index: _Index, refreshed: bool, field: str, values: list[dict[str, Any]],
        *, truncated: bool, **extra: Any,
    ) -> dict[str, Any]:
        languages: dict[str, int] = {}
        for source in index.files:
            languages[source.language] = languages.get(source.language, 0) + 1
        return {
            **extra, field: values, "truncated": truncated,
            "index": {
                "workspace_fingerprint": index.fingerprint,
                "index_version": index.version, "refreshed": refreshed,
                "indexed_files": len(index.files),
                "skipped_files": index.skipped_files,
                "index_truncated": index.truncated, "languages": languages,
            },
        }
