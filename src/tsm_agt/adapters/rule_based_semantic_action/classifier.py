"""Conservative, project-neutral semantic action classification."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
    SemanticAction, SemanticActionFamily, SemanticScopeKind, ToolCall,
)


class RuleBasedSemanticActionClassifier:
    descriptor = AdapterDescriptor(
        "builtin.rule-based-semantic-action", "1.0.0",
        "SemanticActionClassifierPort", "1.0",
        frozenset({"project-neutral", "redacted-events"}),
    )
    _VERSION = "rules-v1"
    _DECLARATION_WORDS = frozenset({
        "class", "interface", "struct", "enum", "object", "trait",
        "def", "func", "fun", "function", "type", "record",
    })
    _DEFINITION_HINTS = (
        "definition", "defined", "declared", "declaration",
        "where is", "定义", "声明", "在哪里",
    )
    _REFERENCE_HINTS = (
        "reference", "references", "caller", "callers", "usage",
        "used by", "invoked", "调用", "引用", "使用方", "入口",
    )
    _VERIFICATION_HINTS = (
        "verify", "verification", "test", "tests", "build", "lint",
        "check", "验证", "测试", "构建", "检查",
    )
    _CONFIG_SUFFIXES = frozenset({
        ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg",
        ".conf", ".properties", ".xml",
    })
    _MUTATION_TOOLS = frozenset({
        "core.apply_patch", "core.apply_patches", "core.delete_file",
        "core.rollback_mutation", "core.rollback_mutations",
        "core.rollback_mutation_batch", "core.rollback_mutation_groups",
    })
    _PROCESS_TOOLS = frozenset({
        "core.process_status", "core.process_logs", "core.process_stop",
    })

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "semantic action classifier ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def classify(self, call: ToolCall) -> SemanticAction:
        if not self._started:
            raise RuntimeError("semantic action classifier is not started")
        family, target_kind, target, confidence = self._family_target(call)
        scope_kind, scope = self._scope(call)
        target_hash = self._hash({"kind": target_kind, "value": target})
        scope_hash = self._hash({"kind": scope_kind.value, "value": scope})
        semantic_target = (
            call.evidence_question.question_id.strip().casefold()
            if call.evidence_question is not None else target
        )
        semantic_signature = self._hash({
            "family": family.value,
            "target": semantic_target,
        })
        return SemanticAction(
            family, target_kind, target_hash, scope_kind, scope_hash,
            semantic_signature, self.descriptor.adapter_id, self._VERSION, confidence,
        )

    def _family_target(
        self, call: ToolCall
    ) -> tuple[SemanticActionFamily, str, str, float]:
        name = call.name
        arguments = call.arguments
        question = (
            call.evidence_question.question.casefold()
            if call.evidence_question is not None else ""
        )
        if name == "core.list_files":
            return (SemanticActionFamily.INSPECT_PROJECT_STRUCTURE, "structure",
                    "project-structure", 0.98)
        if name in {"core.search_text", "core.find_files"}:
            raw_query = arguments.get(
                "query", arguments.get("pattern", "")
            )
            query = self._normalize_query(str(raw_query))
            if self._contains(question, self._REFERENCE_HINTS):
                family = SemanticActionFamily.SEARCH_REFERENCES
            elif (
                self._contains(question, self._DEFINITION_HINTS)
                or name == "core.find_files"
                or self._looks_like_declaration(str(raw_query))
            ):
                family = SemanticActionFamily.SEARCH_DEFINITION
            else:
                family = SemanticActionFamily.SEARCH_CONCEPT
            return family, "concept", query or self._question_or("text", call), 0.9
        if name in {"code.definition", "code.workspace_symbols"}:
            target = arguments.get("symbol", arguments.get("query", "symbol"))
            return (SemanticActionFamily.SEARCH_DEFINITION, "symbol",
                    self._normalize_query(str(target)), 0.99)
        if name in {"code.references", "code.implementations"}:
            return (SemanticActionFamily.SEARCH_REFERENCES, "symbol",
                    self._normalize_query(str(arguments.get("symbol", "symbol"))),
                    0.99)
        if name in {"core.read_file", "code.symbol_overview"}:
            path = self._normalize_path(str(arguments.get("path", "artifact")))
            family = (
                SemanticActionFamily.INSPECT_CONFIGURATION
                if PurePosixPath(path).suffix.casefold() in self._CONFIG_SUFFIXES
                else SemanticActionFamily.READ_ARTIFACT
            )
            return family, "artifact", path, 0.98
        if name == "code.diagnostics":
            return (SemanticActionFamily.EXECUTE_VERIFICATION, "diagnostics",
                    self._normalize_path(str(arguments.get("path", "workspace"))),
                    0.98)
        if name == "code.rename_preview":
            target = f"{arguments.get('symbol', '')}->{arguments.get('new_name', '')}"
            return SemanticActionFamily.MUTATE_WORKSPACE, "rename-preview", target, 0.95
        if name in self._MUTATION_TOOLS:
            target = self._mutation_target(arguments)
            return SemanticActionFamily.MUTATE_WORKSPACE, "workspace-target", target, 0.99
        if name == "core.run_command":
            argv = arguments.get("argv", ())
            command = (
                json.dumps(list(argv), ensure_ascii=False, separators=(",", ":"))
                if isinstance(argv, (list, tuple)) and argv else "command"
            )
            family = (
                SemanticActionFamily.EXECUTE_VERIFICATION
                if self._contains(question, self._VERIFICATION_HINTS)
                else SemanticActionFamily.EXECUTE_COMMAND
            )
            return family, "command", command.casefold(), 0.88
        if name in self._PROCESS_TOOLS:
            return (SemanticActionFamily.CONTROL_PROCESS, "process",
                    str(arguments.get("process_id", "process")), 0.99)
        if name == "core.request_input":
            return (SemanticActionFamily.REQUEST_CLARIFICATION, "question",
                    self._question_or("clarification", call), 0.99)
        if name.startswith(("core.memory_", "core.working_memory_", "core.task_spec_")):
            return SemanticActionFamily.MANAGE_AGENT_STATE, "agent-state", name, 0.99
        return (SemanticActionFamily.USE_TOOL, "tool", name.casefold(), 0.5)

    def _scope(self, call: ToolCall) -> tuple[SemanticScopeKind, str]:
        arguments = call.arguments
        path = arguments.get("path", arguments.get("cwd"))
        if isinstance(path, str) and path.strip():
            normalized = self._normalize_path(path)
            if normalized in {".", "workspace"}:
                return SemanticScopeKind.WORKSPACE, "workspace"
            suffix = PurePosixPath(normalized).suffix
            return (
                SemanticScopeKind.FILE if suffix else SemanticScopeKind.DIRECTORY,
                normalized,
            )
        if isinstance(arguments.get("symbol"), str):
            return SemanticScopeKind.SYMBOL, self._normalize_query(arguments["symbol"])
        if call.name.startswith(("core.memory_", "core.working_memory_", "core.task_spec_")):
            return SemanticScopeKind.INTERNAL, "agent-state"
        if call.name.startswith("core.process_"):
            return SemanticScopeKind.EXTERNAL, "process"
        return SemanticScopeKind.UNKNOWN, "unknown"

    @classmethod
    def _normalize_query(cls, value: str) -> str:
        tokens = re.findall(r"[\w.$:/-]+", value.casefold(), flags=re.UNICODE)
        meaningful = [token for token in tokens if token not in cls._DECLARATION_WORDS]
        return " ".join(meaningful).strip()

    @classmethod
    def _looks_like_declaration(cls, value: str) -> bool:
        tokens = re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE)
        return bool(tokens and tokens[0] in cls._DECLARATION_WORDS)

    @staticmethod
    def _normalize_path(value: str) -> str:
        normalized = value.strip().replace("\\", "/") or "."
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        return normalized.rstrip("/") or "."

    @staticmethod
    def _contains(value: str, hints: tuple[str, ...]) -> bool:
        return any(hint in value for hint in hints)

    @classmethod
    def _question_or(cls, fallback: str, call: ToolCall) -> str:
        if call.evidence_question is None:
            return fallback
        return "question:" + cls._hash(call.evidence_question.question.casefold())

    @classmethod
    def _mutation_target(cls, arguments: Mapping[str, Any]) -> str:
        path = arguments.get("path")
        if isinstance(path, str) and path.strip():
            return cls._normalize_path(path)
        paths = []
        patches = arguments.get("patches")
        if isinstance(patches, (list, tuple)):
            for patch in patches:
                if isinstance(patch, Mapping) and isinstance(patch.get("path"), str):
                    paths.append(cls._normalize_path(patch["path"]))
        return "|".join(sorted(paths)) or "mutation-journal"

    @staticmethod
    def _hash(value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
