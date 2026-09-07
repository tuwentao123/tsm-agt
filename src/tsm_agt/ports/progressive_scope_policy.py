"""Provider-neutral contracts for progressive search-scope control."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .adapter import RuntimeAdapter
from .semantic_action import SemanticAction
from .tool import ToolCall, ToolResult


class ProgressiveScopeAction(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_EXPANSION_REASON = "REQUIRE_EXPANSION_REASON"


@dataclass(frozen=True, slots=True)
class ScopeProbe:
    scope_hash: str = ""
    ancestor_hashes: tuple[str, ...] = ()
    depth: int = 0


@dataclass(frozen=True, slots=True)
class ScopeTrack:
    semantic_signature: str
    scope_hash: str
    ancestor_hashes: tuple[str, ...]
    depth: int
    last_zero_results: bool = False

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ScopeTrack:
        raw = data.get("ancestor_hashes", ())
        if not isinstance(raw, (list, tuple)) or not all(
            isinstance(item, str) and item for item in raw
        ):
            raise ValueError("scope ancestor_hashes must be strings")
        return cls(
            str(data.get("semantic_signature", "")),
            str(data.get("scope_hash", "")), tuple(raw),
            int(data.get("depth", 0)), bool(data.get("last_zero_results", False)),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "semantic_signature": self.semantic_signature,
            "scope_hash": self.scope_hash,
            "ancestor_hashes": list(self.ancestor_hashes),
            "depth": self.depth,
            "last_zero_results": self.last_zero_results,
        }


@dataclass(frozen=True, slots=True)
class ProgressiveScopeState:
    tracks: tuple[ScopeTrack, ...] = ()
    focused_scope_hash: str = ""
    focused_ancestor_hashes: tuple[str, ...] = ()
    focused_depth: int = 0
    broad_hits_pending: bool = False

    @classmethod
    def from_data(cls, data: Mapping[str, Any] | None) -> ProgressiveScopeState:
        if not data:
            return cls()
        raw = data.get("tracks", ())
        if not isinstance(raw, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in raw
        ):
            raise ValueError("scope tracks must be an array of objects")
        raw_ancestors = data.get("focused_ancestor_hashes", ())
        if not isinstance(raw_ancestors, (list, tuple)) or not all(
            isinstance(item, str) and item for item in raw_ancestors
        ):
            raise ValueError("focused scope ancestor hashes must be strings")
        return cls(
            tuple(ScopeTrack.from_data(item) for item in raw),
            str(data.get("focused_scope_hash", "")),
            tuple(raw_ancestors), int(data.get("focused_depth", 0)),
            bool(data.get("broad_hits_pending", False)),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "tracks": [track.to_data() for track in self.tracks],
            "focused_scope_hash": self.focused_scope_hash,
            "focused_ancestor_hashes": list(self.focused_ancestor_hashes),
            "focused_depth": self.focused_depth,
            "broad_hits_pending": self.broad_hits_pending,
        }


@dataclass(frozen=True, slots=True)
class ProgressiveScopeDecision:
    action: ProgressiveScopeAction
    reason: str
    relation: str = "unknown"
    previous_depth: int = 0
    current_depth: int = 0
    expansion_reason_hash: str = ""


@dataclass(frozen=True, slots=True)
class ProgressiveScopeUpdate:
    state: ProgressiveScopeState
    transition: str
    depth: int = 0
    zero_results: bool = False


class ProgressiveScopePolicyPort(RuntimeAdapter, Protocol):
    async def before_call(
        self, call: ToolCall, semantic_action: SemanticAction | None,
        probe: ScopeProbe, state: ProgressiveScopeState,
    ) -> ProgressiveScopeDecision: ...

    async def after_result(
        self, call: ToolCall, result: ToolResult,
        semantic_action: SemanticAction | None, probe: ScopeProbe,
        state: ProgressiveScopeState,
    ) -> ProgressiveScopeUpdate: ...
