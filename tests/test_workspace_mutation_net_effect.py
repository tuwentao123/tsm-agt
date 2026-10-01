"""A rollback must not read as an active workspace change.

The mutation journal is append-only, so a change that was rolled back leaves two
records behind while the net effect on disk is nothing. ``_has_active_workspace_mutation``
is the predicate the delivery accounting reads to decide whether a Task actually
holds a mutation, and these tests are its only coverage.

Recovered from ``test_post_mutation_applicability.py``. That file's other two
halves are gone: its gap assertions drove ``Kernel._completion_readiness_gaps``,
which was removed, and its ``post_mutation_command`` criterion vocabulary is now
a replay-only tombstone.
"""

import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskState
from tsm_agt.core.kernel import _has_active_workspace_mutation


async def executing_task(app, root: Path, task_id: str):
    task = await app.kernel.create_task("review the renderer", root, task_id)
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await app.kernel.transition_task(task.task_id, state, state.value)
    return task


class WorkspaceMutationNetEffectTest(unittest.IsolatedAsyncioTestCase):
    async def test_fully_rolled_back_change_is_not_an_active_mutation(self) -> None:
        """The journal is append-only, so a rollback must not read as a change."""
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application()
            await app.registry.start_all()
            try:
                task = await executing_task(app, Path(directory), "reverted")
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
            finally:
                await app.registry.stop_all()

    async def test_reapplied_change_is_an_active_mutation(self) -> None:
        """Rolling back and changing again leaves a net change on disk."""
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application()
            await app.registry.start_all()
            try:
                task = await executing_task(app, Path(directory), "reapplied")
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
                # The journal tracks the net effect, not the record count.
                self.assertTrue(_has_active_workspace_mutation(current))
            finally:
                await app.registry.stop_all()
