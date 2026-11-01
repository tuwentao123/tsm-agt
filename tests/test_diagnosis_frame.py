from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    DiagnosticFrame, DiagnosisHypothesis, DiagnosisMode, DiagnosisStatus,
    HypothesisStatus, ReplayStatus, TaskState, WorkingMemorySnapshot,
)
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ModelUsage, ProviderCapabilities, TextBlock, ToolCall,
    ToolCallBlock, ToolResultBlock,
)


def working_state() -> dict[str, object]:
    return {
        "goal": "Investigate the issue",
        "constraints": [],
        "facts": [],
        "decisions": [],
        "hypotheses": [],
        "open_questions": [],
        "plan": [],
        "completed_work": [],
        "remaining_work": [],
        "evidence": [],
    }


def open_repair_frame(reference: str = "event:1") -> DiagnosticFrame:
    return DiagnosticFrame(
        mode=DiagnosisMode.REPAIR,
        status=DiagnosisStatus.OPEN,
        symptom="Saving a preference appears to succeed but the next view shows the old value.",
        expected_behavior="The next view receives the saved preference.",
        reproduction="Save a preference, then navigate to the consumer view.",
        hypotheses=(DiagnosisHypothesis(
            "missing-consumer-input",
            "The consumer view does not receive the persisted preference.",
            HypothesisStatus.SUPPORTED,
            (reference,),
        ),),
        next_probe="Trace the saved value into the consumer view before editing.",
        repair_hypothesis_id="missing-consumer-input",
    )


class DiagnosisToolModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=16384)
    descriptor = AdapterDescriptor(
        "fixture.diagnosis-tool-model", "1.0", "ModelProviderPort", "1.0"
    )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        updated = next((
            block.result for message in reversed(request.messages)
            for block in message.content if isinstance(block, ToolResultBlock)
            and block.result.call_id == "diagnosis-update"
        ), None)
        if updated is not None:
            return ModelResponse(
                Message(
                    "diagnosis-final", MessageRole.ASSISTANT,
                    (TextBlock("The diagnosis record is now evidence-linked."),),
                ), FinishReason.STOP, ModelUsage(1, 1),
            )
        state = working_state()
        state["diagnosis"] = open_repair_frame().to_data()
        return ModelResponse(
            Message(
                "diagnosis-update", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "diagnosis-update", "core.working_memory_update", {
                        "expected_revision": 1,
                        "state": state,
                        "operation_id": "record-diagnosis",
                    },
                )),),
            ), FinishReason.TOOL_CALL, ModelUsage(1, 1),
        )


class DiagnosticFrameTest(unittest.IsolatedAsyncioTestCase):
    def test_round_trip_and_legacy_snapshot_hash_remain_valid(self) -> None:
        legacy = WorkingMemorySnapshot.from_update(
            task_id="task-legacy", revision=2, state=working_state(),
            source_event_sequences=(1,),
        )
        legacy_data = legacy.to_data()
        self.assertNotIn("diagnosis", legacy_data)
        self.assertEqual(WorkingMemorySnapshot.from_data(legacy_data), legacy)

        state = working_state()
        state["diagnosis"] = open_repair_frame().to_data()
        snapshot = WorkingMemorySnapshot.from_update(
            task_id="task-new", revision=2, state=state,
            source_event_sequences=(1,),
        )
        self.assertEqual(
            WorkingMemorySnapshot.from_data(snapshot.to_data()), snapshot
        )
        assert snapshot.diagnosis is not None
        self.assertEqual(snapshot.diagnosis.repair_hypothesis_id, "missing-consumer-input")

    def test_repaired_requires_supported_cause_and_original_replay(self) -> None:
        with self.assertRaisesRegex(ValueError, "passed replay"):
            DiagnosticFrame(
                mode=DiagnosisMode.REPAIR,
                status=DiagnosisStatus.REPAIRED,
                symptom="A user-visible action fails.",
                hypotheses=(DiagnosisHypothesis(
                    "cause", "The action is disconnected.",
                    HypothesisStatus.SUPPORTED, ("event:1",),
                ),),
                repair_hypothesis_id="cause",
            )
        with self.assertRaisesRegex(ValueError, "diagnose-only"):
            DiagnosticFrame(
                mode=DiagnosisMode.DIAGNOSE,
                status=DiagnosisStatus.REPAIRED,
                symptom="A user-visible action fails.",
                hypotheses=(DiagnosisHypothesis(
                    "cause", "The action is disconnected.",
                    HypothesisStatus.SUPPORTED, ("event:1",),
                ),),
                replay_status=ReplayStatus.PASSED,
                replay_evidence_refs=("event:1",),
            )

    async def test_kernel_rejects_untraceable_diagnosis_and_preserves_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
            )
            await application.registry.start_all()
            try:
                task = await application.kernel.create_task(
                    "Investigate", Path(directory), "task-diagnosis"
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    await application.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                invalid = working_state()
                invalid["diagnosis"] = open_repair_frame("event:999999").to_data()
                with self.assertRaisesRegex(ValueError, "diagnosis evidence does not exist"):
                    await application.kernel.update_working_memory(
                        task.task_id, 1, invalid, "invalid-diagnosis", "test"
                    )

                valid = working_state()
                valid["diagnosis"] = open_repair_frame().to_data()
                updated = await application.kernel.update_working_memory(
                    task.task_id, 1, valid, "valid-diagnosis", "test"
                )
                assert updated.diagnosis is not None
                self.assertEqual(updated.diagnosis.status, DiagnosisStatus.OPEN)

                progress = working_state()
                progress["facts"] = ["The save path was observed."]
                preserved = await application.kernel.update_working_memory(
                    task.task_id, 2, progress, "progress", "test"
                )
                self.assertEqual(preserved.diagnosis, updated.diagnosis)
            finally:
                await application.registry.stop_all()

    async def test_model_can_record_diagnosis_through_existing_working_memory_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = compose_fixture_application(
                model_adapter=DiagnosisToolModel(), tool_adapters=(),
                enable_working_memory=True,
            )
            await application.registry.start_all()
            try:
                specs = {
                    spec.name: spec for spec in await application.kernel.list_tools()
                }
                state_properties = specs["core.working_memory_update"].parameters[
                    "properties"
                ]["state"]["properties"]
                self.assertIn("diagnosis", state_properties)

                task = await application.kernel.create_task(
                    "Investigate", Path(directory), "task-diagnosis-tool"
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    await application.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                await application.kernel.run_agent_turn(
                    task.task_id, "Why does saving still not take effect?"
                )
                saved = await application.kernel.get_working_memory(task.task_id)
                assert saved.diagnosis is not None
                self.assertEqual(saved.diagnosis.mode, DiagnosisMode.REPAIR)
            finally:
                await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
