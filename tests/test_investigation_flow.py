from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tsm_agt.adapters.rule_based_investigation_flow import (
    RuleBasedInvestigationFlowProjector,
)
from tsm_agt.bootstrap import (
    compose_fixture_application, compose_openai_compatible_readonly_application,
)
from tsm_agt.cli import render_flow_node_diagnostic
from tsm_agt.core import (
    FlowProjector, FlowReplay, FlowReplayBoundary, build_flow_export_document,
)
from tsm_agt.ports import (
    AdapterContext, InvestigationFlowProjectorPort, RuntimeEvent,
)

BASE = datetime(2026, 9, 5, tzinfo=timezone.utc)


def event(sequence: int, event_type: str, payload: dict | None = None) -> RuntimeEvent:
    return RuntimeEvent(
        f"evt-opt012-{sequence}", "task-opt012", sequence, event_type,
        payload or {}, BASE + timedelta(milliseconds=sequence),
    )


def investigation_events() -> tuple[RuntimeEvent, ...]:
    private = {
        "query": "PrivateFlowQuery", "path": "src/private-flow.py",
        "target_hash": "PRIVATE_TARGET_HASH",
        "semantic_signature": "PRIVATE_SIGNATURE",
    }
    return (
        event(1, "task.created"),
        event(2, "turn.started", {"turn_id": "turn-flow"}),
        event(3, "semantic.action_classified", {
            "turn_id": "turn-flow", "tool_call_id": "call-flow",
            "family": "SEARCH_DEFINITION", "scope_kind": "directory",
            "confidence": 0.95, **private,
        }),
        event(4, "stop_or_pivot.decision_made", {
            "turn_id": "turn-flow", "tool_call_id": "call-flow",
            "action": "CHANGE_METHOD", "reason": "consecutive_low_value",
            "terminal": False, "candidate_count": 0,
            "low_value_streak": 2, **private,
        }),
        event(5, "agent_progress.projected", {
            "turn_id": "turn-flow", "tool_call_id": "call-flow",
            "phase": "planned", "scope": "directory",
            "scope_change": "narrowed", "question_ref": "PRIVATE_Q",
            **private,
        }),
        event(6, "tool.prepared", {
            "turn_id": "turn-flow", "invocation_id": "inv-flow",
            "execution_id": "exec-flow",
            "call": {"call_id": "call-flow", "arguments": private},
            "tool": {"name": "core.search_text", "risk": "R0",
                     "is_read_only": True},
        }),
        event(7, "tool.started", {
            "turn_id": "turn-flow", "invocation_id": "inv-flow",
            "execution_id": "exec-flow",
            "call": {"call_id": "call-flow"},
        }),
        event(8, "tool.completed", {
            "turn_id": "turn-flow", "invocation_id": "inv-flow",
            "execution_id": "exec-flow", "result": {"ok": True},
        }),
        event(9, "evidence.delta_evaluated", {
            "turn_id": "turn-flow", "tool_call_id": "call-flow",
            "question_id": "PRIVATE_QUESTION_ID", "total_new": 3,
            "counts": {"new_paths": 1, "new_symbols": 1,
                       "new_relations": 1, "private": 99},
            "consecutive_zero_delta": 0, "items": [private], **private,
        }),
        event(10, "exploration_budget.action_scored", {
            "turn_id": "turn-flow", "tool_call_id": "call-flow",
            "score": -20, "value_band": "low", "new_evidence": 3,
            "semantic_repeat": True, "low_value_streak": 2,
            "elapsed_milliseconds": 8000, **private,
        }),
        event(11, "agent_progress.projected", {
            "turn_id": "turn-flow", "tool_call_id": "call-flow",
            "phase": "tool_result", "scope": "directory",
            "scope_change": "narrowed", "question_ref": "PRIVATE_Q",
            **private,
        }),
        event(12, "turn.completed", {"turn_id": "turn-flow"}),
    )


class InvestigationFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.projector = RuleBasedInvestigationFlowProjector()
        await self.projector.start(
            AdapterContext(config={}, emit_event=lambda *_: None)
        )

    async def asyncTearDown(self):
        await self.projector.stop(datetime.now(timezone.utc))

    async def test_tool_diagnostic_contains_ordered_safe_investigation_facts(self):
        projection = FlowProjector(self.projector).project(investigation_events())
        diagnostic = projection.inspect_node("tool:exec-flow")
        facts = [
            fact for fact in diagnostic.facts
            if fact.code.startswith("investigation.")
        ]
        self.assertEqual([fact.code for fact in facts], [
            "investigation.semantic-action",
            "investigation.route-decision",
            "investigation.scope",
            "investigation.evidence-delta",
            "investigation.budget-score",
            "investigation.scope",
        ])
        semantic = dict(facts[0].values)
        self.assertEqual(semantic["family"], "SEARCH_DEFINITION")
        self.assertEqual(semantic["scope"], "directory")
        route = dict(facts[1].values)
        self.assertEqual(route["action"], "CHANGE_METHOD")
        self.assertEqual(route["reason_code"], "consecutive_low_value")
        delta = dict(facts[3].values)
        self.assertEqual(delta["total_new"], 3)
        self.assertEqual(delta["new_relations"], 1)
        budget = dict(facts[4].values)
        self.assertEqual(budget["score"], -20)
        self.assertEqual(budget["value_band"], "low")
        rendered = render_flow_node_diagnostic(diagnostic, projection.cursor)
        self.assertIn("investigation.semantic-action", rendered)
        self.assertIn("investigation.evidence-delta", rendered)
        self.assertIn("investigation.route-decision", rendered)
        encoded = str(diagnostic.to_data()) + str(build_flow_export_document(projection))
        exported = build_flow_export_document(projection)
        self.assertTrue(any(
            item["code"] == "investigation.evidence-delta"
            for item in exported["diagnostic_facts"]
        ))
        self.assertTrue(any(
            item["code"] == "investigation.route-decision"
            for item in exported["flow"]["diagnostic_facts"]
        ))
        for private in (
            "PrivateFlowQuery", "src/private-flow.py", "PRIVATE_TARGET_HASH",
            "PRIVATE_SIGNATURE", "PRIVATE_QUESTION_ID", "PRIVATE_Q",
        ):
            self.assertNotIn(private, encoded)

    async def test_incremental_and_full_projection_are_identical(self):
        events = investigation_events()
        projector = FlowProjector(self.projector)
        partial = projector.project(events[:5])
        incremental = projector.apply(partial, events[5:])
        full = projector.project(events)
        self.assertEqual(incremental, full)

    async def test_replay_shows_only_facts_available_at_cursor(self):
        events = investigation_events()
        replay = FlowReplay(self.projector)
        before_result = replay.snapshot(
            events, "EXECUTING", at_sequence=8
        )
        early = before_result.projection.inspect_node("tool:exec-flow")
        early_codes = {fact.code for fact in early.facts}
        self.assertIn("investigation.semantic-action", early_codes)
        self.assertNotIn("investigation.evidence-delta", early_codes)
        after_result = replay.snapshot(
            events, "EXECUTING", at_sequence=11
        )
        late = after_result.projection.inspect_node("tool:exec-flow")
        late_codes = {fact.code for fact in late.facts}
        self.assertIn("investigation.evidence-delta", late_codes)
        self.assertIn("investigation.budget-score", late_codes)
        replay_json = after_result.to_data()
        self.assertTrue(any(
            item["code"] == "investigation.evidence-delta"
            for item in replay_json["flow"]["diagnostic_facts"]
        ))
        ended = replay.snapshot_for_node(
            events, "EXECUTING", "tool:exec-flow", FlowReplayBoundary.END
        )
        self.assertEqual(ended.cursor, 8)


class InvestigationFlowCompositionTest(unittest.TestCase):
    def test_fixture_optional_replaceable_and_real_default(self):
        self.assertEqual(
            compose_fixture_application().registry.all(
                InvestigationFlowProjectorPort
            ), ()
        )
        projector = RuleBasedInvestigationFlowProjector()
        fixture = compose_fixture_application(
            investigation_flow_projector_adapter=projector
        )
        self.assertIs(
            fixture.registry.require(InvestigationFlowProjectorPort), projector
        )
        real = compose_openai_compatible_readonly_application(
            base_url="https://example.test/v1", model="model", api_key="key"
        )
        self.assertIsInstance(
            real.registry.require(InvestigationFlowProjectorPort),
            RuleBasedInvestigationFlowProjector,
        )


if __name__ == "__main__":
    unittest.main()
