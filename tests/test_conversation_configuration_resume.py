"""A new conversation Turn upgrades configuration; a pending approval does not."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import AgentCheckpointConflict, AgentTurnCheckpoint, ApprovalDecision, TaskState
from tsm_agt.core.execution import ToolCommitState
from tsm_agt.core.policy import CoreToolPolicy
from tsm_agt.core.process import BackgroundProcessState
from tsm_agt.core.workspace import MutationOperation, MutationRecord
from tsm_agt.ports import CheckpointCompatibilityAction, ToolIdempotency, ToolResult, ToolRisk
from tests.test_approval_context_continuity import ChangeProvider
from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response
from tests import test_session_context as session_fixtures


class FileChangeProvider(ChangeProvider):
    async def invoke(self, call, context):
        item = call.arguments["item"]
        mutation = await context.workspace_control.apply_patch(
            f"{item}.txt", None, ({"old_text": "", "new_text": item},),
        )
        self.calls.append(item)
        return ToolResult(call.call_id, True, data=mutation)


class ConversationConfigurationResumeTest(unittest.IsolatedAsyncioTestCase):
    async def test_existing_sqlite_conversation_upgrades_before_new_turn_and_approval_resumes_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first = compose_fixture_application(
                model_adapter=ScriptedModel([
                    tool_response("prior-change", "prior", "fixture.change", {"item": "prior"}),
                    text_response("proposal", "I can implement A and B."),
                ]),
                tool_adapters=(FileChangeProvider(ToolRisk.R1),), store_adapter=SQLiteRuntimeStore(database),
            )
            await first.registry.start_all()
            try:
                with patch.object(CoreToolPolicy, "_POLICY_VERSION", 2):
                    task = await _executing(first, root)
                    prior = await first.kernel.run_agent_turn(task.task_id, "Make the prior change, then propose A and B")
                    await first.kernel.resolve_agent_approval(
                        prior.approval_request_id, ApprovalDecision.APPROVE, "reviewed prior operation",
                    )
                    old = (await first.kernel.get_task(task.task_id)).effective_configurations[-1]
                    self.assertEqual(old.policy["version"], 2)
            finally:
                await first.registry.stop_all()

            model = ScriptedModel([
                tool_response("modify-a", "a", "fixture.change", {"item": "A"}),
                tool_response("modify-b", "b", "fixture.change", {"item": "B"}),
                text_response("done", "Both changes were made."),
                text_response("explain", "Only explaining, not changing anything."),
            ])
            provider = FileChangeProvider(ToolRisk.R1)
            app = compose_fixture_application(model_adapter=model, tool_adapters=(provider,),
                                              store_adapter=SQLiteRuntimeStore(database))
            await app.registry.start_all()
            try:
                outcome = await app.kernel.run_agent_turn(task.task_id, "Implement both now")
                current = await app.kernel.get_task(task.task_id)
                self.assertEqual(len(current.effective_configurations), 2)
                self.assertEqual(current.effective_configurations[0].policy_hash, old.policy_hash)
                self.assertEqual(current.effective_configurations[-1].policy["version"], CoreToolPolicy._POLICY_VERSION)
                checkpoint = AgentTurnCheckpoint.from_data(current.pending_approval.agent_checkpoint)
                self.assertEqual(checkpoint.effective_config_hash, current.effective_configurations[-1].effective_config_hash)
                self.assertFalse((root / "A.txt").exists())
                self.assertEqual(len(current.mutation_journal), 1)
                for item in ("A", "B"):
                    self.assertFalse((root / f"{item}.txt").exists())
                    outcome = await app.kernel.resolve_agent_approval(
                        outcome.approval_request_id, ApprovalDecision.APPROVE, "reviewed exact action",
                    )
                    self.assertEqual((root / f"{item}.txt").read_text(), item)
                self.assertEqual(provider.calls, ["A", "B"])
                self.assertEqual((root / "prior.txt").read_text(), "prior")
                self.assertEqual(len((await app.kernel.get_task(task.task_id)).mutation_journal), 3)
                self.assertEqual(outcome.assistant_message.text, "Both changes were made.")
                events = await app.kernel.dependencies.store.read_events(task.task_id)
                self.assertFalse(any(event.event_type == "checkpoint.conflict" for event in events))
                refresh = next(event for event in events if event.event_type == "config.snapshot"
                               and event.payload.get("reason") == "new_conversation_turn")
                first_call = next(event for event in events if event.event_type == "tool.requested"
                                  and event.payload.get("turn_id") == refresh.payload["turn_id"])
                self.assertLess(refresh.sequence, first_call.sequence)
                await app.kernel.run_agent_turn(task.task_id, "Explain only")
                self.assertEqual(len((await app.kernel.get_task(task.task_id)).effective_configurations), 2)
                self.assertEqual(provider.calls, ["A", "B"])
            finally:
                await app.registry.stop_all()

    async def test_policy_change_while_approval_pending_still_invalidates_without_execution(self):
        app = compose_fixture_application(
            model_adapter=ScriptedModel([tool_response("modify", "edit", "fixture.change", {"item": "A"})]),
            tool_adapters=(provider := ChangeProvider(ToolRisk.R1),),
        )
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        with tempfile.TemporaryDirectory() as directory:
            task = await _executing(app, Path(directory))
            outcome = await app.kernel.run_agent_turn(task.task_id, "Implement")
            current = await app.kernel.get_task(task.task_id)
            revision = len(current.effective_configurations)
            with patch.object(CoreToolPolicy, "_POLICY_VERSION", CoreToolPolicy._POLICY_VERSION + 1):
                with self.assertRaisesRegex(AgentCheckpointConflict, "pending approval"):
                    await app.kernel._refresh_conversation_configuration(task.task_id, "not-a-new-turn")
                with self.assertRaisesRegex(AgentCheckpointConflict, "policy_hash"):
                    await app.kernel.resolve_agent_approval(
                        outcome.approval_request_id, ApprovalDecision.APPROVE, "reviewed",
                    )
            current = await app.kernel.get_task(task.task_id)
            self.assertEqual(current.state, TaskState.CONFLICT)
            self.assertIsNone(current.pending_approval)
            self.assertEqual(provider.calls, [])
            self.assertEqual(len(current.effective_configurations), revision)
            events = await app.kernel.dependencies.store.read_events(task.task_id)
            invalidated = next(event for event in events if event.event_type == "approval.invalidated")
            self.assertEqual(invalidated.payload["turn_id"], outcome.turn_id)
            self.assertFalse(any(event.event_type == "approval.resolved" for event in events))
            self.assertFalse(any(event.event_type == "tool.started" for event in events))

    async def test_pending_approval_survives_same_configuration_restart_without_new_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first = compose_fixture_application(
                model_adapter=ScriptedModel([
                    tool_response("edit-a", "edit-a", "fixture.change", {"item": "A"}),
                ]), tool_adapters=(FileChangeProvider(ToolRisk.R1),),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await first.registry.start_all()
            try:
                task = await _executing(first, root)
                outcome = await first.kernel.run_agent_turn(task.task_id, "Implement A")
                configurations = (await first.kernel.get_task(task.task_id)).effective_configurations
                self.assertFalse((root / "A.txt").exists())
            finally:
                await first.registry.stop_all()
            provider = FileChangeProvider(ToolRisk.R1)
            second = compose_fixture_application(
                model_adapter=ScriptedModel([text_response("done", "A is written")]),
                tool_adapters=(provider,), store_adapter=SQLiteRuntimeStore(database),
            )
            await second.registry.start_all()
            try:
                result = await second.kernel.resolve_agent_approval(
                    outcome.approval_request_id, ApprovalDecision.APPROVE, "reviewed exact pending action",
                )
                self.assertEqual(result.assistant_message.text, "A is written")
                self.assertEqual(provider.calls, ["A"])
                self.assertEqual((root / "A.txt").read_text(), "A")
                current = await second.kernel.get_task(task.task_id)
                self.assertEqual(current.effective_configurations, configurations)
                self.assertEqual(len(current.mutation_journal), 1)
                self.assertIsNone(current.pending_approval)
            finally:
                await second.registry.stop_all()

    async def test_old_settled_work_is_not_a_current_effect_but_uncertainty_still_blocks(self):
        app = compose_fixture_application(model_adapter=ScriptedModel([text_response("unused", "unused")]),
                                          tool_adapters=())
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        with tempfile.TemporaryDirectory() as directory:
            task = await _executing(app, Path(directory))
            checkpoint = AgentTurnCheckpoint(task.task_id, "new-turn", 1, (), (), (), 0, 0, 0, 0, 5, 5, 256, 10)
            checkpoint = await app.kernel._bind_agent_checkpoint(task, checkpoint, await app.kernel.list_tools())
            task = await app.kernel.get_task(task.task_id)
            old = session_fixtures.SessionContextProjectorTest._execution(call_id="old", tool="core.read_file", arguments={}, data={})
            old = replace(old, turn_id="old-turn")
            settled_task = replace(task, tool_executions={old.execution_id: old})
            with patch.object(CoreToolPolicy, "_POLICY_VERSION", CoreToolPolicy._POLICY_VERSION + 1):
                decision = await app.kernel._evaluate_checkpoint_compatibility(
                    settled_task, checkpoint, await app.kernel.list_tools(),
                )
            self.assertEqual(decision.action, CheckpointCompatibilityAction.REBASE_REQUIRED)
            for state, reason in (
                (ToolCommitState.UNKNOWN_OUTCOME, "unknown_tool_execution_outcome"),
                (ToolCommitState.RUNNING, "running_non_idempotent_tool"),
            ):
                with self.subTest(state=state):
                    unresolved = replace(old, state=state, result=None, idempotency=ToolIdempotency.NON_IDEMPOTENT)
                    task_with_uncertainty = replace(task, tool_executions={old.execution_id: unresolved})
                    decision = await app.kernel._evaluate_checkpoint_compatibility(
                        task_with_uncertainty, checkpoint, await app.kernel.list_tools(),
                    )
                    self.assertEqual(decision.action, CheckpointCompatibilityAction.BLOCKED)
                    self.assertIn(reason, decision.conflict_reasons)


class CheckpointTurnScopeTest(unittest.TestCase):
    def test_prior_successes_are_excluded_but_current_unknown_and_live_processes_are_not(self):
        from tsm_agt.core.kernel_checkpoint import CheckpointMixin
        from tsm_agt.core.task import TaskSnapshot

        old = session_fixtures.SessionContextProjectorTest._execution(call_id="old", tool="core.read_file", arguments={}, data={})
        old = replace(old, turn_id="old-turn", invocation_id="old-invocation")
        live = replace(old, execution_id="live", turn_id="current-turn", invocation_id="current-invocation")
        unknown = replace(old, execution_id="unknown", turn_id="", invocation_id="unknown-invocation")
        mutation = MutationRecord("old-mutation", "a.py", MutationOperation.MODIFY, "before", "after",
                                  old.invocation_id, None, datetime.now(timezone.utc))
        task = replace(TaskSnapshot.create("task", "chat", "/tmp"),
                       tool_executions={"old": old, "live": live, "unknown": unknown},
                       mutation_journal=(mutation, replace(mutation, mutation_id="live", step_id=live.invocation_id),
                                         replace(mutation, mutation_id="unattributed", step_id="no-ledger")),
                       background_processes={
                           "past": SimpleNamespace(turn_id="old-turn", state=BackgroundProcessState.EXITED),
                           "running": SimpleNamespace(turn_id="old-turn", state=BackgroundProcessState.RUNNING),
                           "orphan": SimpleNamespace(turn_id="old-turn", state=BackgroundProcessState.ORPHANED),
                       })
        checkpoint = SimpleNamespace(turn_id="current-turn")
        self.assertEqual(CheckpointMixin._checkpoint_execution_counts(task, checkpoint), (2, 2, 2))
