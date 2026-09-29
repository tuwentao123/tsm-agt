"""AG-16: one verdict -> terminal-state mapping shared by every entry point.

Before this, the CLI mapped every non-passed verification to FAILED while the
SDK already mapped BLOCKED to NEEDS_REVIEW, so the same verdict was reported as
"failed" on one surface and "needs review" on another.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.cli import _exit_code_for
from tsm_agt.core import (
    TERMINAL_STATE_BY_VERDICT,
    AcceptanceStatus,
    InvalidTurnState,
    TaskState,
    terminal_state_for,
)

_PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "tsm_agt"


class VerdictMappingTest(unittest.TestCase):
    def test_every_verdict_has_one_mapping(self) -> None:
        # Totality: a new AcceptanceStatus member must fail this test rather
        # than silently falling through to an ad-hoc default.
        self.assertEqual(
            set(TERMINAL_STATE_BY_VERDICT), set(AcceptanceStatus)
        )
        self.assertIs(
            terminal_state_for(AcceptanceStatus.PASSED), TaskState.SUCCEEDED
        )
        self.assertIs(
            terminal_state_for(AcceptanceStatus.BLOCKED), TaskState.NEEDS_REVIEW
        )
        self.assertIs(
            terminal_state_for(AcceptanceStatus.FAILED), TaskState.FAILED
        )
        self.assertIs(
            terminal_state_for(AcceptanceStatus.NOT_APPLICABLE),
            TaskState.NEEDS_REVIEW,
        )

    def test_blocked_is_never_a_failure(self) -> None:
        """The bug being fixed: BLOCKED must not be reported as FAILED."""
        self.assertIsNot(
            terminal_state_for(AcceptanceStatus.BLOCKED), TaskState.FAILED
        )

    def test_cli_exit_codes_distinguish_review_from_failure(self) -> None:
        self.assertEqual(_exit_code_for(TaskState.SUCCEEDED), 0)
        self.assertEqual(_exit_code_for(TaskState.FAILED), 1)
        self.assertEqual(_exit_code_for(TaskState.NEEDS_REVIEW), 2)
        self.assertNotEqual(
            _exit_code_for(TaskState.NEEDS_REVIEW),
            _exit_code_for(TaskState.FAILED),
        )

    def test_entry_points_delegate_to_the_shared_finalizer(self) -> None:
        """Guards against a client re-introducing its own verdict mapping."""
        cli = (_PACKAGE_ROOT / "cli.py").read_text(encoding="utf-8")
        sdk = (_PACKAGE_ROOT / "sdk" / "runtime.py").read_text(encoding="utf-8")
        # CLI has two legacy-gate verification sites (standalone + interactive).
        self.assertGreaterEqual(cli.count("finalize_acceptance("), 2)
        self.assertIn("finalize_acceptance(", sdk)
        # The exact old divergence must not come back.
        self.assertNotIn('f"trusted verifier {verification.status.value}"', cli)


class FinalizeAcceptanceTest(unittest.IsolatedAsyncioTestCase):
    async def _verifying_task(self, task_id: str):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        app = compose_fixture_application()
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        kernel = app.kernel
        root = Path(temporary.name)
        task = await kernel.create_task("verify me", root, task_id=task_id)
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING, TaskState.VERIFYING,
        ):
            task = await kernel.transition_task(
                task.task_id, state, state.value
            )
        return kernel, task

    async def test_passed_reaches_succeeded_through_finalizing(self) -> None:
        kernel, task = await self._verifying_task("task-passed")

        finalized = await kernel.finalize_acceptance(
            task.task_id, AcceptanceStatus.PASSED
        )

        self.assertIs(finalized.state, TaskState.SUCCEEDED)

    async def test_blocked_reaches_needs_review_not_failed(self) -> None:
        kernel, task = await self._verifying_task("task-blocked")

        finalized = await kernel.finalize_acceptance(
            task.task_id, AcceptanceStatus.BLOCKED
        )

        self.assertIs(finalized.state, TaskState.NEEDS_REVIEW)

    async def test_failed_reaches_failed(self) -> None:
        kernel, task = await self._verifying_task("task-failed")

        finalized = await kernel.finalize_acceptance(
            task.task_id, AcceptanceStatus.FAILED
        )

        self.assertIs(finalized.state, TaskState.FAILED)

    async def test_already_terminal_is_rejected(self) -> None:
        kernel, task = await self._verifying_task("task-twice")
        await kernel.finalize_acceptance(task.task_id, AcceptanceStatus.FAILED)

        with self.assertRaises(InvalidTurnState):
            await kernel.finalize_acceptance(
                task.task_id, AcceptanceStatus.PASSED
            )

    async def test_finalizer_and_mapping_agree_for_every_verdict(self) -> None:
        for index, status in enumerate(AcceptanceStatus):
            with self.subTest(status=status.value):
                kernel, task = await self._verifying_task(
                    f"task-agree-{index}"
                )
                finalized = await kernel.finalize_acceptance(
                    task.task_id, status
                )
                self.assertIs(finalized.state, terminal_state_for(status))


if __name__ == "__main__":
    unittest.main()
