"""Deterministic, side-effect-free projection of Runtime Events into a flow graph."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, TypeAlias

from tsm_agt.ports import InvestigationFlowProjectorPort, RuntimeEvent


class FlowProjectionError(ValueError):
    pass


class FlowNodeKind(StrEnum):
    TASK = "task"
    PHASE = "phase"
    TURN = "turn"
    MODEL = "model"
    TOOL = "tool"
    APPROVAL = "approval"
    CLARIFICATION = "clarification"
    VERIFICATION = "verification"
    PROCESS = "process"
    MUTATION = "mutation"
    MEMORY = "memory"
    ONBOARDING = "onboarding"
    CHECKPOINT = "checkpoint"
    STEERING = "steering"
    PLAN = "plan"
    EVIDENCE_QUESTION = "evidence_question"


class FlowNodeStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_USER = "WAITING_USER"
    RETRYING = "RETRYING"
    SKIPPED = "SKIPPED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            self.SUCCEEDED, self.FAILED, self.CANCELLED,
            self.UNKNOWN_OUTCOME, self.SKIPPED,
        }


class FlowEdgeRelation(StrEnum):
    CONTAINS = "contains"
    NEXT = "next"
    DEPENDS_ON = "depends_on"
    CAUSED_BY = "caused_by"
    RETRY_OF = "retry_of"


class FlowLane(StrEnum):
    """Stable observable lanes; these do not represent hidden reasoning."""

    TASK = "task"
    MODEL = "model"
    TOOL = "tool"
    PROCESS = "process"
    USER = "user"
    CONTROL = "control"
    WORKSPACE = "workspace"


class FlowDiagnosticCategory(StrEnum):
    INPUT = "input"
    RESULT = "result"
    CONTROL = "control"
    PROCESS = "process"
    MUTATION = "mutation"
    FAILURE = "failure"


FlowDiagnosticValue: TypeAlias = str | int | bool | None


@dataclass(frozen=True, slots=True)
class FlowDiagnosticFact:
    """One explicitly allow-listed fact; never an Event payload copy."""

    category: FlowDiagnosticCategory
    event_sequence: int
    code: str
    values: tuple[tuple[str, FlowDiagnosticValue], ...] = ()
    node_id: str | None = None
    invocation_id: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "event_sequence": self.event_sequence,
            "code": self.code,
            "values": dict(self.values),
        }


@dataclass(frozen=True, slots=True)
class FlowFailureAttribution:
    code: str | None
    category: str
    confidence: str
    basis: str
    event_sequence: int | None

    def to_data(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "confidence": self.confidence,
            "basis": self.basis,
            "event_sequence": self.event_sequence,
        }


@dataclass(frozen=True, slots=True)
class FlowNode:
    node_id: str
    task_id: str
    parent_id: str | None
    kind: FlowNodeKind
    label: str
    status: FlowNodeStatus
    started_at: datetime | None
    finished_at: datetime | None
    wait_reason: str | None
    retry_of: str | None
    event_seq_start: int
    event_seq_end: int | None
    summary_ref: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id, "task_id": self.task_id,
            "parent_id": self.parent_id, "kind": self.kind.value,
            "label": self.label, "status": self.status.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at else None
            ),
            "wait_reason": self.wait_reason, "retry_of": self.retry_of,
            "event_seq_start": self.event_seq_start,
            "event_seq_end": self.event_seq_end,
            "summary_ref": self.summary_ref,
        }


@dataclass(frozen=True, slots=True)
class FlowTimelineItem:
    node_id: str
    lane: FlowLane
    kind: FlowNodeKind
    label: str
    status: FlowNodeStatus
    event_seq_start: int
    event_seq_end: int | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None

    def to_data(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "lane": self.lane.value,
            "kind": self.kind.value,
            "label": self.label,
            "status": self.status.value,
            "event_seq_start": self.event_seq_start,
            "event_seq_end": self.event_seq_end,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at else None
            ),
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class FlowTimeline:
    task_id: str
    cursor: int
    lanes: tuple[FlowLane, ...]
    items: tuple[FlowTimelineItem, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "cursor": self.cursor,
            "lanes": [lane.value for lane in self.lanes],
            "items": [item.to_data() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class FlowEdge:
    from_node: str
    to_node: str
    relation: FlowEdgeRelation

    def to_data(self) -> dict[str, str]:
        return {
            "from_node": self.from_node, "to_node": self.to_node,
            "relation": self.relation.value,
        }


@dataclass(frozen=True, slots=True)
class FlowNodeDiagnostic:
    """Payload-free, deterministic drill-down for one projected node."""

    node: FlowNode
    duration_ms: int | None
    parent: FlowNode | None
    children: tuple[FlowNode, ...]
    incoming_edges: tuple[FlowEdge, ...]
    outgoing_edges: tuple[FlowEdge, ...]
    event_anchors: tuple[int, ...]
    is_first_actionable_failure: bool
    facts: tuple[FlowDiagnosticFact, ...]
    downstream_nodes: tuple[FlowNode, ...]
    failure_attribution: FlowFailureAttribution | None

    def to_data(self) -> dict[str, Any]:
        return {
            "node": self.node.to_data(),
            "duration_ms": self.duration_ms,
            "parent": self.parent.to_data() if self.parent else None,
            "children": [node.to_data() for node in self.children],
            "incoming_edges": [edge.to_data() for edge in self.incoming_edges],
            "outgoing_edges": [edge.to_data() for edge in self.outgoing_edges],
            "event_anchors": list(self.event_anchors),
            "is_first_actionable_failure": self.is_first_actionable_failure,
            "facts": [fact.to_data() for fact in self.facts],
            "downstream_nodes": [node.to_data() for node in self.downstream_nodes],
            "failure_attribution": (
                self.failure_attribution.to_data()
                if self.failure_attribution else None
            ),
        }


@dataclass(frozen=True, slots=True)
class FlowProjectionView:
    """A filtered read-only view that keeps matched nodes' ancestor paths."""

    task_id: str
    cursor: int
    nodes: tuple[FlowNode, ...]
    edges: tuple[FlowEdge, ...]
    matched_node_ids: tuple[str, ...]
    kinds: tuple[FlowNodeKind, ...]
    statuses: tuple[FlowNodeStatus, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "cursor": self.cursor,
            "nodes": [node.to_data() for node in self.nodes],
            "edges": [edge.to_data() for edge in self.edges],
            "matched_node_ids": list(self.matched_node_ids),
            "filters": {
                "kinds": [kind.value for kind in self.kinds],
                "statuses": [status.value for status in self.statuses],
            },
        }


@dataclass(frozen=True, slots=True)
class FlowProjection:
    task_id: str
    cursor: int
    nodes: tuple[FlowNode, ...]
    edges: tuple[FlowEdge, ...]
    applied_event_ids: tuple[str, ...] = ()
    active_phase_id: str | None = None
    last_child_by_parent: tuple[tuple[str, str], ...] = ()
    tool_node_by_invocation: tuple[tuple[str, str], ...] = ()
    process_invocation_by_id: tuple[tuple[str, str], ...] = ()
    diagnostic_facts: tuple[FlowDiagnosticFact, ...] = ()

    @property
    def first_actionable_failure(self) -> FlowNode | None:
        actionable = {
            FlowNodeKind.MODEL, FlowNodeKind.TOOL, FlowNodeKind.APPROVAL,
            FlowNodeKind.PROCESS, FlowNodeKind.MUTATION,
        }
        direct = next((
            node for node in self.nodes
            if node.kind in actionable and node.status in {
                FlowNodeStatus.FAILED, FlowNodeStatus.UNKNOWN_OUTCOME
            }
        ), None)
        if direct is not None:
            return direct
        # A Turn is actionable only when the limit/controller itself failed.
        # Do not let a derived Turn failure outrank a concrete child failure.
        limit_turn_ids = {
            fact.node_id for fact in self.diagnostic_facts
            if fact.code == "agent.limit-exceeded" and fact.node_id is not None
        }
        return next((
            node for node in self.nodes
            if node.node_id in limit_turn_ids
            and node.status is FlowNodeStatus.FAILED
        ), None)

    def inspect_node(self, node_id: str) -> FlowNodeDiagnostic:
        """Return one node's observable relationships without Event payloads."""

        normalized = node_id.strip()
        if not normalized:
            raise ValueError("flow node_id must not be empty")
        by_id = {node.node_id: node for node in self.nodes}
        try:
            node = by_id[normalized]
        except KeyError as error:
            raise LookupError(f"flow node not found: {normalized}") from error
        parent = by_id.get(node.parent_id) if node.parent_id is not None else None
        children = tuple(
            candidate for candidate in self.nodes
            if candidate.parent_id == node.node_id
        )
        incoming = tuple(
            edge for edge in self.edges if edge.to_node == node.node_id
        )
        outgoing = tuple(
            edge for edge in self.edges if edge.from_node == node.node_id
        )
        duration_ms = None
        if node.started_at is not None and node.finished_at is not None:
            duration_ms = max(
                0, round((node.finished_at - node.started_at).total_seconds() * 1000)
            )
        anchors = (node.event_seq_start,)
        if (
            node.event_seq_end is not None
            and node.event_seq_end != node.event_seq_start
        ):
            anchors += (node.event_seq_end,)
        first_failure = self.first_actionable_failure
        invocation_ids = {
            invocation_id for invocation_id, mapped_node_id
            in self.tool_node_by_invocation if mapped_node_id == node.node_id
        }
        facts = tuple(
            fact for fact in self.diagnostic_facts
            if fact.node_id == node.node_id
            or (
                fact.invocation_id is not None
                and fact.invocation_id in invocation_ids
            )
        )
        downstream_ids: set[str] = set()
        frontier = [node.node_id]
        while frontier:
            current = frontier.pop()
            for edge in self.edges:
                candidate_id = None
                if (
                    edge.relation is FlowEdgeRelation.CAUSED_BY
                    and edge.from_node == current
                ):
                    candidate_id = edge.to_node
                elif (
                    edge.relation is FlowEdgeRelation.DEPENDS_ON
                    and edge.to_node == current
                ):
                    candidate_id = edge.from_node
                if candidate_id is None or candidate_id in downstream_ids:
                    continue
                if candidate_id == node.node_id:
                    continue
                downstream_ids.add(candidate_id)
                frontier.append(candidate_id)
        downstream = tuple(
            candidate for candidate in self.nodes
            if candidate.node_id in downstream_ids
        )
        attribution = self._failure_attribution(node, facts)
        return FlowNodeDiagnostic(
            node=node, duration_ms=duration_ms, parent=parent, children=children,
            incoming_edges=incoming, outgoing_edges=outgoing,
            event_anchors=anchors,
            is_first_actionable_failure=(
                first_failure is not None and first_failure.node_id == node.node_id
            ),
            facts=facts, downstream_nodes=downstream,
            failure_attribution=attribution,
        )

    @staticmethod
    def _failure_attribution(
        node: FlowNode, facts: tuple[FlowDiagnosticFact, ...],
    ) -> FlowFailureAttribution | None:
        if node.status not in {
            FlowNodeStatus.FAILED, FlowNodeStatus.UNKNOWN_OUTCOME,
        }:
            return None
        failure = next(
            (fact for fact in reversed(facts)
             if fact.category is FlowDiagnosticCategory.FAILURE),
            None,
        )
        if failure is None:
            return FlowFailureAttribution(
                None, "unclassified", "unclassified",
                "no allow-listed failure signal maps deterministically to F1-F14",
                node.event_seq_end or node.event_seq_start,
            )
        values = dict(failure.values)
        error_code = str(values.get("error_code") or "")
        error_type = str(values.get("error_type") or "")
        event_type = str(values.get("event_type") or "")
        if error_type == "ContextWindowExceeded":
            result = (
                "F7",
                "context budget or compaction could not preserve required input",
            )
        elif error_code == "NOT_FOUND":
            result = ("F3", "extension or tool selection error")
        elif error_code == "INVALID_PARAM":
            result = ("F4", "invalid tool parameters without correction")
        elif error_code == "PERMISSION_DENIED":
            result = ("F9", "policy or approval prevented execution")
        elif error_code in {"TIMEOUT", "TOOL_FAILED"}:
            result = ("F5", "tool or environment failure")
        elif error_code == "UNKNOWN_OUTCOME":
            result = ("F13", "side-effect outcome could not be proven")
        elif event_type == "llm.failed" or error_type in {
            "InvalidModelResponse", "ModelInvocationFailed",
            "ProviderCapabilityMismatch",
        }:
            result = ("F11", "provider or normalized message protocol failure")
        elif error_type == "WorkspaceMutationConflict":
            result = ("F13", "workspace transaction conflict")
        elif event_type in {
            "process.failed", "process.denied", "process.orphaned",
            "process.nonzero_exit",
        }:
            result = ("F13", "process lifecycle or side-effect failure")
        elif event_type == "agent.limit_exceeded":
            result = ("F8", "agent loop or progress limit exceeded")
        else:
            return FlowFailureAttribution(
                None, "unclassified", "unclassified",
                "failure signal exists but no deterministic F1-F14 rule matches",
                failure.event_sequence,
            )
        return FlowFailureAttribution(
            result[0], result[1], "deterministic", failure.code,
            failure.event_sequence,
        )

    def timeline(
        self, node_ids: frozenset[str] | None = None,
    ) -> FlowTimeline:
        """Project nodes into deterministic swimlanes without reading payloads."""

        lane_by_kind = {
            FlowNodeKind.TASK: FlowLane.TASK,
            FlowNodeKind.TURN: FlowLane.TASK,
            FlowNodeKind.MODEL: FlowLane.MODEL,
            FlowNodeKind.TOOL: FlowLane.TOOL,
            FlowNodeKind.PROCESS: FlowLane.PROCESS,
            FlowNodeKind.APPROVAL: FlowLane.USER,
            FlowNodeKind.CLARIFICATION: FlowLane.USER,
            FlowNodeKind.STEERING: FlowLane.USER,
            FlowNodeKind.PLAN: FlowLane.CONTROL,
            FlowNodeKind.EVIDENCE_QUESTION: FlowLane.CONTROL,
            FlowNodeKind.VERIFICATION: FlowLane.CONTROL,
            FlowNodeKind.PHASE: FlowLane.CONTROL,
            FlowNodeKind.CHECKPOINT: FlowLane.CONTROL,
            FlowNodeKind.MUTATION: FlowLane.WORKSPACE,
            FlowNodeKind.MEMORY: FlowLane.CONTROL,
            FlowNodeKind.ONBOARDING: FlowLane.CONTROL,
        }
        items = []
        for node in self.nodes:
            if node_ids is not None and node.node_id not in node_ids:
                continue
            duration_ms = None
            if node.started_at is not None and node.finished_at is not None:
                duration_ms = max(
                    0,
                    round(
                        (node.finished_at - node.started_at).total_seconds() * 1000
                    ),
                )
            items.append(FlowTimelineItem(
                node_id=node.node_id, lane=lane_by_kind[node.kind], kind=node.kind,
                label=node.label, status=node.status,
                event_seq_start=node.event_seq_start,
                event_seq_end=node.event_seq_end,
                started_at=node.started_at, finished_at=node.finished_at,
                duration_ms=duration_ms,
            ))
        ordered = tuple(sorted(
            items,
            key=lambda item: (
                item.event_seq_start,
                item.event_seq_end if item.event_seq_end is not None else 2**63,
                item.node_id,
            ),
        ))
        present = {item.lane for item in ordered}
        lanes = tuple(lane for lane in FlowLane if lane in present)
        return FlowTimeline(self.task_id, self.cursor, lanes, ordered)

    def filter_nodes(
        self,
        *,
        kinds: tuple[FlowNodeKind, ...] = (),
        statuses: tuple[FlowNodeStatus, ...] = (),
    ) -> FlowProjectionView:
        """Select nodes and ancestors without inventing or rewriting edges."""

        if not kinds and not statuses:
            raise ValueError("flow filter requires at least one kind or status")
        if any(not isinstance(kind, FlowNodeKind) for kind in kinds):
            raise TypeError("flow filter kinds must be FlowNodeKind values")
        if any(not isinstance(status, FlowNodeStatus) for status in statuses):
            raise TypeError("flow filter statuses must be FlowNodeStatus values")
        selected_kinds = tuple(dict.fromkeys(kinds))
        selected_statuses = tuple(dict.fromkeys(statuses))
        kind_set = set(selected_kinds)
        status_set = set(selected_statuses)
        matches = tuple(
            node for node in self.nodes
            if (not kind_set or node.kind in kind_set)
            and (not status_set or node.status in status_set)
        )
        by_id = {node.node_id: node for node in self.nodes}
        visible_ids = {node.node_id for node in matches}
        for match in matches:
            parent_id = match.parent_id
            visited: set[str] = set()
            while parent_id is not None:
                if parent_id in visited:
                    raise FlowProjectionError("flow parent hierarchy contains a cycle")
                visited.add(parent_id)
                parent = by_id.get(parent_id)
                if parent is None:
                    raise FlowProjectionError(
                        f"flow node has missing parent: {parent_id}"
                    )
                visible_ids.add(parent.node_id)
                parent_id = parent.parent_id
        nodes = tuple(node for node in self.nodes if node.node_id in visible_ids)
        edges = tuple(
            edge for edge in self.edges
            if edge.from_node in visible_ids and edge.to_node in visible_ids
        )
        return FlowProjectionView(
            task_id=self.task_id, cursor=self.cursor, nodes=nodes, edges=edges,
            matched_node_ids=tuple(node.node_id for node in matches),
            kinds=selected_kinds, statuses=selected_statuses,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "cursor": self.cursor,
            "nodes": [node.to_data() for node in self.nodes],
            "edges": [edge.to_data() for edge in self.edges],
            "diagnostic_facts": [
                fact.to_data() for fact in self.diagnostic_facts
            ],
            "first_actionable_failure": (
                self.first_actionable_failure.node_id
                if self.first_actionable_failure else None
            ),
        }


def build_flow_export_document(
    projection: FlowProjection, view: FlowProjectionView | None = None,
) -> dict[str, Any]:
    """Build the deterministic, payload-free v1 Flow export envelope."""

    if view is not None and (
        view.task_id != projection.task_id or view.cursor != projection.cursor
    ):
        raise ValueError("Flow export view does not belong to the projection")
    timeline = projection.timeline(
        frozenset(node.node_id for node in view.nodes)
        if view is not None else None
    )
    visible_node_ids = (
        {node.node_id for node in view.nodes}
        if view is not None else {node.node_id for node in projection.nodes}
    )
    visible_invocation_ids = {
        invocation_id for invocation_id, node_id
        in projection.tool_node_by_invocation if node_id in visible_node_ids
    }
    return {
        "artifact_type": "tsm-agt.flow",
        "schema_version": 1,
        "task_id": projection.task_id,
        "cursor": projection.cursor,
        "redaction": {
            "policy": "projection-only-v1",
            "excluded": [
                "event_payloads", "prompts", "tool_arguments",
                "environment_variables", "log_bodies", "hidden_reasoning",
            ],
        },
        "flow": view.to_data() if view is not None else projection.to_data(),
        "timeline": timeline.to_data(),
        "diagnostic_facts": [
            fact.to_data() for fact in projection.diagnostic_facts
            if fact.node_id in visible_node_ids
            or (
                fact.invocation_id is not None
                and fact.invocation_id in visible_invocation_ids
            )
        ],
    }


class FlowProjector:
    """Apply ordered v1 events; never executes work or infers hidden reasoning."""

    def __init__(
        self, investigation_projector: InvestigationFlowProjectorPort | None = None,
    ) -> None:
        self._investigation_projector = investigation_projector

    def project(self, events: tuple[RuntimeEvent, ...]) -> FlowProjection:
        if not events:
            raise FlowProjectionError("cannot project an empty event stream")
        return self.apply(None, events)

    def apply(
        self, projection: FlowProjection | None,
        events: tuple[RuntimeEvent, ...],
    ) -> FlowProjection:
        if projection is None:
            if not events:
                raise FlowProjectionError("cannot start projection without events")
            task_id = events[0].task_id
            cursor = 0
            nodes: dict[str, FlowNode] = {}
            edges: list[FlowEdge] = []
            applied: set[str] = set()
            active_phase_id = None
            last_child: dict[str, str] = {}
            diagnostic_facts: list[FlowDiagnosticFact] = []
        else:
            task_id = projection.task_id
            cursor = projection.cursor
            nodes = {node.node_id: node for node in projection.nodes}
            edges = list(projection.edges)
            applied = set(projection.applied_event_ids)
            active_phase_id = projection.active_phase_id
            last_child = dict(projection.last_child_by_parent)
            tool_by_invocation = dict(projection.tool_node_by_invocation)
            process_invocation_by_id = dict(projection.process_invocation_by_id)
            diagnostic_facts = list(projection.diagnostic_facts)
        if projection is None:
            tool_by_invocation: dict[str, str] = {}
            process_invocation_by_id: dict[str, str] = {}

        def add_edge(edge: FlowEdge) -> None:
            if edge not in edges:
                edges.append(edge)

        def add_node(node: FlowNode) -> None:
            if node.node_id in nodes:
                return
            nodes[node.node_id] = node
            if node.parent_id is not None:
                add_edge(FlowEdge(
                    node.parent_id, node.node_id, FlowEdgeRelation.CONTAINS
                ))
                previous = last_child.get(node.parent_id)
                if previous is not None and previous != node.node_id:
                    add_edge(FlowEdge(
                        previous, node.node_id, FlowEdgeRelation.NEXT
                    ))
                last_child[node.parent_id] = node.node_id

        def finish(
            node_id: str, event: RuntimeEvent, status: FlowNodeStatus,
            *, wait_reason: str | None = None,
        ) -> None:
            node = nodes.get(node_id)
            if node is None:
                return
            nodes[node_id] = replace(
                node, status=status, event_seq_end=event.sequence,
                finished_at=event.occurred_at if status.is_terminal else None,
                wait_reason=wait_reason,
            )

        def turn_parent(turn_id: str) -> str:
            candidate = f"turn:{turn_id}"
            return candidate if candidate in nodes else f"task:{task_id}"

        def add_fact(
            category: FlowDiagnosticCategory, event: RuntimeEvent, code: str,
            values: dict[str, FlowDiagnosticValue], *,
            node_id: str | None = None,
            invocation_id: object | None = None,
        ) -> None:
            fact = FlowDiagnosticFact(
                category=category, event_sequence=event.sequence, code=code,
                values=tuple(sorted(values.items())), node_id=node_id,
                invocation_id=(
                    str(invocation_id) if invocation_id is not None else None
                ),
            )
            if fact not in diagnostic_facts:
                diagnostic_facts.append(fact)

        def result_fact_values(payload: Any) -> dict[str, FlowDiagnosticValue]:
            result = payload.get("result") if isinstance(payload, Mapping) else None
            if not isinstance(result, Mapping):
                return {}
            meta = result.get("meta")
            meta = meta if isinstance(meta, Mapping) else {}
            return {
                "ok": bool(result.get("ok")),
                "error_code": (
                    str(result["error_code"])
                    if result.get("error_code") is not None else None
                ),
                "retryable": bool(result.get("retryable", False)),
                "recovery_kind": (
                    str(result["recovery_kind"])
                    if result.get("recovery_kind") is not None else None
                ),
                "truncated": bool(result.get("truncated", False)),
                "error_type": (
                    str(meta["error_type"])
                    if meta.get("error_type") is not None else None
                ),
            }

        for event in events:
            if event.event_id in applied:
                continue
            if event.task_id != task_id:
                raise FlowProjectionError("event stream contains multiple task IDs")
            if event.schema_version != 1:
                raise FlowProjectionError(
                    f"unsupported Runtime Event schema version: {event.schema_version}"
                )
            if event.sequence != cursor + 1:
                raise FlowProjectionError(
                    f"event sequence gap: expected {cursor + 1}, got {event.sequence}"
                )
            root_id = f"task:{task_id}"
            payload = event.payload
            event_type = event.event_type

            if event_type == "task.created":
                add_node(FlowNode(
                    root_id, task_id, None, FlowNodeKind.TASK, "Task",
                    FlowNodeStatus.RUNNING, event.occurred_at, None, None, None,
                    event.sequence, None,
                ))
            elif event_type == "task.state_changed":
                next_state = str(payload.get("next_state", "UNKNOWN"))
                terminal = {
                    "SUCCEEDED": FlowNodeStatus.SUCCEEDED,
                    "FAILED": FlowNodeStatus.FAILED,
                    "CANCELLED": FlowNodeStatus.CANCELLED,
                }
                waiting = {
                    "AWAITING_APPROVAL": FlowNodeStatus.WAITING_APPROVAL,
                    "AWAITING_USER": FlowNodeStatus.WAITING_USER,
                    "CONFLICT": FlowNodeStatus.WAITING_USER,
                }
                next_status = terminal.get(
                    next_state, waiting.get(next_state, FlowNodeStatus.RUNNING)
                )
                if active_phase_id is not None:
                    prior_status = (
                        next_status if next_status in {
                            FlowNodeStatus.FAILED, FlowNodeStatus.CANCELLED
                        } else FlowNodeStatus.SUCCEEDED
                    )
                    finish(active_phase_id, event, prior_status)
                phase_id = f"phase:{event.sequence}:{next_state}"
                add_node(FlowNode(
                    phase_id, task_id, root_id, FlowNodeKind.PHASE, next_state,
                    next_status, event.occurred_at,
                    event.occurred_at if next_status.is_terminal else None,
                    (
                        "waiting for user approval"
                        if next_status is FlowNodeStatus.WAITING_APPROVAL
                        else "waiting for user input"
                        if next_status is FlowNodeStatus.WAITING_USER
                        else None
                    ),
                    None, event.sequence,
                    event.sequence if next_status.is_terminal else None,
                ))
                active_phase_id = None if next_status.is_terminal else phase_id
                finish(root_id, event, next_status)
            elif event_type == "turn.started":
                turn_id = str(payload.get("turn_id", f"seq-{event.sequence}"))
                add_node(FlowNode(
                    f"turn:{turn_id}", task_id, root_id, FlowNodeKind.TURN,
                    "Turn", FlowNodeStatus.RUNNING, event.occurred_at, None,
                    None, None, event.sequence, None,
                ))
                add_fact(
                    FlowDiagnosticCategory.INPUT, event, "turn.input-summary",
                    {"max_output_tokens": (
                        int(payload["max_output_tokens"])
                        if isinstance(payload.get("max_output_tokens"), int)
                        else None
                    )}, node_id=f"turn:{turn_id}",
                )
            elif event_type in {"turn.completed", "turn.failed"}:
                turn_id = str(payload.get("turn_id", f"seq-{event.sequence}"))
                turn_node_id = f"turn:{turn_id}"
                finish(
                    turn_node_id, event,
                    FlowNodeStatus.SUCCEEDED
                    if event_type.endswith("completed") else FlowNodeStatus.FAILED,
                )
                if event_type == "turn.failed":
                    failed_child = next((
                        candidate for candidate in nodes.values()
                        if candidate.parent_id == turn_node_id
                        and candidate.status in {
                            FlowNodeStatus.FAILED,
                            FlowNodeStatus.UNKNOWN_OUTCOME,
                        }
                    ), None)
                    if failed_child is not None:
                        add_edge(FlowEdge(
                            failed_child.node_id, turn_node_id,
                            FlowEdgeRelation.CAUSED_BY,
                        ))
                    add_edge(FlowEdge(
                        turn_node_id, root_id, FlowEdgeRelation.CAUSED_BY,
                    ))
            elif event_type == "turn.resumed":
                turn_id = str(payload.get("turn_id", f"seq-{event.sequence}"))
                finish(f"turn:{turn_id}", event, FlowNodeStatus.RUNNING)
            elif event_type in {"llm.completed", "llm.failed"}:
                turn_id = str(payload.get("turn_id", f"seq-{event.sequence}"))
                model_call = payload.get("model_call", event.sequence)
                add_node(FlowNode(
                    f"model:{turn_id}:{model_call}", task_id,
                    turn_parent(turn_id), FlowNodeKind.MODEL,
                    f"Model sample {model_call}",
                    FlowNodeStatus.SUCCEEDED if event_type.endswith("completed")
                    else FlowNodeStatus.FAILED,
                    event.occurred_at, event.occurred_at, None, None,
                    event.sequence, event.sequence,
                ))
                model_node_id = f"model:{turn_id}:{model_call}"
                usage = payload.get("usage")
                if event_type == "llm.completed":
                    usage = usage if isinstance(usage, dict) else {}
                    add_fact(
                        FlowDiagnosticCategory.RESULT, event,
                        "model.output-summary", {
                            "finish_reason": (
                                str(payload["finish_reason"])
                                if payload.get("finish_reason") is not None else None
                            ),
                            "input_tokens": (
                                int(usage["input_tokens"])
                                if isinstance(usage.get("input_tokens"), int) else None
                            ),
                            "output_tokens": (
                                int(usage["output_tokens"])
                                if isinstance(usage.get("output_tokens"), int) else None
                            ),
                            "effective_prompt_hash": (
                                str(payload["effective_prompt_hash"])
                                if payload.get("effective_prompt_hash") is not None
                                else None
                            ),
                            "prompt_manifest_hash": (
                                str(payload["prompt_manifest_hash"])
                                if payload.get("prompt_manifest_hash") is not None
                                else None
                            ),
                            "prompt_message_count": (
                                int(payload["prompt_message_count"])
                                if isinstance(payload.get("prompt_message_count"), int)
                                else None
                            ),
                        }, node_id=model_node_id,
                    )
                else:
                    add_fact(
                        FlowDiagnosticCategory.FAILURE, event,
                        "model.failure", {
                            "error_type": (
                                str(payload["error_type"])
                                if payload.get("error_type") is not None else None
                            ),
                            "event_type": event_type,
                            "effective_prompt_hash": (
                                str(payload["effective_prompt_hash"])
                                if payload.get("effective_prompt_hash") is not None
                                else None
                            ),
                            "prompt_manifest_hash": (
                                str(payload["prompt_manifest_hash"])
                                if payload.get("prompt_manifest_hash") is not None
                                else None
                            ),
                        }, node_id=model_node_id,
                    )
            elif event_type == "policy.evaluated":
                invocation_id = payload.get("invocation_id")
                decision = payload.get("decision")
                decision = decision if isinstance(decision, Mapping) else {}
                factors = decision.get("risk_factors")
                add_fact(
                    FlowDiagnosticCategory.CONTROL, event,
                    "policy.evaluated", {
                        "action": (
                            str(decision["action"])
                            if decision.get("action") is not None else None
                        ),
                        "effective_risk": (
                            str(decision["effective_risk"])
                            if decision.get("effective_risk") is not None else None
                        ),
                        "requires_network": bool(
                            decision.get("requires_network", False)
                        ),
                        "risk_factor_count": (
                            len(factors) if isinstance(factors, list) else 0
                        ),
                    }, invocation_id=invocation_id,
                )
            elif self._investigation_projector is not None and (
                investigation := self._investigation_projector.project_event(
                    event_type, payload
                )
            ) is not None:
                category = {
                    "input": FlowDiagnosticCategory.INPUT,
                    "result": FlowDiagnosticCategory.RESULT,
                    "control": FlowDiagnosticCategory.CONTROL,
                    "failure": FlowDiagnosticCategory.FAILURE,
                }.get(investigation.category, FlowDiagnosticCategory.CONTROL)
                add_fact(
                    category, event, investigation.code,
                    dict(investigation.values),
                    invocation_id=f"call:{investigation.tool_call_id}",
                )
            elif event_type in {
                "tool.prepared", "tool.started", "tool.completed",
                "tool.failed", "tool.recovery_started", "tool.recovered",
                "tool.unknown_outcome", "tool.result_reused",
            }:
                turn_id = str(payload.get("turn_id", "unknown"))
                execution_id = payload.get("execution_id")
                invocation_id = payload.get("invocation_id")
                call = payload.get("call")
                call_id = call.get("call_id") if isinstance(call, dict) else None
                source = str(execution_id or invocation_id or call_id or f"seq-{event.sequence}")
                node_id = f"tool:{source}"
                if invocation_id:
                    tool_by_invocation[str(invocation_id)] = node_id
                if call_id:
                    tool_by_invocation[f"call:{call_id}"] = node_id
                existing = nodes.get(node_id)
                tool = payload.get("tool")
                tool_name = tool.get("name") if isinstance(tool, dict) else None
                if existing is None:
                    add_node(FlowNode(
                        node_id, task_id, turn_parent(turn_id), FlowNodeKind.TOOL,
                        str(tool_name or "Tool"), FlowNodeStatus.PENDING,
                        event.occurred_at, None, None, None, event.sequence, None,
                    ))
                if event_type in {"tool.prepared", "tool.started"}:
                    tool_data = tool if isinstance(tool, Mapping) else {}
                    add_fact(
                        FlowDiagnosticCategory.INPUT, event,
                        "tool.input-summary", {
                            "tool_name": (
                                str(tool_data["name"])
                                if tool_data.get("name") is not None else None
                            ),
                            "risk": (
                                str(tool_data["risk"])
                                if tool_data.get("risk") is not None else None
                            ),
                            "read_only": bool(
                                tool_data.get("is_read_only", False)
                            ),
                            "idempotency": (
                                str(payload["idempotency"])
                                if payload.get("idempotency") is not None else None
                            ),
                            "requires_network": bool(
                                tool_data.get("requires_network", False)
                            ),
                        }, node_id=node_id, invocation_id=invocation_id,
                    )
                statuses = {
                    "tool.prepared": FlowNodeStatus.PENDING,
                    "tool.started": FlowNodeStatus.RUNNING,
                    "tool.recovery_started": FlowNodeStatus.RETRYING,
                    "tool.completed": FlowNodeStatus.SUCCEEDED,
                    "tool.failed": FlowNodeStatus.FAILED,
                    "tool.recovered": FlowNodeStatus.SUCCEEDED,
                    "tool.unknown_outcome": FlowNodeStatus.UNKNOWN_OUTCOME,
                    "tool.result_reused": (
                        existing.status if existing else FlowNodeStatus.SUCCEEDED
                    ),
                }
                status = statuses[event_type]
                commit_state = str(payload.get("commit_state", ""))
                if commit_state == "UNKNOWN_OUTCOME":
                    status = FlowNodeStatus.UNKNOWN_OUTCOME
                elif event_type == "tool.recovered" and commit_state == "FAILED":
                    status = FlowNodeStatus.FAILED
                finish(node_id, event, status)
                if status in {
                    FlowNodeStatus.FAILED, FlowNodeStatus.UNKNOWN_OUTCOME,
                }:
                    failed_process = next((
                        candidate for candidate in nodes.values()
                        if candidate.parent_id == node_id
                        and candidate.kind is FlowNodeKind.PROCESS
                        and candidate.status in {
                            FlowNodeStatus.FAILED,
                            FlowNodeStatus.UNKNOWN_OUTCOME,
                        }
                    ), None)
                    if failed_process is not None:
                        add_edge(FlowEdge(
                            node_id, failed_process.node_id,
                            FlowEdgeRelation.DEPENDS_ON,
                        ))
                if event_type in {
                    "tool.completed", "tool.failed", "tool.recovered",
                    "tool.unknown_outcome", "tool.result_reused",
                }:
                    result_values = result_fact_values(payload)
                    result_values["commit_state"] = (
                        str(payload["commit_state"])
                        if payload.get("commit_state") is not None else None
                    )
                    add_fact(
                        (
                            FlowDiagnosticCategory.RESULT
                            if status is FlowNodeStatus.SUCCEEDED
                            else FlowDiagnosticCategory.FAILURE
                        ),
                        event,
                        "tool.result-summary" if status is FlowNodeStatus.SUCCEEDED
                        else "tool.failure",
                        {
                            **result_values,
                            "event_type": event_type,
                        }, node_id=node_id, invocation_id=invocation_id,
                    )
            elif event_type in {"approval.requested", "approval.resolved"}:
                request_id = str(payload.get("request_id", f"seq-{event.sequence}"))
                node_id = f"approval:{request_id}"
                if event_type == "approval.requested":
                    turn_id = str(payload.get("turn_id", "unknown"))
                    add_node(FlowNode(
                        node_id, task_id, turn_parent(turn_id),
                        FlowNodeKind.APPROVAL, "Approval",
                        FlowNodeStatus.WAITING_APPROVAL, event.occurred_at, None,
                        "waiting for user approval", None, event.sequence, None,
                    ))
                    add_fact(
                        FlowDiagnosticCategory.CONTROL, event,
                        "approval.requested", {
                            "risk": (
                                str(payload["risk"])
                                if payload.get("risk") is not None else None
                            ),
                            "network_access": (
                                str(payload["network_access"])
                                if payload.get("network_access") is not None else None
                            ),
                        }, node_id=node_id,
                        invocation_id=payload.get("invocation_id"),
                    )
                else:
                    decision = str(payload.get("decision", "")).lower()
                    approval_invocation_id = next((
                        fact.invocation_id for fact in diagnostic_facts
                        if fact.node_id == node_id
                        and fact.code == "approval.requested"
                    ), None)
                    finish(
                        node_id, event, FlowNodeStatus.SUCCEEDED
                        if decision == "approve" else FlowNodeStatus.CANCELLED,
                    )
                    add_fact(
                        FlowDiagnosticCategory.CONTROL, event,
                        "approval.resolved", {"decision": decision},
                        node_id=node_id,
                        invocation_id=approval_invocation_id,
                    )
            elif event_type in {
                "clarification.requested", "clarification.resolved",
            }:
                request_id = str(payload.get("request_id", f"seq-{event.sequence}"))
                node_id = f"clarification:{request_id}"
                if event_type == "clarification.requested":
                    turn_id = str(payload.get("turn_id", "unknown"))
                    add_node(FlowNode(
                        node_id, task_id, turn_parent(turn_id),
                        FlowNodeKind.CLARIFICATION, "Clarification",
                        FlowNodeStatus.WAITING_USER, event.occurred_at, None,
                        "waiting for user input", None, event.sequence, None,
                    ))
                    add_fact(
                        FlowDiagnosticCategory.CONTROL, event,
                        "clarification.requested", {
                            "choice_count": payload.get("choice_count"),
                            "required": payload.get("required"),
                            "expires_at": payload.get("expires_at"),
                        }, node_id=node_id,
                    )
                else:
                    finish(node_id, event, FlowNodeStatus.SUCCEEDED)
                    add_fact(
                        FlowDiagnosticCategory.CONTROL, event,
                        "clarification.resolved", {
                            "selected_choice": payload.get("selected_choice"),
                        }, node_id=node_id,
                    )
            elif event_type.startswith("verify."):
                verification_id = "verification:task"
                if event_type == "verify.started":
                    add_node(FlowNode(
                        verification_id, task_id, root_id,
                        FlowNodeKind.VERIFICATION, "Verification",
                        FlowNodeStatus.RUNNING, event.occurred_at, None,
                        None, None, event.sequence, None,
                    ))
                elif event_type == "verify.criterion_completed":
                    criterion_id = str(payload.get("criterion_id", "unknown"))
                    status_text = str(payload.get("status", "blocked"))
                    criterion_status = {
                        "passed": FlowNodeStatus.SUCCEEDED,
                        "failed": FlowNodeStatus.FAILED,
                        "blocked": FlowNodeStatus.WAITING_USER,
                    }.get(status_text, FlowNodeStatus.FAILED)
                    node_id = f"verification:{criterion_id}"
                    add_node(FlowNode(
                        node_id, task_id, verification_id,
                        FlowNodeKind.VERIFICATION, criterion_id, criterion_status,
                        event.occurred_at, event.occurred_at, None, None,
                        event.sequence, event.sequence,
                    ))
                    raw_evidence = payload.get("evidence", ())
                    evidence = raw_evidence if isinstance(raw_evidence, list) else []
                    add_fact(
                        FlowDiagnosticCategory.RESULT, event,
                        "verify.criterion", {
                            "criterion_id": criterion_id, "status": status_text,
                            "evidence_count": len(evidence),
                            "all_evidence_passed": all(
                                bool(item.get("passed"))
                                for item in evidence if isinstance(item, Mapping)
                            ),
                        }, node_id=node_id,
                    )
                elif event_type == "verify.completed":
                    status_text = str(payload.get("status", "failed"))
                    finish(
                        verification_id, event,
                        FlowNodeStatus.SUCCEEDED if status_text == "passed"
                        else FlowNodeStatus.FAILED if status_text == "failed"
                        else FlowNodeStatus.WAITING_USER,
                    )
            elif event_type.startswith("process.") and event_type in {
                "process.started", "process.exited", "process.cancelled",
                "process.failed", "process.denied", "process.orphaned",
            }:
                process_id = str(payload.get("process_id", f"seq-{event.sequence}"))
                node_id = f"process:{process_id}"
                turn_id = str(payload.get("turn_id", "unknown"))
                invocation_id = payload.get("invocation_id")
                if invocation_id is not None:
                    process_invocation_by_id[process_id] = str(invocation_id)
                else:
                    invocation_id = process_invocation_by_id.get(process_id)
                process_parent = (
                    tool_by_invocation.get(str(invocation_id))
                    if invocation_id is not None else None
                ) or turn_parent(turn_id)
                if node_id not in nodes:
                    add_node(FlowNode(
                        node_id, task_id, process_parent,
                        FlowNodeKind.PROCESS, "Process", FlowNodeStatus.RUNNING,
                        event.occurred_at, None, None, None, event.sequence, None,
                    ))
                process_status = {
                    "process.started": FlowNodeStatus.RUNNING,
                    "process.exited": (
                        FlowNodeStatus.SUCCEEDED if payload.get("exit_code") == 0
                        else FlowNodeStatus.FAILED
                    ),
                    "process.cancelled": FlowNodeStatus.CANCELLED,
                    "process.failed": FlowNodeStatus.FAILED,
                    "process.denied": FlowNodeStatus.FAILED,
                    "process.orphaned": FlowNodeStatus.UNKNOWN_OUTCOME,
                }
                finish(node_id, event, process_status[event_type])
                stdout = payload.get("stdout")
                stderr = payload.get("stderr")
                stdout = stdout if isinstance(stdout, Mapping) else {}
                stderr = stderr if isinstance(stderr, Mapping) else {}
                process_values: dict[str, FlowDiagnosticValue] = {
                    "event_type": event_type,
                    "background": bool(payload.get("background", False)),
                    "exit_code": (
                        int(payload["exit_code"])
                        if isinstance(payload.get("exit_code"), int) else None
                    ),
                    "status": (
                        str(payload["status"])
                        if payload.get("status") is not None else None
                    ),
                    "stdout_bytes": (
                        int(stdout["total_bytes"])
                        if isinstance(stdout.get("total_bytes"), int) else None
                    ),
                    "stdout_truncated": bool(stdout.get("truncated", False)),
                    "stderr_bytes": (
                        int(stderr["total_bytes"])
                        if isinstance(stderr.get("total_bytes"), int) else None
                    ),
                    "stderr_truncated": bool(stderr.get("truncated", False)),
                    "error_type": (
                        str(payload["error_type"])
                        if payload.get("error_type") is not None else None
                    ),
                }
                process_failed = process_status[event_type] in {
                    FlowNodeStatus.FAILED, FlowNodeStatus.UNKNOWN_OUTCOME,
                }
                if event_type == "process.exited" and payload.get("exit_code") != 0:
                    process_values["event_type"] = "process.nonzero_exit"
                add_fact(
                    FlowDiagnosticCategory.FAILURE if process_failed
                    else FlowDiagnosticCategory.PROCESS,
                    event, "process.failure" if process_failed
                    else "process.result-summary", process_values,
                    node_id=node_id, invocation_id=invocation_id,
                )
                if process_parent != turn_parent(turn_id):
                    add_edge(FlowEdge(
                        process_parent, node_id, FlowEdgeRelation.CAUSED_BY
                    ))
            elif event_type in {
                "workspace.mutation_committed", "workspace.rollback_committed"
            }:
                mutation_id = str(payload.get("mutation_id", f"seq-{event.sequence}"))
                step_id = str(payload.get("step_id", ""))
                parent_id = tool_by_invocation.get(step_id, root_id)
                node_id = f"mutation:{mutation_id}"
                add_node(FlowNode(
                    node_id, task_id, parent_id, FlowNodeKind.MUTATION,
                    "Workspace mutation", FlowNodeStatus.SUCCEEDED,
                    event.occurred_at, event.occurred_at, None, None,
                    event.sequence, event.sequence,
                ))
                add_fact(
                    FlowDiagnosticCategory.MUTATION, event,
                    "workspace.mutation-evidence", {
                        "operation": (
                            str(payload["operation"])
                            if payload.get("operation") is not None else None
                        ),
                        "before_hash": (
                            str(payload["before_hash"])
                            if payload.get("before_hash") is not None else None
                        ),
                        "after_hash": (
                            str(payload["after_hash"])
                            if payload.get("after_hash") is not None else None
                        ),
                        "reverts_mutation_id": (
                            str(payload["reverts_mutation_id"])
                            if payload.get("reverts_mutation_id") is not None else None
                        ),
                    }, node_id=node_id, invocation_id=step_id or None,
                )
                if parent_id != root_id:
                    add_edge(FlowEdge(
                        parent_id, node_id, FlowEdgeRelation.CAUSED_BY
                    ))
            elif event_type in {
                "memory.saved", "memory.verified", "memory.deleted",
                "working_memory.updated",
            }:
                memory_id = str(payload.get("memory_id", f"seq-{event.sequence}"))
                label = {
                    "memory.saved": "Memory saved",
                    "memory.verified": "Memory verified",
                    "memory.deleted": "Memory deleted",
                    "working_memory.updated": "Working memory updated",
                }[event_type]
                node_id = f"memory:{event.sequence}:{memory_id}"
                add_node(FlowNode(
                    node_id, task_id, root_id, FlowNodeKind.MEMORY, label,
                    FlowNodeStatus.SUCCEEDED, event.occurred_at, event.occurred_at,
                    None, None, event.sequence, event.sequence,
                ))
                add_fact(
                    FlowDiagnosticCategory.CONTROL, event, event_type,
                    {
                        "memory_id": memory_id,
                        "scope": (
                            str(payload["scope"]) if payload.get("scope") else None
                        ),
                        "revision": (
                            int(payload["revision"]) if payload.get("revision") else None
                        ),
                        "operation_replayed": bool(
                            payload.get("operation_replayed", False)
                        ),
                        "content_hash": (
                            str(payload["content_hash"])
                            if event_type == "working_memory.updated"
                            and payload.get("content_hash") else None
                        ),
                    }, node_id=node_id,
                )
            elif event_type in {
                "onboarding.started", "onboarding.phase_completed",
                "onboarding.completed", "onboarding.reused",
                "onboarding.invalidated",
            }:
                phase = str(payload.get("phase", event_type.split(".")[-1]))
                node_id = f"onboarding:{event.sequence}:{phase}"
                status = (
                    FlowNodeStatus.FAILED
                    if event_type == "onboarding.invalidated"
                    else FlowNodeStatus.SUCCEEDED
                )
                labels = {
                    "onboarding.started": "Onboarding started",
                    "onboarding.phase_completed": f"Onboarding: {phase}",
                    "onboarding.completed": "Onboarding completed",
                    "onboarding.reused": "Onboarding cache reused",
                    "onboarding.invalidated": "Onboarding checkpoint invalidated",
                }
                add_node(FlowNode(
                    node_id, task_id, root_id, FlowNodeKind.ONBOARDING,
                    labels[event_type], status, event.occurred_at, event.occurred_at,
                    None, None, event.sequence, event.sequence,
                ))
                add_fact(
                    FlowDiagnosticCategory.CONTROL, event, event_type, {
                        "revision": (
                            int(payload["revision"])
                            if isinstance(payload.get("revision"), int) else None
                        ),
                        "phase": phase,
                        "fact_count": (
                            int(payload["fact_count"])
                            if isinstance(payload.get("fact_count"), int) else None
                        ),
                        "inventory_truncated": bool(
                            payload.get("inventory_truncated", False)
                        ),
                    }, node_id=node_id,
                )
            elif event_type in {
                "plan.action_evaluated", "plan.no_progress_stopped",
            }:
                turn_id = str(payload.get("turn_id", "unknown"))
                stopped = event_type == "plan.no_progress_stopped"
                node_id = f"plan:{event.sequence}"
                add_node(FlowNode(
                    node_id, task_id, turn_parent(turn_id), FlowNodeKind.PLAN,
                    "Plan stopped: no progress" if stopped else "Plan action checked",
                    FlowNodeStatus.FAILED if stopped else FlowNodeStatus.SUCCEEDED,
                    event.occurred_at, event.occurred_at, None, None,
                    event.sequence, event.sequence,
                ))
                add_fact(
                    (FlowDiagnosticCategory.FAILURE if stopped
                     else FlowDiagnosticCategory.CONTROL),
                    event, event_type, {
                        "step_id": (
                            str(payload["step_id"])
                            if payload.get("step_id") is not None else None
                        ),
                        "tool_name": (
                            str(payload["tool_name"])
                            if payload.get("tool_name") is not None else None
                        ),
                        "implicit_step": bool(
                            payload.get("implicit_step", False)
                        ),
                        "consecutive_no_progress": (
                            int(payload["consecutive_no_progress"])
                            if isinstance(
                                payload.get("consecutive_no_progress"), int
                            ) else None
                        ),
                        "should_stop": bool(payload.get("should_stop", stopped)),
                        "limit": (
                            int(payload["limit"])
                            if isinstance(payload.get("limit"), int) else None
                        ),
                    }, node_id=node_id,
                )
            elif event_type in {
                "evidence.question_bound",
                "evidence.question_state_changed",
            }:
                turn_id = str(payload.get("turn_id", "unknown"))
                question_ref = str(payload.get("question_ref", "unknown"))
                node_id = f"evidence-question:{question_ref}"
                if event_type == "evidence.question_bound":
                    add_node(FlowNode(
                        node_id, task_id, turn_parent(turn_id),
                        FlowNodeKind.EVIDENCE_QUESTION,
                        f"Evidence question {question_ref}",
                        FlowNodeStatus.RUNNING, event.occurred_at, None, None,
                        None, event.sequence, None,
                    ))
                else:
                    next_status = str(payload.get("next_status", "OPEN"))
                    status = {
                        "OPEN": FlowNodeStatus.RUNNING,
                        "RESOLVED": FlowNodeStatus.SUCCEEDED,
                        "BLOCKED": FlowNodeStatus.WAITING_USER,
                        "DROPPED": FlowNodeStatus.SKIPPED,
                    }.get(next_status, FlowNodeStatus.FAILED)
                    finish(
                        node_id, event, status,
                        wait_reason=(
                            str(payload.get("blocking_reason"))
                            if status is FlowNodeStatus.WAITING_USER
                            and payload.get("blocking_reason") is not None
                            else None
                        ),
                    )
                    add_fact(
                        FlowDiagnosticCategory.RESULT, event,
                        "evidence.question-state", {
                            "question_ref": question_ref,
                            "previous_status": str(
                                payload.get("previous_status", "")
                            ),
                            "next_status": next_status,
                            "observation_kind": str(
                                payload.get("observation_kind", "")
                            ),
                            "blocking_reason": (
                                str(payload["blocking_reason"])
                                if payload.get("blocking_reason") is not None
                                else None
                            ),
                            "evidence_count": (
                                int(payload["evidence_count"])
                                if isinstance(payload.get("evidence_count"), int)
                                else 0
                            ),
                        }, node_id=node_id,
                    )
            elif event_type in {"steering.queued", "steering.applied"}:
                turn_id = str(payload.get("turn_id", "unknown"))
                steering_applied = event_type == "steering.applied"
                node_id = f"steering:{event.sequence}"
                add_node(FlowNode(
                    node_id, task_id,
                    turn_parent(turn_id) if steering_applied else root_id,
                    FlowNodeKind.STEERING,
                    ("Steering applied" if steering_applied
                     else "Steering queued"),
                    (FlowNodeStatus.SUCCEEDED if steering_applied
                     else FlowNodeStatus.PENDING),
                    event.occurred_at, event.occurred_at, None, None,
                    event.sequence, event.sequence,
                ))
                add_fact(
                    FlowDiagnosticCategory.CONTROL, event, event_type, {
                        "kind": (
                            str(payload["kind"])
                            if payload.get("kind") is not None else None
                        ),
                        "inbound_sequence": (
                            int(payload["inbound_sequence"])
                            if isinstance(payload.get("inbound_sequence"), int)
                            else None
                        ),
                        "safe_point": (
                            str(payload["safe_point"])
                            if payload.get("safe_point") is not None else None
                        ),
                        "goal_revision": (
                            int(payload["goal_revision"])
                            if isinstance(payload.get("goal_revision"), int)
                            else None
                        ),
                        "replaced_pending_tool_calls": (
                            int(payload["replaced_pending_tool_calls"])
                            if isinstance(
                                payload.get("replaced_pending_tool_calls"), int
                            ) else None
                        ),
                    }, node_id=node_id,
                )
            elif event_type in {"checkpoint.saved", "context.compacted"}:
                turn_id = str(payload.get("turn_id", "unknown"))
                label = (
                    "Context compacted"
                    if event_type == "context.compacted" else "Checkpoint"
                )
                add_node(FlowNode(
                    f"checkpoint:{event.sequence}", task_id,
                    turn_parent(turn_id),
                    FlowNodeKind.CHECKPOINT, label,
                    FlowNodeStatus.SUCCEEDED, event.occurred_at, event.occurred_at,
                    None, None, event.sequence, event.sequence,
                ))
                if event_type == "context.compacted":
                    add_fact(
                        FlowDiagnosticCategory.RESULT, event,
                        "context.compaction-summary", {
                            "algorithm": (
                                str(payload["algorithm"])
                                if payload.get("algorithm") is not None else None
                            ),
                            "source_message_count": (
                                int(payload["source_message_count"])
                                if isinstance(payload.get("source_message_count"), int)
                                else None
                            ),
                            "compacted_message_count": (
                                int(payload["compacted_message_count"])
                                if isinstance(payload.get("compacted_message_count"), int)
                                else None
                            ),
                            "preserved_message_count": (
                                int(payload["preserved_message_count"])
                                if isinstance(payload.get("preserved_message_count"), int)
                                else None
                            ),
                            "before_estimated_tokens": (
                                int(payload["before_estimated_tokens"])
                                if isinstance(payload.get("before_estimated_tokens"), int)
                                else None
                            ),
                            "after_estimated_tokens": (
                                int(payload["after_estimated_tokens"])
                                if isinstance(payload.get("after_estimated_tokens"), int)
                                else None
                            ),
                        }, node_id=f"checkpoint:{event.sequence}",
                    )
            elif event_type == "agent.limit_exceeded":
                turn_id = str(payload.get("turn_id", "unknown"))
                add_fact(
                    FlowDiagnosticCategory.FAILURE, event,
                    "agent.limit-exceeded", {
                        "event_type": event_type,
                        "limit": (
                            str(payload["limit"])
                            if payload.get("limit") is not None else None
                        ),
                        "maximum": (
                            int(payload["maximum"])
                            if isinstance(payload.get("maximum"), int) else None
                        ),
                    }, node_id=f"turn:{turn_id}",
                )

            applied.add(event.event_id)
            cursor = event.sequence

        return FlowProjection(
            task_id=task_id, cursor=cursor,
            nodes=tuple(sorted(nodes.values(), key=lambda node: (
                node.event_seq_start, node.node_id
            ))),
            edges=tuple(edges), applied_event_ids=tuple(sorted(applied)),
            active_phase_id=active_phase_id,
            last_child_by_parent=tuple(sorted(last_child.items())),
            tool_node_by_invocation=tuple(sorted(tool_by_invocation.items())),
            process_invocation_by_id=tuple(sorted(
                process_invocation_by_id.items()
            )),
            diagnostic_facts=tuple(sorted(
                diagnostic_facts,
                key=lambda fact: (
                    fact.event_sequence, fact.category.value, fact.code,
                    fact.node_id or "", fact.invocation_id or "",
                ),
            )),
        )
