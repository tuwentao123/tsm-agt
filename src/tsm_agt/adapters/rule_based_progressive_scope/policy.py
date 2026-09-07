"""Conservative project-neutral progressive search-scope policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
    ProgressiveScopeAction, ProgressiveScopeDecision,
    ProgressiveScopePolicyPort, ProgressiveScopeState, ProgressiveScopeUpdate,
    ScopeProbe, ScopeTrack, SemanticAction, SemanticActionFamily, ToolCall, ToolResult,
)


class RuleBasedProgressiveScopePolicy:
    descriptor = AdapterDescriptor(
        "builtin.rule-based-progressive-scope", "1.0.0",
        "ProgressiveScopePolicyPort", "1.0",
        frozenset({"project-neutral", "checkpointed", "redacted-events"}),
    )
    _MAX_TRACKS = 500
    _SEARCH_FAMILIES = frozenset({
        SemanticActionFamily.SEARCH_CONCEPT, SemanticActionFamily.SEARCH_DEFINITION,
        SemanticActionFamily.SEARCH_REFERENCES,
    })
    _RESULT_FIELDS = (
        "matches", "definitions", "references", "implementations", "symbols",
    )
    _SOURCE_SUFFIXES = frozenset({
        ".c", ".cc", ".cpp", ".cs", ".dart", ".go", ".h", ".hpp",
        ".java", ".js", ".jsx", ".kt", ".kts", ".m", ".mm",
        ".php", ".py", ".rb", ".rs", ".scala", ".sh", ".swift",
        ".ts", ".tsx", ".vue",
    })

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "progressive scope policy ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def before_call(
        self, call: ToolCall, semantic_action: SemanticAction | None,
        probe: ScopeProbe, state: ProgressiveScopeState,
    ) -> ProgressiveScopeDecision:
        self._require_started()
        if not self._is_search(semantic_action) or not probe.scope_hash:
            return ProgressiveScopeDecision(ProgressiveScopeAction.ALLOW, "not_scoped_search")
        reason = (
            call.evidence_question.scope_expansion_reason.strip()
            if call.evidence_question is not None else ""
        )
        reason_hash = self._hash(reason) if reason else ""
        if state.focused_scope_hash and not self._inside_focus(state, probe):
            if reason:
                return ProgressiveScopeDecision(
                    ProgressiveScopeAction.ALLOW, "expanded_with_reason",
                    "expanded", state.focused_depth, probe.depth, reason_hash,
                )
            return ProgressiveScopeDecision(
                ProgressiveScopeAction.REQUIRE_EXPANSION_REASON,
                "expanded_without_reason", "expanded",
                state.focused_depth, probe.depth,
            )
        if state.broad_hits_pending and probe.depth == 0:
            if reason:
                return ProgressiveScopeDecision(
                    ProgressiveScopeAction.ALLOW, "repeated_broad_search_with_reason",
                    "same", 0, 0, reason_hash,
                )
            return ProgressiveScopeDecision(
                ProgressiveScopeAction.REQUIRE_EXPANSION_REASON,
                "broad_hits_require_narrowing", "same", 0, 0,
            )
        previous = self._track(state, semantic_action.semantic_signature)
        if previous is None:
            return ProgressiveScopeDecision(
                ProgressiveScopeAction.ALLOW, "first_scope", "first",
                current_depth=probe.depth,
            )
        relation = self._relation(previous, probe)
        if relation != "expanded":
            return ProgressiveScopeDecision(
                ProgressiveScopeAction.ALLOW, relation, relation, previous.depth,
                probe.depth, reason_hash,
            )
        if previous.last_zero_results:
            return ProgressiveScopeDecision(
                ProgressiveScopeAction.ALLOW, "expanded_after_zero_results", relation,
                previous.depth, probe.depth, reason_hash,
            )
        if reason:
            return ProgressiveScopeDecision(
                ProgressiveScopeAction.ALLOW, "expanded_with_reason", relation,
                previous.depth, probe.depth, reason_hash,
            )
        return ProgressiveScopeDecision(
            ProgressiveScopeAction.REQUIRE_EXPANSION_REASON,
            "expanded_without_reason", relation, previous.depth, probe.depth,
        )

    async def after_result(
        self, call: ToolCall, result: ToolResult,
        semantic_action: SemanticAction | None, probe: ScopeProbe,
        state: ProgressiveScopeState,
    ) -> ProgressiveScopeUpdate:
        self._require_started()
        if not result.ok or not probe.scope_hash:
            return ProgressiveScopeUpdate(state, "unchanged")
        if self._is_source_consumer(call, semantic_action):
            focused_probe = self._directory_probe(probe)
            return ProgressiveScopeUpdate(
                ProgressiveScopeState(
                    state.tracks, focused_probe.scope_hash,
                    focused_probe.ancestor_hashes, focused_probe.depth, False,
                ),
                "candidate_scope_focused", focused_probe.depth, False,
            )
        if not self._is_search(semantic_action):
            return ProgressiveScopeUpdate(state, "unchanged")
        zero = self._zero_results(result.data)
        signature = semantic_action.semantic_signature
        track = ScopeTrack(
            signature, probe.scope_hash, probe.ancestor_hashes, probe.depth, zero
        )
        tracks = [item for item in state.tracks if item.semantic_signature != signature]
        tracks.append(track)
        tracks = tracks[-self._MAX_TRACKS:]
        focused_hash = state.focused_scope_hash
        focused_ancestors = state.focused_ancestor_hashes
        focused_depth = state.focused_depth
        return ProgressiveScopeUpdate(
            ProgressiveScopeState(
                tuple(tracks), focused_hash, focused_ancestors, focused_depth,
                not zero and probe.depth == 0,
            ), "scope_recorded", probe.depth, zero
        )

    def _is_search(self, action: SemanticAction | None) -> bool:
        return action is not None and action.family in self._SEARCH_FAMILIES

    @staticmethod
    def _is_source_consumer(
        call: ToolCall, action: SemanticAction | None,
    ) -> bool:
        if call.name == "code.symbol_overview":
            return True
        if call.name != "core.read_file":
            return False
        path = call.arguments.get("path")
        if not isinstance(path, str):
            return False
        from pathlib import PurePosixPath
        return PurePosixPath(path.replace("\\", "/")).suffix.casefold() in (
            RuleBasedProgressiveScopePolicy._SOURCE_SUFFIXES
        )

    @staticmethod
    def _directory_probe(probe: ScopeProbe) -> ScopeProbe:
        if probe.depth <= 0 or not probe.ancestor_hashes:
            return probe
        return ScopeProbe(
            probe.ancestor_hashes[-1], probe.ancestor_hashes[:-1], probe.depth - 1
        )

    @staticmethod
    def _inside_focus(state: ProgressiveScopeState, probe: ScopeProbe) -> bool:
        return (
            probe.scope_hash == state.focused_scope_hash
            or state.focused_scope_hash in probe.ancestor_hashes
        )

    @staticmethod
    def _track(state: ProgressiveScopeState, signature: str) -> ScopeTrack | None:
        return next((
            item for item in reversed(state.tracks)
            if item.semantic_signature == signature
        ), None)

    @staticmethod
    def _relation(previous: ScopeTrack, probe: ScopeProbe) -> str:
        if probe.scope_hash == previous.scope_hash:
            return "same"
        if previous.scope_hash in probe.ancestor_hashes:
            return "narrowed"
        if probe.scope_hash in previous.ancestor_hashes:
            return "expanded"
        return "lateral"

    @classmethod
    def _zero_results(cls, data: Any) -> bool:
        if not isinstance(data, Mapping):
            return False
        present = [data[field] for field in cls._RESULT_FIELDS if field in data]
        return bool(present) and all(
            isinstance(value, Sequence) and not isinstance(value, (str, bytes))
            and len(value) == 0 for value in present
        )

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("progressive scope policy is not started")
