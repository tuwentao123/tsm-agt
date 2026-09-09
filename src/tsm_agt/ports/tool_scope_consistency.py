"""Provider-neutral contract for checking declared and actual tool scope."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .adapter import RuntimeAdapter
from .tool import ToolCall, ToolResult


class ToolScopeConsistencyAction(StrEnum):
    """Standard actions; this policy never grants filesystem authority."""

    ALLOW = "ALLOW"
    REPLAN = "REPLAN"


class ToolScopeRelation(StrEnum):
    """Platform-normalized relationship between intended and actual scope."""

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNDECLARED = "UNDECLARED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class ToolScopeConsistencyProbe:
    """Location facts prepared by Kernel with the active platform adapter.

    `expected_scope` is model-declared intent. `resolved_*` values are facts from
    the completed tool. `relation` is computed with platform-aware canonical path
    handling, so a policy need not contain POSIX or Windows path rules.
    """

    relation: ToolScopeRelation
    expected_scope: str = ""
    requested_path: str = ""
    resolved_path: str = ""
    resolved_root: str = ""
    root_kind: str = ""


@dataclass(frozen=True, slots=True)
class ToolScopeConsistencyDecision:
    action: ToolScopeConsistencyAction
    reason: str
    probe: ToolScopeConsistencyProbe


class ToolScopeConsistencyPolicyPort(RuntimeAdapter, Protocol):
    """Decide whether a real Tool Result answers its declared scope.

    The policy runs after tool execution and before the result becomes evidence.
    It cannot rewrite a path, execute another tool, or bypass Workspace/Sandbox/
    Approval. A REPLAN result is feedback to the model, not authority.
    """

    async def evaluate(
        self, call: ToolCall, result: ToolResult, probe: ToolScopeConsistencyProbe,
    ) -> ToolScopeConsistencyDecision: ...
