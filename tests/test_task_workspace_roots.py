"""A Task keeps one primary root and any number of attached extra roots."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.core.task import LEGAL_TRANSITIONS, TaskSnapshot, TaskState


def _resolved(*paths: Path) -> tuple[str, ...]:
    return tuple(str(path.resolve()) for path in paths)


class TaskWorkspaceRootsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        base = Path(self._directory.name)
        self.primary = base / "app"
        self.extra_a = base / "shared"
        self.extra_b = base / "docs"
        for path in (self.primary, self.extra_a, self.extra_b):
            path.mkdir()

    def test_create_normalises_extra_roots_and_drops_the_primary(self) -> None:
        task = TaskSnapshot.create(
            "task-1", "goal", str(self.primary),
            workspace_roots=(
                str(self.primary), str(self.extra_a), str(self.extra_a),
                f" {self.extra_b} ", "",
            ),
        )
        self.assertEqual(task.workspace, str(self.primary))
        self.assertEqual(task.workspace_roots, _resolved(self.extra_a, self.extra_b))
        self.assertEqual(
            task.all_workspace_roots(),
            (self.primary.resolve(), self.extra_a.resolve(), self.extra_b.resolve()),
        )

    def test_to_data_round_trip_preserves_roots(self) -> None:
        task = TaskSnapshot.create(
            "task-1", "goal", str(self.primary),
            workspace_roots=(str(self.extra_a), str(self.extra_b)),
        )
        restored = TaskSnapshot.from_data(task.to_data())
        self.assertEqual(restored.workspace_roots, task.workspace_roots)
        self.assertEqual(restored.all_workspace_roots(), task.all_workspace_roots())

    def test_restoring_a_record_without_the_field_yields_no_extra_roots(self) -> None:
        task = TaskSnapshot.create("task-1", "goal", str(self.primary))
        data = task.to_data()
        data.pop("workspace_roots")
        self.assertEqual(TaskSnapshot.from_data(data).workspace_roots, ())

    def test_transition_and_grant_updates_keep_extra_roots(self) -> None:
        task = TaskSnapshot.create(
            "task-1", "goal", str(self.primary),
            workspace_roots=(str(self.extra_a),),
        )
        target = next(iter(LEGAL_TRANSITIONS[TaskState.CREATED]))
        moved = task.transition(target)
        self.assertEqual(moved.workspace_roots, _resolved(self.extra_a))
        self.assertEqual(moved.workspace, str(self.primary))

    def test_with_workspace_roots_attaches_and_detaches(self) -> None:
        task = TaskSnapshot.create("task-1", "goal", str(self.primary))
        attached = task.with_workspace_roots([str(self.extra_a), str(self.extra_b)])
        self.assertEqual(
            attached.workspace_roots, _resolved(self.extra_a, self.extra_b),
        )
        detached = attached.with_workspace_roots([str(self.extra_b)])
        self.assertEqual(detached.workspace_roots, _resolved(self.extra_b))
        self.assertEqual(detached.workspace, str(self.primary))


if __name__ == "__main__":
    unittest.main()
