"""P1: NEEDS_REVIEW terminal, stall cap, and terminal authority.

See 《验收判定改造实施SPEC.md》P1. The central guarantee under test is INV-4:
an unclosable required gap must end the Task as NEEDS_REVIEW within a bounded
number of continuations, never loop forever in AWAITING_USER.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.rule_based_completion_readiness import (
    RuleBasedCompletionReadinessPolicy,
)
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AgentContinuationSuspended,
    AgentTurnCheckpoint,
    Phase1TaskState,
    TaskAcceptanceCriterion,
    TaskCriterionKind,
    TaskState,
)
from tsm_agt.ports import (
    CompletionReadinessState,
    FinishReason,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    RuntimeStorePort,
    RuntimeUnitOfWork,
    TextBlock,
)


class _FinalAnswerModel(EchoModelProvider):
    """Always proposes a final answer and never calls a tool."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            Message(
                "final-answer", MessageRole.ASSISTANT,
                (TextBlock("The task is complete."),),
            ),
            FinishReason.STOP, ModelUsage(1, 1),
        )


async def _executing_task(app, root: Path, task_id: str):
    task = await app.kernel.create_task("collect the required fact", root, task_id=task_id)
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await app.kernel.transition_task(task.task_id, state, state.value)
    return task


async def _set_acceptance_criteria(app, task, *criteria) -> None:
    current = await app.kernel.get_task_spec(task.task_id)
    spec = replace(
        current, revision=current.revision + 1,
        acceptance_criteria=tuple(criteria), content_hash="",
    )
    await app.kernel._append_events(task.task_id, ((
        "task_spec.revised", {"snapshot": spec.to_data()},
    ),))


class NeedsReviewLifecycleTest(unittest.TestCase):
    def test_needs_review_is_terminal(self):
        self.assertTrue(TaskState.NEEDS_REVIEW.is_terminal)

    def test_phase1_mapping(self):
        self.assertIs(
            TaskState.NEEDS_REVIEW.phase1_state,
            Phase1TaskState.NEEDS_REVIEW,
        )

    def test_needs_review_only_has_human_review_edges(self):
        from tsm_agt.core.task import LEGAL_TRANSITIONS
        self.assertEqual(
            LEGAL_TRANSITIONS[TaskState.NEEDS_REVIEW],
            frozenset({
                TaskState.EXECUTING, TaskState.CANCELLED, TaskState.SUCCEEDED,
            }),
        )
        self.assertIn(
            TaskState.NEEDS_REVIEW, LEGAL_TRANSITIONS[TaskState.AWAITING_USER],
        )
        self.assertIn(
            TaskState.NEEDS_REVIEW, LEGAL_TRANSITIONS[TaskState.EXECUTING],
        )


class TerminalAuthorityTest(unittest.IsolatedAsyncioTestCase):
    async def test_model_authority_cannot_cancel_or_succeed(self):
        from tsm_agt.core.kernel import TERMINAL_AUTHORITY
        self.assertNotIn("model", TERMINAL_AUTHORITY[TaskState.SUCCEEDED])
        self.assertNotIn("model", TERMINAL_AUTHORITY[TaskState.CANCELLED])

        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await _executing_task(app, Path(directory), "authority")
                with self.assertRaises(Exception):
                    await app.kernel.transition_task(
                        task.task_id, TaskState.CANCELLED,
                        "model tried to cancel", authority="model",
                    )
                cancelled = await app.kernel.transition_task(
                    task.task_id, TaskState.CANCELLED,
                    "user cancelled", authority="user",
                )
                self.assertIs(cancelled.state, TaskState.CANCELLED)
            finally:
                await app.registry.stop_all()


class StallTerminalTest(unittest.IsolatedAsyncioTestCase):
    async def _suspend_once(self, app, root: Path, task_id: str):
        task = await _executing_task(app, root, task_id)
        # A post-mutation criterion only applies once something changed, and a
        # tool-less model can never satisfy it: an unclosable required gap.
        await _set_acceptance_criteria(
            app, task,
            TaskAcceptanceCriterion(
                "command", "Run verification after mutation",
                TaskCriterionKind.POST_MUTATION_COMMAND,
            ),
        )
        await app.kernel.write_workspace_text(
            task.task_id, "change", "changed.txt", "after\n", None,
        )
        result = await app.kernel.run_agent_turn(
            task.task_id, "complete the task", max_model_calls=4, max_tool_calls=1,
        )
        self.assertIsInstance(result, AgentContinuationSuspended)
        current = await app.kernel.get_task(task.task_id)
        self.assertIs(current.state, TaskState.AWAITING_USER)
        return current

    async def test_finalize_needs_review_from_awaiting_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=_FinalAnswerModel(), tool_adapters=(),
                completion_readiness_policy_adapter=(
                    RuleBasedCompletionReadinessPolicy()
                ),
            )
            await app.registry.start_all()
            try:
                current = await self._suspend_once(
                    app, Path(directory), "finalize",
                )
                checkpoint = AgentTurnCheckpoint.from_data(
                    current.active_agent_checkpoint
                )
                await app.kernel._finalize_needs_review(
                    checkpoint, (), reason="test_reason", stalled=2,
                    message=checkpoint.messages[-1],
                )
                final = await app.kernel.get_task(current.task_id)
                self.assertIs(final.state, TaskState.NEEDS_REVIEW)
                self.assertTrue(final.state.is_terminal)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    current.task_id
                )
                self.assertTrue(any(
                    event.event_type == "completion.finalized_needs_review"
                    for event in events
                ))
            finally:
                await app.registry.stop_all()

    async def test_resume_refuses_a_stalled_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=_FinalAnswerModel(), tool_adapters=(),
                completion_readiness_policy_adapter=(
                    RuleBasedCompletionReadinessPolicy()
                ),
            )
            await app.registry.start_all()
            try:
                current = await self._suspend_once(app, Path(directory), "refuse")
                checkpoint = AgentTurnCheckpoint.from_data(
                    current.active_agent_checkpoint
                )
                cap = app.kernel._dependencies.completion_max_stalled_continuations
                stalled_state = replace(
                    CompletionReadinessState.from_data(
                        checkpoint.completion_readiness_state
                    ),
                    stalled_continuations=cap,
                )
                staged = current.with_agent_checkpoint(replace(
                    checkpoint,
                    completion_readiness_state=stalled_state.to_data(),
                ).to_data())
                stored = await app.kernel._require_stored_task(current.task_id)
                await app.kernel._dependencies.store.commit(RuntimeUnitOfWork(
                    current.task_id, stored.version, staged.to_data(), (),
                ))
                await app.kernel.resume_agent_continuation(
                    current.task_id, "continue", input_id="refuse-1",
                )
                final = await app.kernel.get_task(current.task_id)
                self.assertIs(final.state, TaskState.NEEDS_REVIEW)
            finally:
                await app.registry.stop_all()


class HumanReviewResolutionTest(unittest.IsolatedAsyncioTestCase):
    async def _needs_review(self, app, root: Path, task_id: str):
        current = await StallTerminalTest()._suspend_once(app, root, task_id)
        checkpoint = AgentTurnCheckpoint.from_data(
            current.active_agent_checkpoint
        )
        await app.kernel._finalize_needs_review(
            checkpoint, (), reason="test_reason", stalled=2,
            message=checkpoint.messages[-1],
        )
        final = await app.kernel.get_task(current.task_id)
        self.assertIs(final.state, TaskState.NEEDS_REVIEW)
        return final

    async def _app(self):
        app = compose_fixture_application(
            model_adapter=_FinalAnswerModel(), tool_adapters=(),
            completion_readiness_policy_adapter=(
                RuleBasedCompletionReadinessPolicy()
            ),
        )
        await app.registry.start_all()
        return app

    async def test_accept_marks_manual_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = await self._app()
            try:
                final = await self._needs_review(app, Path(directory), "review-accept")
                resolved = await app.kernel.resolve_needs_review(
                    final.task_id, "accept", reason="looks good",
                )
                self.assertIs(resolved.state, TaskState.SUCCEEDED)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    final.task_id
                )
                manual = [
                    event for event in events
                    if event.event_type == "verify.completed"
                    and event.payload.get("manual_verified")
                ]
                self.assertTrue(manual)
                self.assertTrue(any(
                    event.event_type == "task.review_resolved"
                    and event.payload["decision"] == "accept"
                    for event in events
                ))
            finally:
                await app.registry.stop_all()

    async def test_return_for_revision_requeues_unmet_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = await self._app()
            try:
                final = await self._needs_review(app, Path(directory), "review-return")
                resolved = await app.kernel.resolve_needs_review(
                    final.task_id, "return_for_revision", reason="run the check",
                )
                self.assertIs(resolved.state, TaskState.EXECUTING)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    final.task_id
                )
                steering = [
                    event for event in events
                    if event.event_type == "steering.queued"
                ]
                self.assertTrue(steering)
            finally:
                await app.registry.stop_all()

    async def test_cancel_from_needs_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = await self._app()
            try:
                final = await self._needs_review(app, Path(directory), "review-cancel")
                resolved = await app.kernel.resolve_needs_review(
                    final.task_id, "cancel", reason="abandon",
                )
                self.assertIs(resolved.state, TaskState.CANCELLED)
            finally:
                await app.registry.stop_all()

    async def test_review_rejects_a_task_that_is_not_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = await self._app()
            try:
                task = await _executing_task(app, Path(directory), "review-wrong-state")
                with self.assertRaises(Exception):
                    await app.kernel.resolve_needs_review(
                        task.task_id, "accept", reason="nope",
                    )
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()