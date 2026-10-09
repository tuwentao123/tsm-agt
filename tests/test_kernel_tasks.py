from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import InMemoryRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    InvalidTaskTransition,
    SessionTaskRelation,
    TaskNotFound,
    TaskState,
)
from tsm_agt.core.configuration import canonical_hash
from tsm_agt.core.single_loop import OrchestrationMode
from tsm_agt.core.session_context import (
    SessionContextProjector,
    SessionConversationMessage,
    SessionConversationProjection,
    SessionTaskSummary,
    SessionWorkingState,
)
from tsm_agt.ports.evidence import (
    ContractAuthority,
    EvidenceRequirement,
    TaskContract,
)
from tsm_agt.ports import MessageRole
from tsm_agt.ports import RuntimeStorePort
from tsm_agt.core.turn import InvalidTurnState


class KernelTaskTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.application = compose_fixture_application()
        await self.application.registry.start_all()
        self.temp_dir = tempfile.TemporaryDirectory()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()
        await self.application.registry.stop_all()

    async def test_create_and_transition_persist_state_with_ordered_events(self) -> None:
        task = await self.application.kernel.create_task(
            "  fix failing tests  ", Path(self.temp_dir.name), task_id="task-1"
        )
        updated = await self.application.kernel.transition_task(
            task.task_id, TaskState.INTAKE, "begin intake"
        )

        self.assertEqual(task.goal, "fix failing tests")
        self.assertEqual(updated.state, TaskState.INTAKE)
        self.assertEqual(await self.application.kernel.get_task("task-1"), updated)

        store = self.application.registry.require(RuntimeStorePort)
        events = await store.read_events("task-1")
        self.assertEqual([event.sequence for event in events], [1, 2])
        self.assertEqual(
            [event.event_type for event in events],
            ["task.created", "task.state_changed"],
        )
        self.assertEqual(events[1].payload["previous_state"], "CREATED")
        self.assertEqual(events[1].payload["next_state"], "INTAKE")

    async def test_create_command_rejects_contract_content_change(self) -> None:
        first = TaskContract(
            objective="verify source",
            authority=ContractAuthority.USER,
            evidence_requirements=(EvidenceRequirement("source", "source.artifact"),),
        )
        second = TaskContract(
            objective="verify source",
            authority=ContractAuthority.USER,
            evidence_requirements=(EvidenceRequirement("source", "source.artifact", 2),),
        )
        await self.application.kernel.create_task(
            "answer",
            Path(self.temp_dir.name),
            command_id="create-contract-idempotency",
            evidence_contract=first,
        )

        with self.assertRaisesRegex(ValueError, "different Task input"):
            await self.application.kernel.create_task(
                "answer",
                Path(self.temp_dir.name),
                command_id="create-contract-idempotency",
                evidence_contract=second,
            )

    async def test_new_task_persists_single_loop_orchestration_metadata(self) -> None:
        task = await self.application.kernel.create_task(
            "fix failing tests", Path(self.temp_dir.name), task_id="task-legacy"
        )

        metadata = await self.application.kernel.get_task_orchestration_metadata(
            task.task_id
        )
        event = (await self.application.registry.require(
            RuntimeStorePort
        ).read_events(task.task_id))[0]

        self.assertEqual(
            metadata.orchestration_mode, OrchestrationMode.SINGLE_LOOP
        )
        self.assertEqual(metadata.root_goal, "fix failing tests")
        self.assertEqual(
            event.payload["single_loop"], metadata.to_data()
        )
        self.assertTrue(metadata.root_input_event_ref.startswith("session-event:"))
        self.assertEqual(
            metadata.root_input_event_ref, metadata.latest_input_event_ref
        )

    async def test_task_mode_is_fixed_across_composition_and_legacy_runner(self) -> None:
        store = InMemoryRuntimeStore()
        original = compose_fixture_application(
            store_adapter=store,
            orchestration_mode=OrchestrationMode.LEGACY,
        )
        await original.registry.start_all()
        try:
            task = await original.kernel.create_task(
                "answer", Path(self.temp_dir.name), task_id="task-fixed-mode"
            )
        finally:
            await original.registry.stop_all()

        reopened = compose_fixture_application(
            store_adapter=store,
            tool_adapters=(),
            orchestration_mode=OrchestrationMode.SINGLE_LOOP,
        )
        await reopened.registry.start_all()
        try:
            metadata = await reopened.kernel.get_task_orchestration_metadata(
                task.task_id
            )
            self.assertEqual(metadata.orchestration_mode, OrchestrationMode.LEGACY)
            for state in (
                TaskState.INTAKE,
                TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS,
                TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                await reopened.kernel.transition_task(task.task_id, state, state.value)
            await reopened.kernel.run_agent_turn(task.task_id, "answer")
            events = await store.read_events(task.task_id)
            self.assertEqual(
                [
                    event.payload["orchestration_mode"]
                    for event in events
                    if event.event_type == "turn.started"
                ],
                [OrchestrationMode.LEGACY.value],
            )
        finally:
            await reopened.registry.stop_all()

    async def test_single_loop_task_runs_the_single_loop(self) -> None:
        application = compose_fixture_application(
            tool_adapters=(),
            orchestration_mode=OrchestrationMode.SINGLE_LOOP,
        )
        await application.registry.start_all()
        try:
            task = await application.kernel.create_task(
                "answer", Path(self.temp_dir.name), task_id="task-single-loop"
            )
            for state in (
                TaskState.INTAKE,
                TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS,
                TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await application.kernel.transition_task(
                    task.task_id, state, state.value
                )
            result = await application.kernel.run_agent_turn(task.task_id, "answer")
            events = await application.registry.require(RuntimeStorePort).read_events(
                task.task_id
            )
            self.assertEqual(result.assistant_message.role.value, "assistant")
            self.assertEqual(
                next(event for event in events if event.event_type == "turn.started").payload[
                    "orchestration_mode"
                ],
                OrchestrationMode.SINGLE_LOOP.value,
            )
        finally:
            await application.registry.stop_all()

    async def test_illegal_transition_does_not_write_state_or_event(self) -> None:
        await self.application.kernel.create_task(
            "fix tests", Path(self.temp_dir.name), task_id="task-1"
        )

        with self.assertRaises(InvalidTaskTransition):
            await self.application.kernel.transition_task(
                "task-1", TaskState.EXECUTING, "skip required phases"
            )

        current = await self.application.kernel.get_task("task-1")
        store = self.application.registry.require(RuntimeStorePort)
        self.assertEqual(current.state, TaskState.CREATED)
        self.assertEqual(len(await store.read_events("task-1")), 1)

    async def test_unknown_task_is_reported(self) -> None:
        with self.assertRaises(TaskNotFound):
            await self.application.kernel.get_task("missing")

    async def test_invalid_input_is_rejected_before_persistence(self) -> None:
        with self.assertRaisesRegex(ValueError, "goal must not be empty"):
            await self.application.kernel.create_task(
                "   ", Path(self.temp_dir.name), task_id="task-1"
            )

        store = self.application.registry.require(RuntimeStorePort)
        self.assertIsNone(await store.load_task("task-1"))

    async def test_task_spec_planning_context_uses_follow_up_user_text(self) -> None:
        source = await self.application.kernel.create_task(
            "review the prior change", Path(self.temp_dir.name),
            task_id="task-source",
        )
        original_user_text = "检查敏感信息后，用中文提交并推送改动"
        follow_up = await self.application.kernel.create_task(
            "[session-follow-up]\nCurrent request:\n"
            + original_user_text,
            Path(self.temp_dir.name),
            task_id="task-follow-up",
            session_id=source.session_id,
            source_task_id=source.task_id,
            task_relation=SessionTaskRelation.FOLLOW_UP,
            original_user_text=original_user_text,
        )

        current_request, planning_context, session_revision, context_hash = (
            await self.application.kernel._task_spec_planning_context(follow_up)
        )

        self.assertNotEqual(follow_up.goal, original_user_text)
        self.assertEqual(current_request, original_user_text)
        self.assertGreater(session_revision, 0)
        self.assertEqual(context_hash, canonical_hash(planning_context))
        self.assertIn(
            {
                "message_id": "msg-session-input-task-follow-up",
                "role": "user",
                "text": original_user_text,
                "task_id": "task-follow-up",
                "turn_id": None,
                "source_event_sequence": 3,
                "authority": "AUTHORITATIVE",
            },
            planning_context["session"]["scoped_messages"],
        )
        self.assertEqual(
            planning_context["session"]["background_messages"], []
        )
        self.assertEqual(
            planning_context["related_task"]["authority"],
            "SCOPED_BACKGROUND",
        )

    def test_for_prompt_preserves_pinned_task_summaries(self) -> None:
        projector = SessionContextProjector(
            recent_visible_message_limit=2,
            detailed_task_summary_limit=1,
        )
        messages = (
            SessionConversationMessage(
                "m1", MessageRole.USER, "latest", "task-new", None, 1
            ),
        )
        task_summaries = (
            SessionTaskSummary(
                task_id="task-old",
                turn_id="turn-old",
                goal="old goal",
                recorded_task_state="SUCCEEDED",
            ),
            SessionTaskSummary(
                task_id="task-new",
                turn_id="turn-new",
                goal="new goal",
                recorded_task_state="SUCCEEDED",
            ),
        )
        projection = SessionConversationProjection(
            session_id="session-1",
            revision=1,
            messages=messages,
            working_state=SessionWorkingState(),
            resource_catalog=(),
            question_catalog=(),
            task_summaries=task_summaries,
            source_event_sequences=(1,),
            content_hash=canonical_hash({
                "schema_version": 1,
                "session_id": "session-1",
                "revision": 1,
                "messages": [item.source_data() for item in messages],
                "working_state": SessionWorkingState().to_data(),
                "resource_catalog": [],
                "question_catalog": [],
                "task_summaries": [
                    item.to_data() for item in task_summaries
                ],
                "source_event_sequences": [1],
            }),
        )

        prompt = projector.for_prompt(
            projection,
            pinned_task_ids=("task-old",),
        )

        summaries = prompt.prompt_data["recent_task_summaries"]
        self.assertEqual([item["task_id"] for item in summaries], [
            "task-new",
            "task-old",
        ])


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()


class LegacyTaskCompatibilityTest(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_task_can_roll_back_after_default_composition_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = InMemoryRuntimeStore()
            legacy = compose_fixture_application(
                store_adapter=store,
                orchestration_mode=OrchestrationMode.LEGACY,
            )
            await legacy.registry.start_all()
            try:
                task = await legacy.kernel.create_task(
                    "preserve rollback compatibility",
                    workspace,
                    task_id="task-legacy-rollback",
                )
                for state in (
                    TaskState.INTAKE,
                    TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS,
                    TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await legacy.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                mutation = await legacy.kernel.write_workspace_text(
                    task.task_id, "write", "legacy.txt", "legacy\n", None
                )
            finally:
                await legacy.registry.stop_all()

            reopened = compose_fixture_application(store_adapter=store)
            await reopened.registry.start_all()
            try:
                metadata = await reopened.kernel.get_task_orchestration_metadata(
                    task.task_id
                )
                rollback = await reopened.kernel.rollback_workspace_mutation(
                    task.task_id, "rollback", mutation.mutation_id
                )
            finally:
                await reopened.registry.stop_all()

        self.assertEqual(metadata.orchestration_mode, OrchestrationMode.LEGACY)
        self.assertEqual(rollback.reverts_mutation_id, mutation.mutation_id)
        self.assertFalse((workspace / "legacy.txt").exists())
