"""Task-scoped Artifact Graph: replayable from Runtime events, never a summary.

Design constraints (see 《任务证明闭环实施SPEC.md》 §3.1):

* **Pure** -- no IO, no ``await``, no mutation. The projection is immutable and
  every ``observe_*`` returns a new value, so it can be rebuilt from events.
* **Facts only.** A node enters the graph because an observation, parse,
  execution or mutation event produced it. A *candidate* may enter the graph
  (AG-5) but can never close a required obligation.
* **Per-kind version semantics.** ``stale`` depends on ``version``, so every
  kind defines what a version change means -- not just files.
* **No business dispatch.** Kind/relation come from the producing adapter's
  declaration, never from a directory name, framework word or model prose.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .configuration import canonical_hash


class ArtifactKind(StrEnum):
    FILE = "FILE"
    SYMBOL = "SYMBOL"
    CONFIGURATION = "CONFIGURATION"
    COMMAND = "COMMAND"
    PROCESS = "PROCESS"
    TEST = "TEST"
    EXTERNAL_RESOURCE = "EXTERNAL_RESOURCE"
    EFFECT = "EFFECT"


class ArtifactRelation(StrEnum):
    CONTAINS = "CONTAINS"
    DEFINES = "DEFINES"
    REFERENCES = "REFERENCES"
    INVOKES = "INVOKES"
    CONFIGURES = "CONFIGURES"
    PRODUCES = "PRODUCES"
    VERIFIES = "VERIFIES"


def content_version(content: bytes | str) -> str:
    """``FILE`` / ``CONFIGURATION`` content version."""

    payload = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(payload).hexdigest()


def configuration_version(
    content: bytes | str, *, scope: Mapping[str, Any] | None = None,
) -> str:
    """Content **and** effective scope, because either change can alter meaning."""

    return canonical_hash({
        "content": content_version(content),
        "scope": dict(scope or {}),
    })


def symbol_version(
    *, host_content: bytes | str, qualified_name: str, declaration_span: str,
) -> str:
    """Host file content plus declaration position."""

    return canonical_hash({
        "host": content_version(host_content),
        "qualified_name": qualified_name,
        "declaration_span": declaration_span,
    })


def command_version(argv: Sequence[str]) -> str:
    """Normalized argv signature. **Run results are not part of a command's
    version** -- they belong to Evidence."""

    if not argv:
        raise ValueError("command_version requires a non-empty argv")
    executable = str(argv[0]).replace("\\", "/").rsplit("/", 1)[-1]
    return canonical_hash({"executable": executable, "arguments": list(argv[1:])})


def process_version(*, process_id: str, started_at: datetime) -> str:
    return canonical_hash({
        "process_id": process_id,
        "started_at": started_at.isoformat(),
    })


def test_version(*, command_signature: str, result_hash: str) -> str:
    return canonical_hash({
        "command_signature": command_signature,
        "result_hash": result_hash,
    })


def effect_version(
    *,
    mutation_id: str | None = None,
    path: str = "",
    before_hash: str = "",
    after_hash: str = "",
) -> str:
    if mutation_id:
        return canonical_hash({"mutation_id": mutation_id})
    return canonical_hash({
        "path": path, "before": before_hash, "after": after_hash,
    })


def external_version(server_version: str | None) -> str:
    """``EXTERNAL_RESOURCE`` version, or ``"unknown"`` when unavailable.

    An ``unknown`` version can never cover a "current version" obligation
    (§3.1) -- otherwise a stale fetch would be treated as current fact.
    """

    cleaned = (server_version or "").strip()
    return cleaned or "unknown"


def edge_version(
    *, source_version: str, relation: ArtifactRelation, target_version: str,
) -> str:
    """An edge is stale when *either* endpoint changes."""

    return canonical_hash({
        "source": source_version,
        "relation": relation.value,
        "target": target_version,
    })


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    version: str
    locator: str

    def to_data(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "version": self.version,
            "locator": self.locator,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ArtifactRef:
        return cls(
            artifact_id=str(data["artifact_id"]),
            version=str(data["version"]),
            locator=str(data["locator"]),
        )


@dataclass(frozen=True, slots=True)
class ArtifactNode:
    artifact_id: str
    kind: ArtifactKind
    locator: str
    version: str
    attributes: Mapping[str, Any] = field(default_factory=dict)
    source_event_refs: tuple[str, ...] = ()

    def ref(self) -> ArtifactRef:
        return ArtifactRef(self.artifact_id, self.version, self.locator)

    def to_data(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind.value,
            "locator": self.locator,
            "version": self.version,
            "attributes": dict(self.attributes),
            "source_event_refs": list(self.source_event_refs),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ArtifactNode:
        return cls(
            artifact_id=str(data["artifact_id"]),
            kind=ArtifactKind(data["kind"]),
            locator=str(data["locator"]),
            version=str(data["version"]),
            attributes=dict(data.get("attributes") or {}),
            source_event_refs=tuple(data.get("source_event_refs") or ()),
        )


@dataclass(frozen=True, slots=True)
class ArtifactEdge:
    edge_id: str
    source_artifact_id: str
    relation: ArtifactRelation
    target_artifact_id: str
    version: str
    source_event_refs: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_artifact_id": self.source_artifact_id,
            "relation": self.relation.value,
            "target_artifact_id": self.target_artifact_id,
            "version": self.version,
            "source_event_refs": list(self.source_event_refs),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ArtifactEdge:
        return cls(
            edge_id=str(data["edge_id"]),
            source_artifact_id=str(data["source_artifact_id"]),
            relation=ArtifactRelation(data["relation"]),
            target_artifact_id=str(data["target_artifact_id"]),
            version=str(data["version"]),
            source_event_refs=tuple(data.get("source_event_refs") or ()),
        )


def derive_edge_id(
    *, source_artifact_id: str, relation: ArtifactRelation, target_artifact_id: str,
) -> str:
    """Edge identity excludes versions, so an edge keeps its id across edits."""

    digest = canonical_hash({
        "source": source_artifact_id,
        "relation": relation.value,
        "target": target_artifact_id,
    })
    return f"edg-{digest[:16]}"


@dataclass(frozen=True, slots=True)
class ArtifactGraphProjection:
    """Immutable, replayable graph for one Task."""

    nodes: tuple[ArtifactNode, ...] = ()
    edges: tuple[ArtifactEdge, ...] = ()

    def node(self, artifact_id: str) -> ArtifactNode | None:
        for candidate in self.nodes:
            if candidate.artifact_id == artifact_id:
                return candidate
        return None

    def edge(self, edge_id: str) -> ArtifactEdge | None:
        for candidate in self.edges:
            if candidate.edge_id == edge_id:
                return candidate
        return None

    def version_of(self, artifact_id: str) -> str | None:
        node = self.node(artifact_id)
        return None if node is None else node.version

    def is_current(self, reference: ArtifactRef) -> bool:
        """True when the reference still points at the observed version."""

        current = self.version_of(reference.artifact_id)
        return current is not None and current == reference.version

    def observe_node(self, node: ArtifactNode) -> ArtifactGraphProjection:
        """Add or replace a node. Re-observing the same version is idempotent."""

        retained = tuple(
            candidate for candidate in self.nodes
            if candidate.artifact_id != node.artifact_id
        )
        return ArtifactGraphProjection(
            tuple(sorted(retained + (node,), key=lambda item: item.artifact_id)),
            self.edges,
        )

    def observe_edge(self, edge: ArtifactEdge) -> ArtifactGraphProjection:
        retained = tuple(
            candidate for candidate in self.edges
            if candidate.edge_id != edge.edge_id
        )
        return ArtifactGraphProjection(
            self.nodes,
            tuple(sorted(retained + (edge,), key=lambda item: item.edge_id)),
        )

    def make_edge(
        self,
        *,
        source_artifact_id: str,
        relation: ArtifactRelation,
        target_artifact_id: str,
        source_event_refs: Sequence[str] = (),
    ) -> ArtifactEdge | None:
        """Build an edge only when both endpoints are already observed.

        Relations may only come from a parse, execution or mutation event --
        never from model prose (AG-4 / §3.1).
        """

        source = self.node(source_artifact_id)
        target = self.node(target_artifact_id)
        if source is None or target is None:
            return None
        return ArtifactEdge(
            edge_id=derive_edge_id(
                source_artifact_id=source_artifact_id, relation=relation,
                target_artifact_id=target_artifact_id,
            ),
            source_artifact_id=source_artifact_id,
            relation=relation,
            target_artifact_id=target_artifact_id,
            version=edge_version(
                source_version=source.version, relation=relation,
                target_version=target.version,
            ),
            source_event_refs=tuple(source_event_refs),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_data() for node in self.nodes],
            "edges": [edge.to_data() for edge in self.edges],
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ArtifactGraphProjection:
        return cls(
            nodes=tuple(
                ArtifactNode.from_data(item) for item in data.get("nodes") or ()
            ),
            edges=tuple(
                ArtifactEdge.from_data(item) for item in data.get("edges") or ()
            ),
        )
