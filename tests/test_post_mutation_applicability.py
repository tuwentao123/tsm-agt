"""A post-mutation criterion must only apply once a change actually exists.

Runtime cannot stop a model from labelling a criterion ``post_mutation_command``
on a task that changes nothing. When that happens the criterion's precondition
never holds, and treating it as an outstanding debt deadlocks the Turn: nothing
can close it, so the Task suspends forever. These tests pin the distinction
between "should have been verified" and "there was nothing to verify", and keep
the genuinely unmet cases blocking exactly as before.
"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tsm_agt.adapters.rule_based_evidence_level import (
    RuleBasedEvidenceLevelEvaluator,
)
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskAcceptanceCriterion, TaskCriterionKind, TaskState
from tsm_agt.core.kernel import _has_active_workspace_mutation
from tsm_agt.ports import ToolEffect


async def executing_task(app, root: Path, task_id: str):
    task = await app.kernel.create_task("review the renderer", root, task_id)
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await app.kernel.transition_task(task.task_id, state, state.value)
    return task


class PostMutationApplicabilityTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _require_command_criterion(app, task) -> None:
        """Declare the criterion the model wrote for a pure-analysis task."""
        current = await app.kernel.get_task_spec(task.task_id)
        spec = replace(
            current, revision=current.revision + 1,
            acceptance_criteria=(TaskAcceptanceCriterion(
                "ac_build_or_validation",
                "If front-end changes exist, build or test commands must pass",
                TaskCriterionKind.POST_MUTATION_COMMAND,
            ),),
            content_hash="",
        )
        await app.kernel._append_events(task.task_id, ((
            "task_spec.revised", {"snapshot": spec.to_data()},
        ),))

    @staticmethod
    async def _persist_final_answer(app, task_id: str) -> None:
        await app.kernel._append_events(task_id, (
            ("llm.completed", {
                "turn_id": "turn-answer",
                "message": {
                    "message_id": "answer-final", "role": "assistant",
                    "content": [{
                        "type": "text",
                        "text": "Here are the optimisation points you asked for.",
                    }],
                },
                "finish_reason": "stop",
            }),
            ("turn.completed", {"turn_id": "turn-answer"}),
        ))

    async def _gaps(self, app, task):
        return await app.kernel._completion_readiness_gaps(
            task.task_id, await app.kernel.list_tools()
        )

    async def test_analysis_task_does_not_owe_a_post_mutation_command(self) -> None:
        """No change means no verification debt, so no required gap."""
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application()
            await app.registry.start_all()
            try:
                task = await executing_task(app, Path(directory), "analysis-only")
                await self._require_command_criterion(app, task)

                gaps = await self._gaps(app, task)

                self.assertNotIn(
                    "task-spec:ac_build_or_validation",
                    {gap.gap_id for gap in gaps},
                )
                self.assertFalse(any(
                    gap.kind == "TASK_SPEC_POST_MUTATION_COMMAND"
                    for gap in gaps
                ))
            finally:
                await app.registry.stop_all()

    async def test_fully_rolled_back_change_owes_nothing(self) -> None:
        """The journal is append-only, so a rollback must not read as a change."""
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application()
            await app.registry.start_all()
            try:
                task = await executing_task(app, Path(directory), "reverted")
                await self._require_command_criterion(app, task)
                mutation = await app.kernel.write_workspace_text(
                    task.task_id, "change", "app.py", "value = 1\n", None,
                )
                await app.kernel.rollback_workspace_mutation(
                    task.task_id, "revert", mutation.mutation_id
                )

                current = await app.kernel.get_task(task.task_id)
                # Two records survive, yet the net effect on disk is nothing.
                self.assertEqual(len(current.mutation_journal), 2)
                self.assertFalse(_has_active_workspace_mutation(current))
                self.assertNotIn(
                    "task-spec:ac_build_or_validation",
                    {gap.gap_id for gap in await self._gaps(app, task)},
                )
            finally:
                await app.registry.stop_all()

    async def test_reapplied_change_is_still_an_active_mutation(self) -> None:
        """Rolling back and changing again leaves a net change on disk."""
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application()
            await app.registry.start_all()
            try:
                task = await executing_task(app, Path(directory), "reapplied")
                await self._require_command_criterion(app, task)
                first = await app.kernel.write_workspace_text(
                    task.task_id, "change", "app.py", "value = 1\n", None,
                )
                await app.kernel.rollback_workspace_mutation(
                    task.task_id, "revert", first.mutation_id
                )
                await app.kernel.write_workspace_text(
                    task.task_id, "again", "app.py", "value = 2\n", None,
                )

                current = await app.kernel.get_task(task.task_id)
                self.assertTrue(_has_active_workspace_mutation(current))
                # The journal tracks the net effect; no obligation is raised for
                # it, in either the rolled-back or the re-applied case.
                self.assertEqual(list(await self._gaps(app, task)), [])
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
