from __future__ import annotations

import unittest

from tsm_agt.core.single_loop import (
    ActiveContextProjection,
    CompletionClaim,
    IntentDependency,
    IntentDependencyKind,
    OrchestrationMode,
    TaskBindingDecision,
    TaskBindingKind,
    TaskOrchestrationMetadata,
    TurnIntent,
    TurnIntentKind,
)
from tsm_agt.core.single_loop.task_projection import TaskOrchestrationProjector
from tsm_agt.ports import RuntimeEvent, ToolEffect


class TaskOrchestrationMetadataTest(unittest.TestCase):
    def test_legacy_task_created_payload_uses_its_original_goal(self) -> None:
        metadata = TaskOrchestrationMetadata.from_task_created_payload(
            {"goal": "legacy goal"}, fallback_goal="legacy goal"
        )

        self.assertEqual(metadata.orchestration_mode, OrchestrationMode.LEGACY)
        self.assertEqual(metadata.root_goal, "legacy goal")
        self.assertEqual(metadata.binding_kind, TaskBindingKind.NEW)

    def test_single_loop_metadata_round_trips(self) -> None:
        metadata = TaskOrchestrationMetadata(
            schema_version=1,
            orchestration_mode=OrchestrationMode.SINGLE_LOOP,
            root_goal="inspect the source and fix it",
            root_input_event_ref="session-input:1",
            latest_input_event_ref="session-input:2",
            binding_kind=TaskBindingKind.ACTIVE_CONTINUATION,
        )

        self.assertEqual(
            TaskOrchestrationMetadata.from_data(metadata.to_data()), metadata
        )
    def test_projector_reads_new_metadata_and_legacy_goal(self) -> None:
        legacy = TaskOrchestrationProjector.project(
            "task-legacy", "fallback", (
                RuntimeEvent("evt-1", "task-legacy", 1, "task.created", {
                    "goal": "legacy root goal",
                }),
            ),
        )
        current = TaskOrchestrationProjector.project(
            "task-current", "fallback", (
                RuntimeEvent("evt-2", "task-current", 1, "task.created", {
                    "goal": "ignored fallback",
                    "single_loop": {
                        "schema_version": 1,
                        "orchestration_mode": "SINGLE_LOOP",
                        "root_goal": "current root goal",
                        "root_input_event_ref": "session-input:1",
                        "latest_input_event_ref": "session-input:1",
                        "binding_kind": "NEW",
                    },
                }),
            ),
        )

        self.assertEqual(legacy.orchestration_mode, OrchestrationMode.LEGACY)
        self.assertEqual(legacy.root_goal, "legacy root goal")
        self.assertEqual(current.orchestration_mode, OrchestrationMode.SINGLE_LOOP)
        self.assertEqual(current.root_goal, "current root goal")


class TurnIntentTest(unittest.TestCase):
    def test_runtime_inspection_requires_observe_and_runtime_dependency(self) -> None:
        intent = TurnIntent(
            schema_version=1,
            turn_id="turn-1",
            kind=TurnIntentKind.INSPECT_RUNTIME,
            expected_effects=(ToolEffect.OBSERVE,),
            dependencies=(IntentDependency(
                IntentDependencyKind.RUNTIME_TASK, "task-123"
            ),),
            summary="inspect the requested task",
        )

        self.assertEqual(
            TurnIntent.from_data(intent.to_data()), intent
        )

    def test_modify_requires_mutate(self) -> None:
        with self.assertRaisesRegex(ValueError, "must declare mutate"):
            TurnIntent(
                schema_version=1,
                turn_id="turn-1",
                kind=TurnIntentKind.MODIFY,
            )


class SingleLoopContractTypeTest(unittest.TestCase):
    def test_binding_choice_can_omit_task_id(self) -> None:
        decision = TaskBindingDecision(
            schema_version=1,
            task_id=None,
            binding_kind=TaskBindingKind.CHOICE_REQUIRED,
            current_input_event_ref="session-input:1",
        )
        self.assertIsNone(decision.task_id)

    def test_active_context_keeps_authority_separate(self) -> None:
        projection = ActiveContextProjection(
            schema_version=1,
            task={"task_id": "task-1", "root_goal": "fix the source"},
            turn={"turn_id": "turn-1"},
            authoritative={"open_obligations": ["observe"]},
            non_authoritative={"assistant_prior_text": ["guess"]},
        )

        data = projection.to_data()
        self.assertEqual(data["authority"]["task"], "AUTHORITATIVE")
        self.assertEqual(
            data["authority"]["non_authoritative"], "NON_AUTHORITATIVE"
        )

    def test_completion_claim_rejects_duplicate_effects(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be unique"):
            CompletionClaim(
                schema_version=1,
                turn_id="turn-1",
                assistant_message_id="assistant-1",
                claimed_effects=(ToolEffect.OBSERVE, ToolEffect.OBSERVE),
            )
