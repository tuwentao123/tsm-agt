"""Recover a rejected edit after an approval wait without overwriting newer work."""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider, CoreWorkspaceMutationToolProvider
from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ApprovalDecision, TaskState
from tsm_agt.core.conversation_loop.finalization import RuntimeFinalizationGate
from tsm_agt.ports import (
    FinalizationAction, ToolEffect, ToolRecoveryKind, ToolResult, ToolResultAuthority,
)
from tsm_agt.sdk import EngineeringAgentClient
from tests.test_conversation_loop import ScriptedModel, text_response, tool_response
from tests import test_turn_finalization as finalization_fixtures


_BEFORE = 'value = 1\n'
_CONCURRENT = '# independent change during approval\nvalue = 1\n'
_AFTER = '# independent change during approval\nvalue = 2\n'


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def edit_arguments(kind, expected):
    if kind == 'core.apply_patch_v4a':
        return {'expected_hash': expected, 'patch': (
            '*** Begin Patch\n*** Update File: app.py\n@@\n'
            '-value = 1\n+value = 2\n*** End Patch'
        )}
    edit = {'path': 'app.py', 'expected_hash': expected,
            'edits': [{'old_text': 'value = 1', 'new_text': 'value = 2'}]}
    if kind == 'core.apply_patches':
        return {'patches': [edit, {
            'path': 'other.py', 'expected_hash': digest('other = 1\n'),
            'edits': [{'old_text': 'other = 1', 'new_text': 'other = 2'}],
        }]}
    return edit


class PatchRecoveryIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def client(self, root, model):
        with patch('tsm_agt.bootstrap.composition.UserSkillsStore',
                   return_value=UserSkillsStore(root / 'home')):
            application = compose_fixture_application(
                model_adapter=model,
                tool_adapters=(CoreReadOnlyToolProvider(), CoreWorkspaceMutationToolProvider()),
                store_adapter=SQLiteRuntimeStore(root / 'runtime.db'),
            )
        client = EngineeringAgentClient(root, application_factory=lambda: application)
        await client.start()
        self.addAsyncCleanup(client.close)
        return client

    async def test_stale_approval_recovers_all_patch_formats_and_survives_restart(self):
        for kind in ('core.apply_patch', 'core.apply_patch_v4a', 'core.apply_patches'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / 'app.py'
                other = root / 'other.py'
                target.write_text(_BEFORE)
                other.write_text('other = 1\n')
                model = ScriptedModel([
                    tool_response('edit', 'stale', kind, edit_arguments(kind, digest(_BEFORE))),
                    text_response('abandoned', 'Here is a proposed fix; please apply it yourself.'),
                    tool_response('refresh', 'refresh', 'core.read_file', {'path': 'app.py'}),
                    tool_response('retry', 'fresh', kind, edit_arguments(kind, digest(_CONCURRENT))),
                ])
                client = await self.client(root, model)
                accepted = await client.submit_task('Fix value in app.py', command_id='create')
                await client.wait_task(accepted.task_id, timeout=5)
                task = await client.application.kernel.get_task(accepted.task_id)
                old_approval = task.pending_approval
                self.assertIsNotNone(old_approval)
                target.write_text(_CONCURRENT)  # editor/user changes while approval is pending
                await client.resolve_approval(old_approval.request_id, ApprovalDecision.APPROVE,
                                              'reviewed original edit', command_id='approve-stale')
                task = await client.application.kernel.get_task(task.task_id)
                self.assertIs(task.state, TaskState.AWAITING_APPROVAL)
                fresh_approval = task.pending_approval
                self.assertNotEqual(old_approval.request_id, fresh_approval.request_id)
                self.assertNotEqual(old_approval.payload_hash, fresh_approval.payload_hash)
                self.assertEqual(target.read_text(), _CONCURRENT)
                self.assertEqual(other.read_text(), 'other = 1\n')  # failed batch is atomic
                checkpoint = task.active_agent_checkpoint
                self.assertEqual(checkpoint['finalization_state']['edit_recovery_continuations'], 1)
                self.assertEqual([len(request.tools) > 0 for request in model.requests], [True] * 4)
                # The recovery must see a real fresh read, not simply substitute actual_hash.
                read_result = [block.result for message in model.requests[-1].messages
                               for block in message.content if hasattr(block, 'result')
                               and block.result.call_id == 'refresh'][0]
                self.assertEqual(read_result.data['sha256'], digest(_CONCURRENT))
                self.assertEqual(read_result.data['content'], _CONCURRENT)
                await client.close()

                resumed_model = ScriptedModel([
                    tool_response('verify', 'verify', 'core.read_file', {'path': 'app.py'}),
                    text_response('done', 'The edit is applied and the file content is verified.'),
                ])
                resumed = await self.client(root, resumed_model)
                await resumed.resolve_approval(fresh_approval.request_id, ApprovalDecision.APPROVE,
                                               'reviewed fresh edit', command_id='approve-fresh')
                result = await resumed.get_task_result(task.task_id)
                self.assertEqual(result.state, TaskState.AWAITING_INPUT.value)
                self.assertEqual(target.read_text(), _AFTER)
                if kind == 'core.apply_patches':
                    self.assertEqual(other.read_text(), 'other = 2\n')
                events = await resumed._store().read_events(task.task_id)
                recoveries = [event for event in events
                              if event.event_type == 'turn.finalization_runtime_evaluated'
                              and event.payload['reason_code'] == 'workspace_edit_recovery_required']
                self.assertEqual(len(recoveries), 1)
                self.assertNotEqual(result.assistant_text, 'Here is a proposed fix; please apply it yourself.')

    async def test_repeated_conflict_after_restart_stops_partial_without_reusing_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'app.py'
            target.write_text(_BEFORE)
            model = ScriptedModel([
                tool_response('edit', 'stale', 'core.apply_patch',
                              edit_arguments('core.apply_patch', digest(_BEFORE))),
                text_response('draft', 'Here is the patch.'),
                tool_response('refresh', 'refresh', 'core.read_file', {'path': 'app.py'}),
                tool_response('retry', 'fresh', 'core.apply_patch',
                              edit_arguments('core.apply_patch', digest(_CONCURRENT))),
            ])
            client = await self.client(root, model)
            accepted = await client.submit_task('Fix app.py', command_id='create')
            await client.wait_task(accepted.task_id, timeout=5)
            first = (await client.application.kernel.get_task(accepted.task_id)).pending_approval
            target.write_text(_CONCURRENT)
            await client.resolve_approval(first.request_id, ApprovalDecision.APPROVE,
                                          'reviewed first attempt', command_id='first')
            pending = (await client.application.kernel.get_task(accepted.task_id)).pending_approval
            await client.close()
            latest = '# a second independent change\n' + _CONCURRENT
            target.write_text(latest)
            resumed = await self.client(root, ScriptedModel([text_response('draft-again', 'Apply it yourself.')]))
            await resumed.resolve_approval(pending.request_id, ApprovalDecision.APPROVE,
                                           'reviewed second attempt', command_id='second')
            result = await resumed.get_task_result(accepted.task_id)
            self.assertEqual(result.status, 'incomplete')
            self.assertIsNone(result.assistant_text)
            self.assertEqual(target.read_text(), latest)
            events = await resumed._store().read_events(accepted.task_id)
            self.assertFalse(any(event.event_type == 'turn.completed' for event in events))
            boundary = [event for event in events if event.event_type == 'turn.incomplete_boundary']
            self.assertEqual(boundary[-1].payload['reason'], 'workspace_edit_recovery_incomplete')

            await resumed.close()
            restored = await self.client(root, ScriptedModel([]))
            persisted = await restored.get_task_result(accepted.task_id)
            self.assertEqual(persisted.status, 'interrupted')
            self.assertEqual(persisted.phase1_state, 'INTERRUPTED')
            restored_task = await restored.application.kernel.get_task(accepted.task_id)
            self.assertEqual(restored_task.active_agent_checkpoint['finalization_state']
                             ['edit_recovery_continuations'], 1)

            async def collect():
                return [packet async for packet in restored.subscribe_task_ui(accepted.task_id)]

            packets = await asyncio.wait_for(collect(), timeout=2)
            snapshot = [packet for packet in packets if packet['type'] == 'task.snapshot'][-1]
            self.assertTrue(snapshot['final'])
            self.assertEqual(snapshot['status'], 'interrupted')
            self.assertIsNone(snapshot['assistant_text'])


class PatchRecoveryGateTest(unittest.IsolatedAsyncioTestCase):
    def state(self, *, error='CONFLICT', not_started=True, recovery=True):
        fixtures = finalization_fixtures.RuntimeFinalizationGateTest()
        state = fixtures._state()
        state.visible_tool_by_name = {
            'core.apply_patch': fixtures._spec('core.apply_patch', ToolEffect.MUTATE,
                                               ToolResultAuthority.MUTATION_FACT),
        }
        state.messages = fixtures._tool_exchange(
            'edit', 'core.apply_patch', {'path': 'app.py'},
            ToolResult('edit', False, error_code=error,
                       data={'execution_state': 'not_started' if not_started else 'unknown'},
                       recovery_kind=(ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE if recovery
                                      else ToolRecoveryKind.NONE)),
        )
        return state

    async def test_rejected_edits_get_one_recovery_and_ignored_recovery_is_partial(self):
        for code in ('CONFLICT', 'PATCH_CONFLICT', 'PATCH_INVALID_V4A', 'PATCH_LITERAL_NEWLINE_ESCAPE'):
            with self.subTest(code=code):
                state = self.state(error=code)
                answer = text_response('draft', 'Use this patch yourself.')
                gate = RuntimeFinalizationGate()
                first = await gate.evaluate(state, answer)
                self.assertIs(first.action, FinalizationAction.CONTINUE)
                state.edit_recovery_continuations = 1
                second = await gate.evaluate(state, answer)
                self.assertIs(second.action, FinalizationAction.INCOMPLETE)
                self.assertEqual(second.reason_code, 'workspace_edit_recovery_incomplete')

    async def test_denials_unknown_outcomes_and_known_failures_never_force_retry(self):
        for code, started in (('DENIED', True), ('APPROVAL_DENIED', True),
                              ('UNKNOWN_OUTCOME', False), ('CONFLICT', False),
                              ('TOOL_FAILED', True)):
            with self.subTest(code=code, started=started):
                decision = await RuntimeFinalizationGate().evaluate(
                    self.state(error=code, not_started=started), text_response('blocker', 'Stopped.'),
                )
                self.assertIs(decision.action, FinalizationAction.ACCEPT)

    async def test_no_budget_keeps_rejected_edit_incomplete(self):
        state = self.state()
        state.model_call_count = state.checkpoint.max_model_calls
        decision = await RuntimeFinalizationGate().evaluate(state, text_response('draft', 'Use this.'))
        self.assertIs(decision.action, FinalizationAction.INCOMPLETE)

    async def test_subsequent_inspection_may_establish_no_further_edit_is_needed(self):
        state = self.state()
        fixtures = finalization_fixtures.RuntimeFinalizationGateTest()
        state.messages.extend(fixtures._tool_exchange(
            'read', 'core.read_file', {'path': 'app.py'},
            ToolResult('read', True, data={'content': 'value = 2\n'}),
        ))
        decision = await RuntimeFinalizationGate().evaluate(
            state, text_response('already-done', 'The current file already contains the requested change.'),
        )
        self.assertIs(decision.action, FinalizationAction.ACCEPT)
