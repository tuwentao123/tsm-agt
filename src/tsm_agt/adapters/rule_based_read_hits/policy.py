"""Conservative project-neutral READ_HITS policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
    ReadHitsAction, ReadHitsDecision, ReadHitsState, ReadHitsUpdate,
    SemanticAction, SemanticActionFamily, ToolCall, ToolResult,
)


class RuleBasedReadHitsPolicy:
    """Require one bounded candidate to be consumed before another same-goal search."""

    descriptor = AdapterDescriptor(
        "builtin.rule-based-read-hits", "1.0.0",
        "ReadHitsPolicyPort", "1.0",
        frozenset({"project-neutral", "checkpointed", "redacted-events"}),
    )
    _MAX_CANDIDATE_PATHS = 5
    _SEARCH_FAMILIES = frozenset({
        SemanticActionFamily.SEARCH_CONCEPT,
        SemanticActionFamily.SEARCH_DEFINITION,
        SemanticActionFamily.SEARCH_REFERENCES,
    })
    _SEARCH_TOOLS = frozenset({
        "core.search_text", "core.find_files",
        "code.definition", "code.references",
        "code.implementations", "code.workspace_symbols",
    })
    _CONSUMER_TOOLS = frozenset({
        "core.read_file", "code.symbol_overview",
    })
    _MUTATION_TOOLS = frozenset({
        "core.apply_patch", "core.apply_patches", "core.delete_file",
        "core.rollback_mutation", "core.rollback_mutations",
        "core.rollback_mutation_batch", "core.rollback_mutation_groups",
    })
    _PATH_COLLECTIONS = (
        "matches", "definitions", "references", "implementations",
        "symbols",
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "read-hits policy ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def before_call(
        self, call: ToolCall, semantic_action: SemanticAction | None,
        state: ReadHitsState,
    ) -> ReadHitsDecision:
        self._require_started()
        if not state.active:
            return ReadHitsDecision(ReadHitsAction.ALLOW, "no_pending_hits")
        question_hash = self._question_hash(call)
        if not question_hash or question_hash != state.question_hash:
            return ReadHitsDecision(
                ReadHitsAction.ALLOW, "different_evidence_question",
                len(state.candidate_path_hashes), state.question_hash,
            )
        if self._consumes_candidate(call, state):
            return ReadHitsDecision(
                ReadHitsAction.ALLOW, "candidate_read",
                len(state.candidate_path_hashes), state.question_hash,
            )
        if self._is_search(call, semantic_action):
            return ReadHitsDecision(
                ReadHitsAction.REQUIRE_READ, "same_question_has_bounded_hits",
                len(state.candidate_path_hashes), state.question_hash,
            )
        return ReadHitsDecision(
            ReadHitsAction.ALLOW, "non_search_action",
            len(state.candidate_path_hashes), state.question_hash,
        )

    async def after_result(
        self, call: ToolCall, result: ToolResult,
        semantic_action: SemanticAction | None, state: ReadHitsState,
    ) -> ReadHitsUpdate:
        self._require_started()
        if result.ok and state.active and self._consumes_candidate(call, state):
            return ReadHitsUpdate(ReadHitsState(), "consumed")
        if (
            result.ok and (
                call.name in self._MUTATION_TOOLS
                or semantic_action is not None
                and semantic_action.family is SemanticActionFamily.MUTATE_WORKSPACE
            )
        ):
            return ReadHitsUpdate(ReadHitsState(), "workspace_changed")
        if not result.ok or not self._is_search(call, semantic_action):
            return ReadHitsUpdate(state, "unchanged", len(state.candidate_path_hashes))
        candidates = self._candidate_hashes(result)
        if result.truncated or not 1 <= len(candidates) <= self._MAX_CANDIDATE_PATHS:
            return ReadHitsUpdate(ReadHitsState(), "not_bounded", len(candidates))
        question_hash = self._question_hash(call)
        if not question_hash:
            return ReadHitsUpdate(ReadHitsState(), "missing_question", len(candidates))
        family = semantic_action.family.value if semantic_action is not None else "SEARCH"
        next_state = ReadHitsState(
            question_hash, candidates, call.name, family,
        )
        return ReadHitsUpdate(next_state, "candidates_detected", len(candidates))

    def _is_search(
        self, call: ToolCall, semantic_action: SemanticAction | None,
    ) -> bool:
        return (
            call.name in self._SEARCH_TOOLS
            or semantic_action is not None
            and semantic_action.family in self._SEARCH_FAMILIES
        )

    def _consumes_candidate(self, call: ToolCall, state: ReadHitsState) -> bool:
        if call.name not in self._CONSUMER_TOOLS:
            return False
        path = call.arguments.get("path")
        return (
            isinstance(path, str) and bool(path.strip())
            and self._path_hash(path) in state.candidate_path_hashes
        )

    def _candidate_hashes(self, result: ToolResult) -> tuple[str, ...]:
        data = result.data
        if not isinstance(data, Mapping):
            return ()
        paths: set[str] = set()
        for field in self._PATH_COLLECTIONS:
            values = data.get(field)
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                continue
            for item in values:
                if not isinstance(item, Mapping):
                    continue
                path = item.get("path") or item.get("file")
                if isinstance(path, str) and path.strip():
                    paths.add(self._path_hash(path))
        return tuple(sorted(paths))

    @classmethod
    def _question_hash(cls, call: ToolCall) -> str:
        if call.evidence_question is None:
            return ""
        return cls._hash(call.evidence_question.question_id.strip().casefold())

    @classmethod
    def _path_hash(cls, path: str) -> str:
        normalized = path.strip().replace("\\", "/")
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        normalized = normalized.rstrip("/") or "."
        return cls._hash(normalized.casefold())

    @staticmethod
    def _hash(value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("read-hits policy is not started")
