"""End-to-end Contract lifecycle coverage over the public SDK."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import AgentClarificationSuspended, TaskState
from tsm_agt.ports.evidence import (
    ContractAuthority,
    ContractPhase,
    EvidenceRequirement,
    TaskContract,
)
from tsm_agt.sdk import EngineeringAgentClient


class ContractLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def _client(self, root: Path) -> EngineeringAgentClient:
        application = compose_fixture_application(
            model_adapter=EchoModelProvider(), tool_adapters=()
        )
        return EngineeringAgentClient(
            root, application_factory=lambda: application, poll_interval=0.001
        )

    async def test_draft_does_not_gate_and_accept_enables_active_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            async with client:
                accepted = await client.submit_task("summarize", command_id="draft-run")
                completed = await client.wait_task(accepted.task_id, timeout=5)
                self.assertEqual(completed.state, TaskState.SUCCEEDED.value)
                draft = await client.get_contract(completed.task_id)
                assert draft is not None
                self.assertEqual(draft["authority"], ContractAuthority.MODEL_DRAFT.value)
                self.assertEqual(draft["phase"], ContractPhase.DRAFT.value)

                task = await client.create_task("inspect", command_id="draft-accept")
                pending = await client.get_contract(task.task_id)
                assert pending is not None
                drafted = await client.draft_contract(
                    task.task_id,
                    TaskContract(
                        objective="inspect with source evidence",
                        authority=ContractAuthority.MODEL_DRAFT,
                        evidence_requirements=(EvidenceRequirement("source", "source.artifact"),),
                    ),
                    expected_revision=pending["revision"],
                    command_id="draft-source-requirement",
                )
                accepted = await client.accept_contract(
                    task.task_id,
                    drafted.result["contract_id"],
                    expected_revision=drafted.result["revision"],
                    command_id="accept-once",
                )
                replay = await client.accept_contract(
                    task.task_id,
                    drafted.result["contract_id"],
                    expected_revision=drafted.result["revision"],
                    command_id="accept-once",
                )
                self.assertEqual(accepted.result["phase"], ContractPhase.ACTIVE.value)
                self.assertTrue(replay.replayed)
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    await client.application.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                gated = await client.application.kernel.run_agent_turn(task.task_id, "inspect")
                self.assertIsInstance(gated, AgentClarificationSuspended)
                with self.assertRaisesRegex(ValueError, "revision conflict"):
                    await client.update_contract(
                        task.task_id,
                        TaskContract(
                            objective="inspect with source evidence",
                            authority=ContractAuthority.USER_EXPLICIT,
                            evidence_requirements=(EvidenceRequirement("source", "source.artifact"),),
                        ),
                        expected_revision=drafted.result["revision"],
                        command_id="stale-update",
                    )

    async def test_clear_active_contract_recovers_blocked_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            active = TaskContract(
                objective="must prove source",
                authority=ContractAuthority.USER_EXPLICIT,
                evidence_requirements=(EvidenceRequirement("source", "source.artifact"),),
            )
            async with client:
                accepted = await client.submit_task(
                    "answer", command_id="blocked-contract", evidence_contract=active
                )
                blocked = await client.wait_task(accepted.task_id, timeout=5)
                self.assertEqual(blocked.status, "awaiting_user")
                current = await client.get_contract(blocked.task_id)
                assert current is not None
                cleared = await client.clear_contract(
                    blocked.task_id,
                    current["contract_id"],
                    expected_revision=current["revision"],
                    command_id="clear-blocking-contract",
                )
                self.assertEqual(cleared.result["phase"], ContractPhase.CLEARED.value)
                recovered = await client.resume_continuation(
                    blocked.task_id, command_id="resume-after-clear", text="continue"
                )
                self.assertEqual(recovered.result["state"], TaskState.SUCCEEDED.value)


if __name__ == "__main__":
    unittest.main()
