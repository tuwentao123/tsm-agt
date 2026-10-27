"""A running Task state is a ledger claim; the turn is process-local.

Two shapes of the mismatch are locked here, both from
``task-c2d21bce8ea74f0bbcac07aba4aefedf``: the process that owned the turn died
(or its model call never returned), the Task stayed ``EXECUTING``, and every
later Session message was steered into a queue nothing drained -- no error, no
progress, no output.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import (
    compose_fixture_application, compose_local_flow_query_application,
)
from tsm_agt.core import (
    RuntimeInputIntent,
    SessionContinuationDecision, SessionContinuationMode, SessionResumeSafety,
    TaskState,
)
from tsm_agt.ports import AdapterDescriptor, HealthState, HealthStatus
from tsm_agt.sdk import EngineeringAgentClient


EXECUTION_PATH = (
    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
)


async def advance(application, task_id: str, *states: TaskState):
    task = await application.kernel.get_task(task_id)
    for state in states:
        task = await application.kernel.transition_task(
            task_id, state, state.value,
        )
    return task


class FixtureResolver:
    descriptor = AdapterDescriptor(
        "fixture.orphan-routing", "1.0", "SessionInputResolverPort", "1.0"
    )

    async def start(self, _context):
        pass

    async def stop(self, _deadline):
        pass

    async def health(self):
        return HealthStatus(HealthState.HEALTHY)

    async def resolve_session_input(self, _text, _context):
        return {
            "disposition": "CREATE_TASK", "relation": "INDEPENDENT",
            "source_task_id": None, "input_grounding": "SELF_CONTAINED",
            "confidence": 0.96, "reason_code": "fixture",
            "clarification": None, "candidate_task_ids": [],
        }


class OrphanedRunningReconcileTest(unittest.IsolatedAsyncioTestCase):
    async def test_running_task_is_settled_and_stays_resumable(self) -> None:
        application = compose_fixture_application()
        await application.registry.start_all()
        self.addAsyncCleanup(application.registry.stop_all)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = await application.kernel.create_task("goal", root)
            await advance(application, task.task_id, *EXECUTION_PATH)

            settled = await application.kernel.reconcile_orphaned_running_tasks()
            self.assertEqual(settled, (task.task_id,))

            current = await application.kernel.get_task(task.task_id)
            self.assertIs(current.state, TaskState.INTERRUPTED)
            events = await application.kernel.dependencies.store.read_events(
                task.task_id
            )
            interrupted = [
                event for event in events
                if event.event_type == "turn.interrupted"
            ]
            self.assertEqual(len(interrupted), 1)
            self.assertIn(
                "orphaned task settled on startup",
                interrupted[0].payload["reason"],
            )
            # Idempotent: a settled Task is no longer in a running state.
            self.assertEqual(
                await application.kernel.reconcile_orphaned_running_tasks(), (),
            )

    async def test_waiting_task_is_left_alone(self) -> None:
        application = compose_fixture_application()
        await application.registry.start_all()
        self.addAsyncCleanup(application.registry.stop_all)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = await application.kernel.create_task("goal", root)
            await advance(
                application, task.task_id,
                *EXECUTION_PATH, TaskState.AWAITING_USER,
            )

            self.assertEqual(
                await application.kernel.reconcile_orphaned_running_tasks(), (),
            )
            current = await application.kernel.get_task(task.task_id)
            self.assertIs(current.state, TaskState.AWAITING_USER)

    async def test_verifying_task_is_settled_not_left_blocking(self) -> None:
        """The fb9d5b incident: a restart killed the verifier mid-judge."""

        application = compose_fixture_application()
        await application.registry.start_all()
        self.addAsyncCleanup(application.registry.stop_all)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = await application.kernel.create_task("goal", root)
            await advance(
                application, task.task_id,
                *EXECUTION_PATH, TaskState.VERIFYING,
            )

            settled = await application.kernel.reconcile_orphaned_running_tasks()
            self.assertEqual(settled, (task.task_id,))
            current = await application.kernel.get_task(task.task_id)
            # Verification is re-derived on the next run, so the recoverable
            # interruption is the honest outcome -- not a stuck VERIFYING that
            # answers every later message with "busy".
            self.assertIs(current.state, TaskState.INTERRUPTED)

    async def test_finalizing_task_with_passed_verification_succeeds(self) -> None:
        application = compose_fixture_application()
        await application.registry.start_all()
        self.addAsyncCleanup(application.registry.stop_all)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = await application.kernel.create_task("goal", root)
            await advance(
                application, task.task_id,
                *EXECUTION_PATH, TaskState.VERIFYING,
            )
            # FINALIZING is only reachable behind a trusted passed verification.
            await application.kernel._append_events(task.task_id, ((
                "verify.completed", {"status": "passed"},
            ),))
            await advance(application, task.task_id, TaskState.FINALIZING)

            settled = await application.kernel.reconcile_orphaned_running_tasks()
            self.assertEqual(settled, (task.task_id,))
            current = await application.kernel.get_task(task.task_id)
            self.assertIs(current.state, TaskState.SUCCEEDED)

    async def test_never_started_task_is_cancelled(self) -> None:
        application = compose_fixture_application()
        await application.registry.start_all()
        self.addAsyncCleanup(application.registry.stop_all)

        with tempfile.TemporaryDirectory() as directory:
            task = await application.kernel.create_task(
                "goal", Path(directory),
            )
            self.assertIs(task.state, TaskState.CREATED)

            settled = await application.kernel.reconcile_orphaned_running_tasks()
            self.assertEqual(settled, (task.task_id,))
            current = await application.kernel.get_task(task.task_id)
            self.assertIs(current.state, TaskState.CANCELLED)

    def test_read_only_query_application_never_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = compose_local_flow_query_application(Path(directory))
            self.assertFalse(application.reconcile_on_start)


class OrphanedActiveTaskInputTest(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, root: Path) -> EngineeringAgentClient:
        app = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(root / ".agent" / "runtime.db"),
            model_adapter=EchoModelProvider(), tool_adapters=(),
            session_input_resolver_adapter=FixtureResolver(),
        )
        client = EngineeringAgentClient(root, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        return client

    async def make_active_task(
        self, client: EngineeringAgentClient, root: Path,
    ):
        kernel = client.application.kernel
        session = await kernel.create_session("orphan session")
        task = await kernel.create_task(
            "original goal", root, session_id=session.session_id,
        )
        await advance(client.application, task.task_id, *EXECUTION_PATH)
        session = await kernel.get_session(session.session_id)
        self.assertEqual(session.active_task_id, task.task_id)
        return session, task

    async def test_orphaned_running_task_is_settled_before_routing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = await self.make_client(root)
            session, task = await self.make_active_task(client, root)

            result = await client.submit_user_input(
                session.session_id, "看下今天的nba新闻",
                input_id="orphan-input-1",
            )

            settled = await client.application.kernel.get_task(task.task_id)
            self.assertIs(settled.state, TaskState.INTERRUPTED)
            # The message must not be reported as absorbed by a Task nothing is
            # executing: settling it lets the ordinary routing decide instead.
            self.assertNotEqual(result.result.get("kind"), "steered")
            receipt = await client.get_task_result(task.task_id)
            self.assertEqual(receipt.pending_steering, ())

    async def test_queued_steering_is_visible_as_a_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = await self.make_client(root)
            session, task = await self.make_active_task(client, root)

            runner = asyncio.create_task(asyncio.sleep(30))

            async def cancel_runner() -> None:
                runner.cancel()

            self.addAsyncCleanup(cancel_runner)
            client._run_tasks[task.task_id] = runner

            await client.submit_user_input(
                session.session_id, "排队中的指令", input_id="receipt-1",
                explicit_intent=RuntimeInputIntent.STEER,
            )

            receipt = await client.get_task_result(task.task_id)
            self.assertEqual(
                [item["text"] for item in receipt.pending_steering],
                ["排队中的指令"],
            )
            self.assertEqual(
                [item["text"] for item in receipt.to_data()["pending_steering"]],
                ["排队中的指令"],
            )

            # A queued message expires with the Task it was queued into: once it
            # can no longer run, no safe point will drain the queue.
            await client.cancel(
                task.task_id, command_id="cancel-receipt",
                reason="receipt expiry test",
            )
            expired = await client.get_task_result(task.task_id)
            self.assertEqual(expired.pending_steering, ())

    async def test_running_task_with_a_live_turn_restarts_same_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = await self.make_client(root)
            session, task = await self.make_active_task(client, root)

            # The liveness registry is the in-process runner map, so a stand-in
            # that is simply not done() is exactly a live turn for this guard.
            runner = asyncio.create_task(asyncio.sleep(30))

            async def cancel_runner() -> None:
                runner.cancel()

            self.addAsyncCleanup(cancel_runner)
            client._run_tasks[task.task_id] = runner

            result = await client.submit_user_input(
                session.session_id, "补充一句", input_id="live-input-1",
            )

            self.assertEqual(result.result.get("kind"), "continuation")
            self.assertEqual(
                result.result.get("relation"), "INTERRUPT_AND_REDIRECT"
            )
            self.assertEqual(result.result.get("routed_task_id"), task.task_id)


    async def test_task_being_started_is_not_settled_as_an_orphan(self) -> None:
        """A Task with a submission in flight is not an orphan.

        ``submit_task`` points the Session at the new Task before it publishes
        the runner, and it holds ``_submit_lock`` across that span. To another
        coroutine the window looks exactly like a Task whose process died
        mid-flight, so the liveness read has to happen under the same lock --
        otherwise the message cancels the work it came to join.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = await self.make_client(root)
            kernel = client.application.kernel
            session = await kernel.create_session("starting session")
            task = await kernel.create_task(
                "original goal", root, session_id=session.session_id,
            )
            self.assertIs(task.state, TaskState.CREATED)

            runner = asyncio.create_task(asyncio.sleep(30))

            async def cancel_runner() -> None:
                runner.cancel()

            self.addAsyncCleanup(cancel_runner)

            async with client._submit_lock:
                pending = asyncio.create_task(client.submit_user_input(
                    session.session_id, "补充一句", input_id="starting-1",
                ))
                # Let routing reach the liveness read. Whether it gets there
                # before or after the runner is published does not matter: the
                # lock defers the decision either way.
                for _ in range(50):
                    await asyncio.sleep(0)
                self.assertIn(task.task_id, {
                    item.task_id
                    for item in await kernel.list_session_tasks(
                        session.session_id
                    )
                    if item.state is TaskState.CREATED
                })
                client._run_tasks[task.task_id] = runner

            result = await pending
            # The Task was being started, so the answer is the ordinary
            # "still working" one -- not a settlement that cancels it.
            self.assertEqual(result.result.get("kind"), "busy")
            self.assertIs(
                (await kernel.get_task(task.task_id)).state, TaskState.CREATED,
            )

    async def test_message_to_orphaned_verifying_task_is_not_busy(self) -> None:
        """The fb9d5b shape on the input path, without waiting for a restart."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = await self.make_client(root)
            session, task = await self.make_active_task(client, root)
            await advance(client.application, task.task_id, TaskState.VERIFYING)

            result = await client.submit_user_input(
                session.session_id, "还是不对", input_id="verifying-input-1",
            )

            self.assertNotEqual(result.result.get("kind"), "busy")
            settled = await client.application.kernel.get_task(task.task_id)
            self.assertIs(settled.state, TaskState.INTERRUPTED)

    async def test_live_transitional_task_still_answers_busy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = await self.make_client(root)
            session, task = await self.make_active_task(client, root)
            await advance(client.application, task.task_id, TaskState.VERIFYING)

            runner = asyncio.create_task(asyncio.sleep(30))

            async def cancel_runner() -> None:
                runner.cancel()

            self.addAsyncCleanup(cancel_runner)
            client._run_tasks[task.task_id] = runner

            result = await client.submit_user_input(
                session.session_id, "补充", input_id="verifying-live-1",
            )

            self.assertEqual(result.result.get("kind"), "busy")
            current = await client.application.kernel.get_task(task.task_id)
            self.assertIs(current.state, TaskState.VERIFYING)

    async def test_restart_settles_the_task_and_accepts_new_input(self) -> None:
        """The incident's exact shape: restart, then a message that must land."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = await self.make_client(root)
            session, task = await self.make_active_task(first, root)
            await first.close()

            # A new process over the same ledger. Starting it reconciles the
            # Task the previous process left claiming to be running.
            second = await self.make_client(root)
            settled = await second.application.kernel.get_task(task.task_id)
            self.assertIs(settled.state, TaskState.INTERRUPTED)

            result = await second.submit_user_input(
                session.session_id, "看下今天的nba新闻",
                input_id="restart-input-1",
            )
            self.assertNotEqual(result.result.get("kind"), "steered")
            tasks = await second.application.kernel.list_session_tasks(
                session.session_id
            )
            self.assertEqual(result.result["task"]["task_id"], task.task_id)
            self.assertEqual(len(tasks), 1)
            await second.wait_task(task.task_id, timeout=5)
            self.assertNotEqual((await second.application.kernel.get_task(task.task_id)).state, TaskState.INTERRUPTED)


class OrphanedPendingActionInputTest(unittest.IsolatedAsyncioTestCase):
    """A Task parked on a pending action nothing can produce must not wedge.

    ``completion_exhausted_with_required_gaps`` and ``unmet_acceptance_criteria``
    were written by machinery removed with the proof layer: no code path emits
    them any more, so no reply can move such a Task forward. Refusing new input
    on one (rule 3 of ``submit_user_input``) stranded the Session permanently --
    the user's report was exactly this, on
    ``task-8d1ec232a0e54910852a029dcf11b03f`` (``AWAITING_USER``,
    ``reason=completion_exhausted_with_required_gaps``). 18 of the 35 parked
    Tasks in the real ledger carry one of these two reasons.

    The decision is driven through the real routing entry point with the parked
    Task supplied directly, because fabricating a persisted checkpoint would mean
    forging its integrity hash, and a real suspension can only ever produce the
    *live* reasons -- which is the case these tests must contrast against.
    """

    async def _client_with_waiting_task(
        self, root: Path, *, pending_reason: str | None, state=TaskState.AWAITING_USER,
    ):
        app = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(root / ".agent" / "runtime.db"),
            model_adapter=EchoModelProvider(), tool_adapters=(),
            session_input_resolver_adapter=FixtureResolver(),
        )
        client = EngineeringAgentClient(root, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        kernel = client.application.kernel
        session = await kernel.create_session("waiting session")
        task = await kernel.create_task("goal", root, session_id=session.session_id)
        await advance(client.application, task.task_id, *EXECUTION_PATH, state)
        task = await kernel.get_task(task.task_id)
        parked = replace(
            task,
            active_agent_checkpoint=(
                {"pending_user_action": {"kind": "CONTINUATION", "reason": pending_reason}}
                if pending_reason is not None else None
            ),
        )
        return client, session, parked

    async def _submit_against(self, client, session_id: str, parked, input_id: str):
        with patch.object(
            type(client), "_active_task", new=AsyncMock(return_value=parked),
        ):
            return await client.submit_user_input(
                session_id, "看下今天的nba新闻", input_id=input_id,
            )

    async def test_orphaned_reason_no_longer_refuses_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client, session, parked = await self._client_with_waiting_task(
                root, pending_reason="completion_exhausted_with_required_gaps",
            )

            result = await self._submit_against(
                client, session.session_id, parked, "orphan-pending-1",
            )

            self.assertNotEqual(
                result.result.get("reason_code"), "task_awaits_user_action",
            )

    async def test_a_live_pending_reason_still_refuses(self) -> None:
        """The refusal is only dropped for reasons that cannot be produced."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client, session, parked = await self._client_with_waiting_task(
                root, pending_reason="incomplete_recoverable",
            )

            result = await self._submit_against(
                client, session.session_id, parked, "orphan-pending-2",
            )

            self.assertEqual(
                result.result.get("reason_code"), "task_awaits_user_action",
            )

    async def test_unresumable_continuation_no_longer_refuses_plain_text(
        self,
    ) -> None:
        """A continuation that can never be resumed must not wedge the Session.

        The producer-removal set cannot express this one: ``unresolved_tool_batch``
        may still be produced and is normally resumable, so it must keep the
        refusal in general. What died here is this *Task's* checkpoint: deleting an
        adapter changed ``adapter_lock_hash``, and a Task carrying tool-execution
        state then resolved to ``REQUIRES_VALIDATION``, which
        ``resolve_session_continuation`` answers with ``BLOCKED``. Rule 3 refused
        plain text while recommending ``/resume``, and ``/resume`` answered
        "use /new" -- so the Session was wedged for the one input a user types.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client, session, parked = await self._client_with_waiting_task(
                root, pending_reason="unresolved_tool_batch",
            )
            blocked = SessionContinuationDecision(
                SessionContinuationMode.BLOCKED, parked.task_id, "AWAITING_USER",
                "runtime_upgrade_has_execution_state",
                SessionResumeSafety.REQUIRES_VALIDATION,
            )
            with patch.object(
                type(client.application.kernel), "resolve_session_continuation",
                new=AsyncMock(return_value=blocked),
            ):
                result = await self._submit_against(
                    client, session.session_id, parked, "unresumable-1",
                )

            self.assertNotEqual(
                result.result.get("reason_code"), "task_awaits_user_action",
            )

    async def test_a_resumable_continuation_still_refuses(self) -> None:
        """The contrast: a deliverable continuation keeps plain text out."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client, session, parked = await self._client_with_waiting_task(
                root, pending_reason="unresolved_tool_batch",
            )
            resumable = SessionContinuationDecision(
                SessionContinuationMode.RESUME_CONTINUATION, parked.task_id,
                "AWAITING_USER", "safe_checkpoint_available",
                SessionResumeSafety.EXACT_RESUME,
            )
            with patch.object(
                type(client.application.kernel), "resolve_session_continuation",
                new=AsyncMock(return_value=resumable),
            ):
                result = await self._submit_against(
                    client, session.session_id, parked, "resumable-1",
                )

            self.assertEqual(
                result.result.get("reason_code"), "task_awaits_user_action",
            )

    async def test_a_waiting_task_without_a_pending_action_still_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client, session, parked = await self._client_with_waiting_task(
                root, pending_reason=None,
            )

            result = await self._submit_against(
                client, session.session_id, parked, "orphan-pending-3",
            )

            self.assertEqual(
                result.result.get("reason_code"), "task_awaits_user_action",
            )

    async def test_the_refusal_names_commands_the_cli_actually_has(self) -> None:
        """``/stop`` is a Web route, not a chat command.

        The message used to tell CLI users to run ``/stop``, which the chat loop
        answers with ``unknown command``. Name the commands that exist.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client, session, parked = await self._client_with_waiting_task(
                root, pending_reason="incomplete_recoverable",
            )

            result = await self._submit_against(
                client, session.session_id, parked, "orphan-pending-4",
            )

            clarification = result.result.get("clarification") or ""
            self.assertIn("/new", clarification)
            self.assertIn("/resume", clarification)
            self.assertNotIn("/stop", clarification)


if __name__ == "__main__":
    unittest.main()
