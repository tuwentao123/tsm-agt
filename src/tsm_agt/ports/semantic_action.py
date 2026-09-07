"""Provider-neutral semantic action classification contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .adapter import RuntimeAdapter
from .tool import ToolCall


class SemanticActionFamily(StrEnum):
    INSPECT_PROJECT_STRUCTURE = "INSPECT_PROJECT_STRUCTURE"
    SEARCH_CONCEPT = "SEARCH_CONCEPT"
    SEARCH_DEFINITION = "SEARCH_DEFINITION"
    SEARCH_REFERENCES = "SEARCH_REFERENCES"
    READ_ARTIFACT = "READ_ARTIFACT"
    INSPECT_CONFIGURATION = "INSPECT_CONFIGURATION"
    MUTATE_WORKSPACE = "MUTATE_WORKSPACE"
    EXECUTE_VERIFICATION = "EXECUTE_VERIFICATION"
    EXECUTE_COMMAND = "EXECUTE_COMMAND"
    CONTROL_PROCESS = "CONTROL_PROCESS"
    REQUEST_CLARIFICATION = "REQUEST_CLARIFICATION"
    MANAGE_AGENT_STATE = "MANAGE_AGENT_STATE"
    USE_TOOL = "USE_TOOL"


class SemanticScopeKind(StrEnum):
    WORKSPACE = "workspace"
    DIRECTORY = "directory"
    FILE = "file"
    SYMBOL = "symbol"
    EXTERNAL = "external"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SemanticAction:
    family: SemanticActionFamily
    target_kind: str
    target_hash: str
    scope_kind: SemanticScopeKind
    scope_hash: str
    semantic_signature: str
    classifier_id: str
    classifier_version: str
    confidence: float

    def __post_init__(self) -> None:
        if not self.target_kind.strip() or not self.target_hash.strip():
            raise ValueError("semantic action target fields must not be empty")
        if not self.scope_hash.strip() or not self.semantic_signature.strip():
            raise ValueError("semantic action hashes must not be empty")
        if not self.classifier_id.strip() or not self.classifier_version.strip():
            raise ValueError("semantic classifier identity must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("semantic action confidence must be within 0..1")

    def event_data(self) -> dict[str, str | float]:
        return {
            "family": self.family.value,
            "target_kind": self.target_kind,
            "target_hash": self.target_hash,
            "scope_kind": self.scope_kind.value,
            "scope_hash": self.scope_hash,
            "semantic_signature": self.semantic_signature,
            "classifier_id": self.classifier_id,
            "classifier_version": self.classifier_version,
            "confidence": self.confidence,
        }


class SemanticActionClassifierPort(RuntimeAdapter, Protocol):
    async def classify(self, call: ToolCall) -> SemanticAction: ...
