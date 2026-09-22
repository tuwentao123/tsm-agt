from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    FlowEdgeRelation, FlowLane, FlowNodeKind, FlowNodeStatus, FlowProjectionError,
    FlowProjector, TaskState, build_flow_export_document,
)
from tsm_agt.ports import RuntimeEvent, RuntimeStorePort, ToolCall


BASE_TIME = datetime(2026, 9, 2, tzinfo=timezone.utc)


def event(
    sequence: int, event_type: str, payload: dict | None = None, *,
    event_id: str | None = None, schema_version: int = 1,
) -> RuntimeEvent:
    return RuntimeEvent(
        event_id or f"evt-{sequence}", "task-flow", sequence, event_type,
        payload or {}, BASE_TIME + timedelta(seconds=sequence), schema_version,
    )


class FlowProjectorContractTest(unittest.TestCase):
    def lifecycle_events(self) -> tuple[RuntimeEvent, ...]:
        return (
            event(1, "task.created", {"goal": "change a file"}),
            event(2, "task.state_changed", {
                "previous_state": "CREATED", "next_state": "EXECUTING",
                "reason": "start execution",
            }),
            event(3, "turn.started", {"turn_id": "turn-1"}),
            event(4, "policy.evaluated", {"turn_id": "turn-1"}),
            event(5, "approval.requested", {
                "turn_id": "turn-1", "request_id": "approval-1",
            }),
            event(6, "task.state_changed", {
                "previous_state": "EXECUTING",
                "next_state": "AWAITING_APPROVAL",
                "reason": "approval required",
            }),
            event(7, "approval.resolved", {
                "request_id": "approval-1", "decision": "approve",
            }),
            event(8, "task.state_changed", {
                "previous_state": "AWAITING_APPROVAL",
                "next_state": "EXECUTING", "reason": "approved",
            }),
            event(9, "tool.prepared", {
                "turn_id": "turn-1", "invocation_id": "inv-1",
                "execution_id": "exec-1",
                "tool": {"name": "core.apply_patch"},
                "call": {"call_id": "call-1"},
            }),
            event(10, "tool.started", {
                "turn_id": "turn-1", "invocation_id": "inv-1",
                "execution_id": "exec-1",
            }),
            event(11, "workspace.mutation_committed", {
                "mutation_id": "mutation-1", "step_id": "inv-1",
                "path": "src/app.py",
            }),
            event(12, "tool.completed", {
                "turn_id": "turn-1", "invocation_id": "inv-1",
                "execution_id": "exec-1",
            }),
            event(13, "turn.completed", {"turn_id": "turn-1"}),
            event(14, "task.state_changed", {
                "previous_state": "EXECUTING", "next_state": "SUCCEEDED",
                "reason": "done",
            }),
        )

    def test_projects_lifecycle_hierarchy_status_and_causality(self) -> None:
        projection = FlowProjector().project(self.lifecycle_events())
        nodes = {node.node_id: node for node in projection.nodes}

        self.assertEqual(projection.cursor, 14)
        self.assertEqual(nodes["task:task-flow"].status, FlowNodeStatus.SUCCEEDED)
        self.assertEqual(nodes["turn:turn-1"].status, FlowNodeStatus.SUCCEEDED)
        self.assertEqual(nodes["tool:exec-1"].status, FlowNodeStatus.SUCCEEDED)
        self.assertEqual(
            nodes["approval:approval-1"].status, FlowNodeStatus.SUCCEEDED
        )
        self.assertEqual(
            nodes["mutation:mutation-1"].parent_id, "tool:exec-1"
        )
        self.assertIn(
            ("tool:exec-1", "mutation:mutation-1", "caused_by"),
            {
                (edge.from_node, edge.to_node, edge.relation.value)
                for edge in projection.edges
            },
        )
        waiting = next(
            node for node in projection.nodes
            if node.kind is FlowNodeKind.PHASE
            and node.label == "AWAITING_APPROVAL"
        )
        self.assertEqual(waiting.status, FlowNodeStatus.SUCCEEDED)

    def test_projects_redacted_steering_queue_and_safe_point(self) -> None:
        projection = FlowProjector().project((
            event(1, "task.created", {"goal": "old"}),
            event(2, "turn.started", {"turn_id": "turn-1"}),
            event(3, "steering.queued", {
                "kind": "replace", "inbound_sequence": 1,
                "text": "private replacement text",
            }),
            event(4, "steering.applied", {
                "turn_id": "turn-1", "safe_point": "before-tool",
                "goal_revision": 2, "replaced_pending_tool_calls": 1,
            }),
        ))
        steering = [
            node for node in projection.nodes
            if node.kind is FlowNodeKind.STEERING
        ]
        self.assertEqual(
            [node.label for node in steering],
            ["Steering queued", "Steering applied"],
        )
        applied = projection.inspect_node("steering:4")
        fact = next(item for item in applied.facts if item.code == "steering.applied")
        self.assertEqual(dict(fact.values)["safe_point"], "before-tool")
        self.assertNotIn(
            "private replacement text", str(build_flow_export_document(projection))
        )

    def test_full_and_incremental_projection_are_identical(self) -> None:
        events = self.lifecycle_events()
        projector = FlowProjector()
        partial = projector.project(events[:6])
        incremental = projector.apply(partial, events[6:])
        full = projector.project(events)
        self.assertEqual(incremental, full)
        self.assertEqual(projector.apply(full, (events[-1],)), full)

    def test_unknown_event_advances_cursor_without_inventing_node(self) -> None:
        projection = FlowProjector().project((
            event(1, "task.created"), event(2, "future.optional_event"),
        ))
        self.assertEqual(projection.cursor, 2)
        self.assertEqual(len(projection.nodes), 1)

    def test_gap_mixed_task_and_unknown_schema_fail_closed(self) -> None:
        projector = FlowProjector()
        cases = (
            (event(2, "task.created"),),
            (event(1, "task.created", schema_version=2),),
            (
                event(1, "task.created"),
                RuntimeEvent("other", "other-task", 2, "task.created"),
            ),
        )
        for events in cases:
            with self.subTest(events=events):
                with self.assertRaises(FlowProjectionError):
                    projector.project(events)

    def test_first_actionable_failure_ignores_derived_task_failure(self) -> None:
        projection = FlowProjector().project((
            event(1, "task.created"),
            event(2, "turn.started", {"turn_id": "turn-1"}),
            event(3, "llm.failed", {
                "turn_id": "turn-1", "message": "provider failed",
            }),
            event(4, "turn.failed", {"turn_id": "turn-1"}),
            event(5, "task.state_changed", {
                "previous_state": "EXECUTING", "next_state": "FAILED",
                "reason": "model invocation failed",
            }),
        ))
        failure = projection.first_actionable_failure
        self.assertIsNotNone(failure)
        assert failure is not None
        self.assertEqual(failure.kind, FlowNodeKind.MODEL)
        self.assertEqual(failure.event_seq_start, 3)

    def test_unknown_outcome_is_not_flattened_to_failed(self) -> None:
        projection = FlowProjector().project((
            event(1, "task.created"),
            event(2, "tool.failed", {
                "turn_id": "turn-without-start-event",
                "execution_id": "exec-unknown",
                "commit_state": "UNKNOWN_OUTCOME",
            }),
        ))
        nodes = {node.node_id: node for node in projection.nodes}
        tool = nodes["tool:exec-unknown"]
        self.assertEqual(tool.status, FlowNodeStatus.UNKNOWN_OUTCOME)
        self.assertEqual(tool.parent_id, "task:task-flow")
        node_ids = set(nodes)
        self.assertTrue(all(
            edge.from_node in node_ids and edge.to_node in node_ids
            for edge in projection.edges
        ))

    def test_node_diagnostic_has_duration_relationships_and_event_anchors(self) -> None:
        projection = FlowProjector().project(self.lifecycle_events())
        diagnostic = projection.inspect_node("tool:exec-1")

        self.assertEqual(diagnostic.node.kind, FlowNodeKind.TOOL)
        self.assertEqual(diagnostic.duration_ms, 3000)
        self.assertEqual(diagnostic.parent.node_id, "turn:turn-1")
        self.assertEqual(
            [node.node_id for node in diagnostic.children],
            ["mutation:mutation-1"],
        )
        self.assertEqual(diagnostic.event_anchors, (9, 12))
        self.assertIn(
            ("tool:exec-1", "mutation:mutation-1", "caused_by"),
            {
                (edge.from_node, edge.to_node, edge.relation.value)
                for edge in diagnostic.outgoing_edges
            },
        )
        self.assertFalse(diagnostic.is_first_actionable_failure)

    def test_node_diagnostic_marks_first_failure_and_rejects_unknown_node(self) -> None:
        projection = FlowProjector().project((
            event(1, "task.created"),
            event(2, "turn.started", {"turn_id": "turn-1"}),
            event(3, "llm.failed", {"turn_id": "turn-1"}),
            event(4, "turn.failed", {"turn_id": "turn-1"}),
        ))
        diagnostic = projection.inspect_node("model:turn-1:3")
        self.assertTrue(diagnostic.is_first_actionable_failure)
        self.assertEqual(diagnostic.event_anchors, (3,))
        with self.assertRaisesRegex(LookupError, "flow node not found"):
            projection.inspect_node("tool:missing")
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            projection.inspect_node("  ")

    def test_node_diagnostic_allowlists_facts_causality_and_attribution(self) -> None:
        events = (
            event(1, "task.created", {"goal": "PRIVATE_GOAL"}),
            event(2, "turn.started", {
                "turn_id": "turn-1", "input": {"text": "PRIVATE_PROMPT"},
                "max_output_tokens": 400,
            }),
            event(3, "policy.evaluated", {
                "turn_id": "turn-1", "invocation_id": "inv-1",
                "decision": {
                    "action": "require_approval", "effective_risk": "R2",
                    "requires_network": False, "risk_factors": ["write"],
                    "reason": "PRIVATE_POLICY_REASON",
                },
            }),
            event(4, "approval.requested", {
                "turn_id": "turn-1", "request_id": "approval-1",
                "invocation_id": "inv-1", "risk": "R2",
                "network_access": "not required", "preview": "PRIVATE_PREVIEW",
                "call": {"arguments": {"text": "PRIVATE_ARGUMENT"}},
            }),
            event(5, "approval.resolved", {
                "request_id": "approval-1", "decision": "approve",
                "reason": "PRIVATE_APPROVAL_REASON",
            }),
            event(6, "tool.prepared", {
                "turn_id": "turn-1", "invocation_id": "inv-1",
                "execution_id": "exec-1",
                "call": {"call_id": "call-1", "arguments": {
                    "text": "PRIVATE_ARGUMENT",
                }},
                "tool": {
                    "name": "fixture.write", "risk": "R2",
                    "is_read_only": False, "requires_network": False,
                },
                "idempotency": "keyed",
            }),
            event(7, "workspace.mutation_committed", {
                "mutation_id": "mutation-1", "step_id": "inv-1",
                "path": "PRIVATE_PATH", "operation": "modify",
                "before_hash": "before-hash", "after_hash": "after-hash",
            }),
            event(8, "process.started", {
                "turn_id": "turn-1", "invocation_id": "inv-1",
                "process_id": "process-1", "argv_hash": "safe-hash",
            }),
            event(9, "process.exited", {
                "turn_id": "turn-1",
                "process_id": "process-1", "exit_code": 2,
                "status": "exited",
                "stdout": {"text": "PRIVATE_STDOUT", "total_bytes": 14,
                           "truncated": False},
                "stderr": {"text": "PRIVATE_STDERR", "total_bytes": 14,
                           "truncated": True},
            }),
            event(10, "tool.failed", {
                "turn_id": "turn-1", "invocation_id": "inv-1",
                "execution_id": "exec-1", "commit_state": "FAILED",
                "result": {
                    "ok": False, "error_code": "TOOL_FAILED",
                    "message": "PRIVATE_ERROR_MESSAGE", "retryable": False,
                    "truncated": False, "meta": {"error_type": "RuntimeError"},
                },
            }),
            event(11, "turn.failed", {"turn_id": "turn-1"}),
            event(12, "task.state_changed", {"next_state": "FAILED"}),
        )
        projection = FlowProjector().project(events)
        diagnostic = projection.inspect_node("tool:exec-1")

        self.assertEqual(diagnostic.failure_attribution.code, "F5")
        self.assertEqual(diagnostic.failure_attribution.confidence, "deterministic")
        self.assertEqual(
            {fact.code for fact in diagnostic.facts},
            {
                "policy.evaluated", "approval.requested", "approval.resolved",
                "tool.input-summary", "workspace.mutation-evidence",
                "process.result-summary", "process.failure", "tool.failure",
            },
        )
        self.assertEqual(
            {node.node_id for node in diagnostic.downstream_nodes},
            {
                "process:process-1", "mutation:mutation-1", "turn:turn-1",
                "task:task-flow",
            },
        )
        mutation = next(
            fact for fact in diagnostic.facts
            if fact.code == "workspace.mutation-evidence"
        )
        self.assertEqual(dict(mutation.values)["after_hash"], "after-hash")
        rendered = str(diagnostic.to_data())
        for private in (
            "PRIVATE_GOAL", "PRIVATE_PROMPT", "PRIVATE_POLICY_REASON",
            "PRIVATE_PREVIEW", "PRIVATE_ARGUMENT", "PRIVATE_PATH",
            "PRIVATE_STDOUT", "PRIVATE_STDERR", "PRIVATE_ERROR_MESSAGE",
            "PRIVATE_APPROVAL_REASON",
        ):
            self.assertNotIn(private, rendered)

        process = projection.inspect_node("process:process-1")
        self.assertEqual(process.failure_attribution.code, "F13")
        self.assertEqual(
            {node.node_id for node in process.downstream_nodes},
            {
                "tool:exec-1", "mutation:mutation-1", "turn:turn-1",
                "task:task-flow",
            },
        )

    def test_failed_node_without_deterministic_rule_is_unclassified(self) -> None:
        projection = FlowProjector().project((
            event(1, "task.created"),
            event(2, "tool.failed", {
                "turn_id": "turn-1", "execution_id": "exec-1",
                "result": {"ok": False, "error_code": "CUSTOM_FAILURE"},
            }),
        ))
        attribution = projection.inspect_node("tool:exec-1").failure_attribution
        self.assertIsNotNone(attribution)
        self.assertIsNone(attribution.code)
        self.assertEqual(attribution.confidence, "unclassified")

    def test_filter_keeps_matches_ancestors_and_only_original_edges(self) -> None:
        projection = FlowProjector().project(self.lifecycle_events())
        view = projection.filter_nodes(
            kinds=(FlowNodeKind.TOOL, FlowNodeKind.MUTATION),
            statuses=(FlowNodeStatus.SUCCEEDED,),
        )

        self.assertEqual(
            view.matched_node_ids, ("tool:exec-1", "mutation:mutation-1")
        )
        self.assertEqual(
            {node.node_id for node in view.nodes},
            {
                "task:task-flow", "turn:turn-1", "tool:exec-1",
                "mutation:mutation-1",
            },
        )
        original_edges = set(projection.edges)
        self.assertTrue(all(edge in original_edges for edge in view.edges))
        self.assertTrue(all(
            edge.from_node in {node.node_id for node in view.nodes}
            and edge.to_node in {node.node_id for node in view.nodes}
            for edge in view.edges
        ))
        self.assertEqual(
            view.to_data()["filters"],
            {"kinds": ["tool", "mutation"], "statuses": ["SUCCEEDED"]},
        )

    def test_filter_supports_no_matches_and_validates_filters(self) -> None:
        projection = FlowProjector().project(self.lifecycle_events())
        view = projection.filter_nodes(statuses=(FlowNodeStatus.UNKNOWN_OUTCOME,))
        self.assertEqual(view.nodes, ())
        self.assertEqual(view.edges, ())
        self.assertEqual(view.matched_node_ids, ())
        with self.assertRaisesRegex(ValueError, "at least one"):
            projection.filter_nodes()
        with self.assertRaisesRegex(TypeError, "FlowNodeKind"):
            projection.filter_nodes(kinds=("tool",))  # type: ignore[arg-type]

    def test_export_document_is_versioned_deterministic_and_payload_free(self) -> None:
        projection = FlowProjector().project(self.lifecycle_events())
        view = projection.filter_nodes(kinds=(FlowNodeKind.TOOL,))
        document = build_flow_export_document(projection, view)

        self.assertEqual(document["artifact_type"], "tsm-agt.flow")
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["cursor"], projection.cursor)
        self.assertEqual(document["flow"], view.to_data())
        self.assertEqual(
            {item["node_id"] for item in document["timeline"]["items"]},
            {node.node_id for node in view.nodes},
        )
        self.assertIn("event_payloads", document["redaction"]["excluded"])
        self.assertEqual(document, build_flow_export_document(projection, view))
        rendered = str(document)
        self.assertNotIn("change a file", rendered)
        self.assertNotIn("src/app.py", rendered)

    def test_export_document_rejects_view_from_another_cursor(self) -> None:
        projection = FlowProjector().project(self.lifecycle_events())
        partial = FlowProjector().project(self.lifecycle_events()[:-1])
        view = partial.filter_nodes(kinds=(FlowNodeKind.TOOL,))
        with self.assertRaisesRegex(ValueError, "does not belong"):
            build_flow_export_document(projection, view)

    def test_timeline_is_deterministic_payload_free_and_uses_stable_lanes(self) -> None:
        projection = FlowProjector().project(self.lifecycle_events())
        timeline = projection.timeline()

        self.assertEqual(timeline, projection.timeline())
        self.assertEqual(timeline.cursor, projection.cursor)
        self.assertIn(FlowLane.TOOL, timeline.lanes)
        self.assertIn(FlowLane.USER, timeline.lanes)
        self.assertIn(FlowLane.WORKSPACE, timeline.lanes)
        tool = next(item for item in timeline.items if item.node_id == "tool:exec-1")
        self.assertEqual(tool.lane, FlowLane.TOOL)
        self.assertEqual(tool.duration_ms, 3000)
        self.assertEqual(
            [item.event_seq_start for item in timeline.items],
            sorted(item.event_seq_start for item in timeline.items),
        )
        rendered = str(timeline.to_data())
        self.assertNotIn("change a file", rendered)
        self.assertNotIn("src/app.py", rendered)

    def test_wait_reason_is_structured_not_copied_from_event_text(self) -> None:
        projection = FlowProjector().project((
            event(1, "task.created"),
            event(2, "task.state_changed", {
                "next_state": "AWAITING_USER",
                "reason": "private user text must not enter projection",
            }),
        ))
        phase = next(
            node for node in projection.nodes if node.kind is FlowNodeKind.PHASE
        )
        self.assertEqual(phase.wait_reason, "waiting for user input")
        self.assertNotIn(
            "private user text", str(build_flow_export_document(projection))
        )

    def test_clarification_node_is_visible_without_question_or_answer(self) -> None:
        projection = FlowProjector().project((
            event(1, "task.created"),
            event(2, "turn.started", {"turn_id": "turn-question"}),
            event(3, "clarification.requested", {
                "request_id": "question-1", "turn_id": "turn-question",
                "question_hash": "hash-only", "choice_count": 2,
                "required": True, "expires_at": "later",
            }),
            event(4, "clarification.resolved", {
                "request_id": "question-1", "turn_id": "turn-question",
                "answer_hash": "answer-hash", "selected_choice": "dark",
            }),
        ))
        node = next(
            item for item in projection.nodes
            if item.kind is FlowNodeKind.CLARIFICATION
        )
        self.assertEqual(node.status, FlowNodeStatus.SUCCEEDED)
        exported = str(build_flow_export_document(projection))
        self.assertNotIn("question_hash", exported)
        self.assertNotIn("answer_hash", exported)

    def test_verification_criteria_are_projected_without_evidence_text(self) -> None:
        projection = FlowProjector().project((
            event(1, "task.created"),
            event(2, "verify.started", {"criterion_count": 1}),
            event(3, "verify.criterion_completed", {
                "criterion_id": "tests", "status": "passed",
                "evidence": [{
                    "passed": True, "observed": "private command output",
                }],
            }),
            event(4, "verify.completed", {"status": "passed"}),
        ))
        criterion = next(
            item for item in projection.nodes
            if item.node_id == "verification:tests"
        )
        self.assertEqual(criterion.status, FlowNodeStatus.SUCCEEDED)
        exported = str(build_flow_export_document(projection))
        self.assertNotIn("private command output", exported)
        diagnostic = projection.inspect_node("verification:tests")
        fact = next(
            item for item in diagnostic.facts if item.code == "verify.criterion"
        )
        self.assertEqual(dict(fact.values)["evidence_count"], 1)

    def test_inapplicable_criterion_is_skipped_rather_than_failed(self) -> None:
        """A criterion whose precondition never held did not fail.

        The status map falls back to FAILED, so an unmapped status would render
        a failure the Task never had.
        """
        projection = FlowProjector().project((
            event(1, "task.created"),
            event(2, "verify.started", {"criterion_count": 1}),
            event(3, "verify.criterion_completed", {
                "criterion_id": "build", "status": "not_applicable",
                "evidence": [{
                    "passed": True,
                    "observed": "not applicable: task produced no surviving "
                                "workspace mutation",
                }],
            }),
            event(4, "verify.completed", {"status": "passed"}),
        ))
        criterion = next(
            item for item in projection.nodes
            if item.node_id == "verification:build"
        )
        self.assertEqual(criterion.status, FlowNodeStatus.SKIPPED)
        self.assertNotEqual(criterion.status, FlowNodeStatus.FAILED)
        self.assertTrue(criterion.status.is_terminal)


class KernelFlowProjectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_kernel_projects_persisted_events_and_updates_from_cursor(self) -> None:
        application = compose_fixture_application()
        await application.registry.start_all()
        temporary = tempfile.TemporaryDirectory()
        try:
            task = await application.kernel.create_task(
                "project real events", Path(temporary.name), "task-real-flow"
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await application.kernel.transition_task(
                    task.task_id, state, state.value
                )
            before = await application.kernel.get_flow_projection(task.task_id)
            result = await application.kernel.invoke_tool(
                task.task_id, "turn-flow",
                ToolCall("call-flow", "fixture.echo", {"text": "hello"}),
            )
            self.assertTrue(result.ok)
            incremental = await application.kernel.get_flow_projection(
                task.task_id, before
            )
            events = await application.registry.require(RuntimeStorePort).read_events(
                task.task_id
            )
            full = FlowProjector().project(events)
            self.assertEqual(incremental, full)
            tool = next(
                node for node in full.nodes if node.kind is FlowNodeKind.TOOL
            )
            self.assertEqual(tool.status, FlowNodeStatus.SUCCEEDED)
            node_ids = {node.node_id for node in full.nodes}
            self.assertTrue(all(
                edge.from_node in node_ids and edge.to_node in node_ids
                for edge in full.edges
            ))
        finally:
            temporary.cleanup()
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
