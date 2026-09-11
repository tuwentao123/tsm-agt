from __future__ import annotations

import unittest

from tsm_agt.adapters.rule_based_checkpoint_compatibility import (
    RuleBasedCheckpointCompatibilityPolicy,
)
from tsm_agt.ports import (
    AdapterContext, CheckpointCompatibilityAction,
    CheckpointCompatibilityProbe,
)


class CheckpointCompatibilityPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.policy = RuleBasedCheckpointCompatibilityPolicy()
        await self.policy.start(AdapterContext({}, lambda _kind, _data: None))

    async def _evaluate(self, **changes):
        return await self.policy.evaluate(CheckpointCompatibilityProbe(**changes))

    async def test_unchanged_checkpoint_resumes_exactly(self) -> None:
        decision = await self._evaluate()
        self.assertEqual(
            decision.action, CheckpointCompatibilityAction.EXACT_RESUME
        )

    async def test_zero_side_effect_runtime_upgrade_rebases(self) -> None:
        decision = await self._evaluate(differences=(
            "adapter_lock_hash", "prompt_manifest_hash", "policy_hash",
            "toolset_hash", "model_configuration",
        ))
        self.assertEqual(
            decision.action, CheckpointCompatibilityAction.REBASE_REQUIRED
        )
        self.assertEqual(decision.reason_code, "safe_runtime_upgrade_rebase")

    async def test_identity_and_workspace_changes_are_blocked(self) -> None:
        for difference in (
            "task_id", "session_id", "local_subject",
            "workspace_fingerprint", "working_memory_hash",
        ):
            with self.subTest(difference=difference):
                decision = await self._evaluate(differences=(difference,))
                self.assertEqual(
                    decision.action, CheckpointCompatibilityAction.BLOCKED
                )

    async def test_committed_pending_tool_allows_projection_reconciliation(self) -> None:
        decision = await self._evaluate(
            differences=("evidence_question_state",),
            reconcilable_differences=("evidence_question_state",),
            committed_pending_tool_count=1, pending_tool_call_count=1,
        )
        self.assertEqual(
            decision.action, CheckpointCompatibilityAction.RECONCILE_REQUIRED
        )

    async def test_reconciliation_can_rebind_safe_runtime_upgrade(self) -> None:
        decision = await self._evaluate(
            differences=("evidence_question_state", "policy_hash"),
            reconcilable_differences=("evidence_question_state",),
            committed_pending_tool_count=1, pending_tool_call_count=1,
        )
        self.assertEqual(
            decision.action, CheckpointCompatibilityAction.RECONCILE_REQUIRED
        )

    async def test_unproven_evidence_difference_remains_blocked(self) -> None:
        decision = await self._evaluate(
            differences=("evidence_question_state",),
        )
        self.assertEqual(decision.action, CheckpointCompatibilityAction.BLOCKED)

    async def test_unknown_or_non_idempotent_execution_is_blocked(self) -> None:
        for changes in (
            {"unknown_outcome_count": 1},
            {"running_non_idempotent_count": 1},
        ):
            with self.subTest(changes=changes):
                decision = await self._evaluate(**changes)
                self.assertEqual(
                    decision.action, CheckpointCompatibilityAction.BLOCKED
                )

    async def test_runtime_upgrade_with_execution_state_requires_validation(self) -> None:
        cases = (
            {"pending_tool_call_count": 1},
            {"tool_execution_count": 1},
            {"mutation_count": 1},
            {"background_process_count": 1},
        )
        for execution_state in cases:
            with self.subTest(execution_state=execution_state):
                decision = await self._evaluate(
                    differences=("adapter_lock_hash",), **execution_state
                )
                self.assertEqual(
                    decision.action,
                    CheckpointCompatibilityAction.REQUIRES_VALIDATION,
                )


if __name__ == "__main__":
    unittest.main()
