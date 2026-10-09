from __future__ import annotations

import unittest

from tsm_agt.core.task import TaskSnapshot
from tsm_agt.ports import RuntimeEvent
from tsm_agt.sdk.runtime import RuntimeTaskResult, _single_loop_projection


class RuntimeTaskResultTest(unittest.TestCase):
    def test_runtime_api_result_exposes_phase1_state_with_legacy_state(self) -> None:
        result = RuntimeTaskResult(
            task_id="task-1",
            state="AWAITING_APPROVAL",
            phase1_state="WAITING",
            status="awaiting_approval",
            cursor=4,
        )

        self.assertEqual(result.to_data(), {
            "task_id": "task-1",
            "state": "AWAITING_APPROVAL",
            "phase1_state": "WAITING",
            "status": "awaiting_approval",
            "cursor": 4,
            "assistant_text": None,
            "approval": None,
            "clarification": None,
            "verification": None,
            "evidence_level": None,
            "projection": None,
            "latest_answer_event_ref": None,
            "conclusion_claims": [],
            "conclusion_validation": None,
            "completion_diagnostics": None,
            "single_loop": None,
        })

    def test_runtime_api_result_serializes_single_loop_projection(self) -> None:
        result = RuntimeTaskResult(
            task_id="task-1", state="FAILED", phase1_state="FAILED",
            status="failed", cursor=9,
            single_loop={
                "orchestration_mode": "SINGLE_LOOP",
                "binding": {"kind": "ACTIVE_CONTINUATION"},
                "turn_intent": {"intent": "MODIFY"},
                "open_obligations": [{"question_ref": "Q1", "status": "OPEN"}],
                "accepted_evidence": [{"evidence_id": "e1"}],
                "incomplete_boundary": {
                    "turn_id": "turn-1",
                    "reason": "completion_gaps_unrecoverable",
                    "gaps": [{"code": "NET_MUTATION_MISSING", "detail": "Mutation is still required."}],
                },
            },
        )

        data = result.to_data()

        self.assertEqual(data["single_loop"]["orchestration_mode"], "SINGLE_LOOP")
        self.assertEqual(data["single_loop"]["binding"]["kind"], "ACTIVE_CONTINUATION")
        self.assertEqual(data["single_loop"]["turn_intent"]["intent"], "MODIFY")
        self.assertEqual(data["single_loop"]["incomplete_boundary"]["gaps"][0]["code"], "NET_MUTATION_MISSING")

    def test_runtime_projects_single_loop_events_without_legacy_reader(self) -> None:
        task = TaskSnapshot.create("task-1", "repair the source", "/workspace")
        events = (
            RuntimeEvent("event-1", "task-1", 1, "task.created", {
                "goal": "repair the source",
                "single_loop": {
                    "schema_version": 1,
                    "orchestration_mode": "SINGLE_LOOP",
                    "root_goal": "repair the source",
                    "root_input_event_ref": "session-input:1",
                    "latest_input_event_ref": "session-input:2",
                    "binding_kind": "ACTIVE_CONTINUATION",
                },
            }),
            RuntimeEvent("event-2", "task-1", 2, "turn.intent_declared", {
                "schema_version": 1,
                "turn_id": "turn-1",
                "intent": "INVESTIGATE",
                "expected_effects": ["observe"],
                "dependencies": [],
                "summary": "inspect source",
                "source": "MODEL_DECLARED",
            }),
            RuntimeEvent(
                "event-3", "task-1", 3, "context.single_loop_shadow_projected",
                {"active_context": {"authoritative": {
                    "open_obligations": [{"question_ref": "Q1", "status": "OPEN"}],
                    "evidence_refs": [{"evidence_id": "e1", "kind": "source", "reference": "src"}],
                }}},
            ),
            RuntimeEvent("event-4", "task-1", 4, "turn.incomplete_boundary", {
                "turn_id": "turn-1",
                "reason": "completion_gaps_unrecoverable",
                "completion": {"gaps": [{
                    "code": "SOURCE_OBSERVATION_MISSING",
                    "detail": "A source dependency requires observation.",
                }]},
            }),
        )

        projection = _single_loop_projection(task, events)

        self.assertEqual(projection["orchestration_mode"], "SINGLE_LOOP")
        self.assertEqual(projection["binding"]["kind"], "ACTIVE_CONTINUATION")
        self.assertEqual(projection["turn_intent"]["intent"], "INVESTIGATE")
        self.assertEqual(projection["open_obligations"][0]["question_ref"], "Q1")
        self.assertEqual(projection["accepted_evidence"][0]["evidence_id"], "e1")
        self.assertEqual(
            projection["incomplete_boundary"]["gaps"][0]["code"],
            "SOURCE_OBSERVATION_MISSING",
        )
    def test_runtime_projects_completion_contract_fields(self) -> None:
        task = TaskSnapshot.create("task-1", "repair", "/workspace")
        events = (
            RuntimeEvent("event-1", "task-1", 1, "task.created", {
                "goal": "repair",
                "single_loop": {
                    "schema_version": 1,
                    "orchestration_mode": "SINGLE_LOOP",
                    "root_goal": "repair",
                    "root_input_event_ref": "input:1",
                    "latest_input_event_ref": "input:1",
                    "binding_kind": "NEW",
                },
            }),
            RuntimeEvent("event-2", "task-1", 2, "completion.shadow_validated", {
                "claim": {
                    "succeeded_effects": ["mutate"],
                    "net_mutation_ids": ["mutation-1"],
                },
                "gaps": [{"code": "MISSING_PROOF", "detail": "verify"}],
                "divergences": [{"code": "CLAIM_UNOBSERVED", "detail": "inspect"}],
            }),
        )

        projection = _single_loop_projection(task, events)

        self.assertEqual(projection["completion"], {
            "diagnostic": True,
            "outcome": "SUCCEEDED",
            "gaps": [{"code": "MISSING_PROOF", "detail": "verify"}],
            "divergences": [{"code": "CLAIM_UNOBSERVED", "detail": "inspect"}],
            "effects": ["mutate"],
            "committed_delta": ["mutation-1"],
            "waiting": None,
        })

    def test_runtime_keeps_shadow_diagnostics_at_legacy_incomplete_boundary(self) -> None:
        task = TaskSnapshot.create("task-1", "repair", "/workspace")
        events = (
            RuntimeEvent("event-1", "task-1", 1, "task.created", {
                "goal": "repair",
                "single_loop": {
                    "schema_version": 1,
                    "orchestration_mode": "SINGLE_LOOP",
                    "root_goal": "repair",
                    "root_input_event_ref": "input:1",
                    "latest_input_event_ref": "input:1",
                    "binding_kind": "NEW",
                },
            }),
            RuntimeEvent("event-2", "task-1", 2, "completion.shadow_validated", {
                "claim": {
                    "succeeded_effects": ["observe"],
                    "net_mutation_ids": [],
                },
                "gaps": [{"code": "SHADOW_GAP", "detail": "diagnostic"}],
                "divergences": [],
            }),
            RuntimeEvent("event-3", "task-1", 3, "turn.incomplete_boundary", {
                "turn_id": "turn-1",
                "reason": "completion_gaps_unrecoverable",
                "completion": {"gaps": [{
                    "code": "SOURCE_OBSERVATION_MISSING",
                    "detail": "Observe the source.",
                }]},
            }),
        )

        projection = _single_loop_projection(task, events)

        self.assertEqual(projection["completion"], {
            "diagnostic": True,
            "outcome": "INCOMPLETE",
            "gaps": [{"code": "SHADOW_GAP", "detail": "diagnostic"}],
            "divergences": [],
            "effects": ["observe"],
            "committed_delta": [],
            "waiting": {"reason": "completion_gaps_unrecoverable"},
        })
        self.assertNotIn("authority", projection["completion"])


if __name__ == "__main__":
    unittest.main()
