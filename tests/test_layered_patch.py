"""Layered matching must avoid ambiguity and show what approval will change."""
from __future__ import annotations

import unittest

from tsm_agt.core.v4a import apply_v4a_update, plan_v4a_update
from tsm_agt.core.patch_text import plan_text_edits
from tsm_agt.core.workspace import WorkspacePatchConflict
from tsm_agt.core import ApprovalRequired, ApprovalDecision
from tests import test_core_apply_patch as fixtures

sha256 = fixtures.sha256


def patch(body):
    return "*** Begin Patch\n*** Update File: app.py\n@@\n" + body + "\n*** End Patch"


BEFORE = 'def route():\n    # endpoint: /api/items\n    value = 1\n    return value\n'
BODY = (' def route():\n     # endpoint: /api.items\n'
        '-    value = 1\n+    value = 2\n     return value')


class LayeredPatchTest(unittest.TestCase):
    def test_flat_multiline_replacement_uses_the_shared_unique_match(self):
        old = BEFORE.replace('/api/items', '/api.items')
        new = BEFORE.replace('value = 1', 'value = 2')
        plan = plan_text_edits('app.py', BEFORE, ({'old_text': old, 'new_text': new},))
        self.assertEqual(plan.content, new)
        self.assertEqual(plan.matches[0]['strategy'], 'anchored_similarity')
        self.assertEqual((plan.matches[0]['start_line'], plan.matches[0]['end_line']), (1, 4))

    def test_flat_exact_substring_wins_and_does_not_use_similarity(self):
        source = BEFORE + BEFORE.replace('/api/items', '/api.items')
        old = BEFORE.replace('/api/items', '/api.items')
        plan = plan_text_edits('app.py', source, ({'old_text': old, 'new_text': 'changed\n'},))
        self.assertEqual(plan.content, BEFORE + 'changed\n')
        self.assertFalse(plan.matches)

    def test_flat_similarity_does_not_guess_partial_lines_or_choose_duplicates(self):
        for source, old in (
            (BEFORE, '# endpoint: /api.items'),
            (BEFORE, BEFORE.replace('def route():', 'route():').replace('/api/items', '/api.items')),
            (BEFORE + BEFORE, BEFORE.replace('/api/items', '/api.items')),
            (BEFORE, BEFORE.replace('return value', 'return values').replace('/api/items', '/api.items')),
        ):
            with self.subTest(source=source, old=old), self.assertRaises(WorkspacePatchConflict):
                plan_text_edits('app.py', source, ({'old_text': old, 'new_text': 'changed\n'},))

    def test_flat_fallback_preserves_unquoted_final_newline_and_crlf(self):
        source = BEFORE.replace('\n', '\r\n')
        old = BEFORE.replace('/api/items', '/api.items').rstrip('\n')
        new = BEFORE.replace('value = 1', 'value = 2').rstrip('\n')
        plan = plan_text_edits('app.py', source, ({'old_text': old, 'new_text': new},))
        self.assertEqual(plan.content, new.replace('\n', '\r\n') + '\r\n')

    def test_contiguous_exact_match_wins_over_earlier_blank_line_drift(self):
        source = 'start\n\nvalue = 1\nend\nstart\nvalue = 1\nend\n'
        plan = plan_v4a_update('app.py', source, patch(' start\n-value = 1\n+value = 2\n end'))
        self.assertEqual(plan.matches[0]['strategy'], 'exact')
        self.assertEqual(plan.matches[0]['start_line'], 5)
        self.assertEqual(plan.content, 'start\n\nvalue = 1\nend\nstart\nvalue = 2\nend\n')

    def test_exact_match_wins_over_an_earlier_normalized_match(self):
        source = '  value = 1\nvalue = 1\n'
        plan = plan_v4a_update("app.py", source, patch('-value = 1\n+value = 2'))
        self.assertEqual(plan.content, '  value = 1\nvalue = 2\n')
        self.assertEqual(plan.matches[0]["strategy"], "exact")
        self.assertEqual(plan.matches[0]["start_line"], 2)

    def test_unicode_context_is_matched_without_rewriting_unchanged_source(self):
        source = '# “header”\nvalue = 1\n'
        plan = plan_v4a_update("app.py", source, patch(' # "header"\n-value = 1\n+value = 2'))
        self.assertEqual(plan.content, '# “header”\nvalue = 2\n')
        self.assertEqual(plan.matches[0]["strategy"], "unicode_normalized")

    def test_similarity_preserves_actual_context_and_reports_the_range(self):
        plan = plan_v4a_update("app.py", BEFORE, patch(BODY))
        self.assertEqual(plan.content, BEFORE.replace('value = 1', 'value = 2'))
        self.assertEqual(plan.matches[0]["strategy"], "anchored_similarity")
        self.assertTrue(plan.matches[0]["approximate"])
        self.assertEqual((plan.matches[0]["start_line"], plan.matches[0]["end_line"]), (1, 4))

    def test_similarity_can_match_deleted_old_text_in_a_whole_block(self):
        old = BEFORE.replace('/api/items', '/api.items')
        new = BEFORE.replace('value = 1', 'value = 2')
        body = '\n'.join('-' + line for line in old.splitlines()) + '\n'
        body += '\n'.join('+' + line for line in new.splitlines())
        self.assertEqual(apply_v4a_update("app.py", BEFORE, patch(body)), new)

    def test_similarity_does_not_modify_single_line_or_short_quotes(self):
        for body in ('-    # endpoint: /api.items\n+    # replacement',
                     ' def route():\n-    # endpoint: /api.items\n+    # replacement'):
            with self.subTest(body=body), self.assertRaises(WorkspacePatchConflict):
                apply_v4a_update("app.py", BEFORE, patch(body))

    def test_similarity_requires_both_boundaries_to_match(self):
        for body in (BODY.replace(' def route():', ' def route_other():'),
                     BODY.replace('     return value', '     return values')):
            with self.subTest(body=body), self.assertRaises(WorkspacePatchConflict):
                apply_v4a_update("app.py", BEFORE, patch(body))

    def test_every_interior_line_must_qualify(self):
        body = BODY.replace('# endpoint: /api.items', '# completely unrelated contents')
        with self.assertRaises(WorkspacePatchConflict):
            apply_v4a_update("app.py", BEFORE, patch(body))

    def test_similar_duplicate_blocks_are_rejected(self):
        with self.assertRaises(WorkspacePatchConflict) as caught:
            apply_v4a_update("app.py", BEFORE + BEFORE, patch(BODY))
        self.assertEqual(caught.exception.diagnostics["candidate_count"], 2)
        self.assertEqual(caught.exception.diagnostics["match_strategy"], "anchored_similarity")

    def test_crlf_is_preserved_in_inserted_and_unchanged_lines(self):
        source = BEFORE.replace('\n', '\r\n')
        plan = plan_v4a_update("app.py", source, patch(BODY))
        self.assertEqual(plan.content, source.replace('value = 1', 'value = 2'))


class LayeredPatchRuntimeTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CoreApplyPatchTest.asyncSetUp
    asyncTearDown = fixtures.CoreApplyPatchTest.asyncTearDown
    v4a_call = fixtures.CoreApplyPatchTest.v4a_call
    approve = fixtures.CoreApplyPatchTest.approve
    call = fixtures.CoreApplyPatchTest.call

    async def test_flat_similarity_approval_and_receipt_show_the_actual_change(self):
        target = self.workspace / 'app.py'
        target.write_text(BEFORE)
        old = BEFORE.replace('/api/items', '/api.items')
        new = BEFORE.replace('value = 1', 'value = 2')
        call = self.call('flat-approximate', 'app.py', sha256(BEFORE), [{'old_text': old, 'new_text': new}])
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(self.task.task_id, 'turn-flat', call)
        request = caught.exception.request
        self.assertIn('anchored_similarity', request.preview)
        self.assertNotIn('/api.items', request.preview)
        self.assertEqual(target.read_text(), BEFORE)
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash, ApprovalDecision.APPROVE, 'reviewed actual diff')
        self.assertTrue(result.ok)
        self.assertEqual(target.read_text(), new)
        self.assertIn(result.data['diff'], request.preview)
        self.assertTrue(result.data['patch_matches'][0]['approximate'])

    async def test_flat_later_failure_does_not_commit_an_earlier_similar_edit(self):
        target = self.workspace / 'app.py'
        target.write_text(BEFORE)
        result = await self.approve(self.call('flat-atomic', 'app.py', sha256(BEFORE), [
            {'old_text': BEFORE.replace('/api/items', '/api.items'), 'new_text': BEFORE.replace('value = 1', 'value = 2')},
            {'old_text': 'not in file', 'new_text': 'changed'},
        ]))
        self.assertEqual(result.error_code, 'PATCH_CONFLICT')
        self.assertEqual(target.read_text(), BEFORE)
        self.assertFalse((await self.application.kernel.get_task(self.task.task_id)).mutation_journal)

    async def test_flat_fallback_is_rejected_if_file_changes_after_preview(self):
        target = self.workspace / 'app.py'
        target.write_text(BEFORE)
        call = self.call('flat-race', 'app.py', sha256(BEFORE), [
            {'old_text': BEFORE.replace('/api/items', '/api.items'), 'new_text': BEFORE.replace('value = 1', 'value = 2')}])
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(self.task.task_id, 'turn-flat-race', call)
        request = caught.exception.request
        external = BEFORE.replace('value = 1', 'value = 3')
        target.write_text(external)
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash, ApprovalDecision.APPROVE, 'approved')
        self.assertEqual(result.error_code, 'CONFLICT')
        self.assertEqual(target.read_text(), external)
        self.assertFalse((await self.application.kernel.get_task(self.task.task_id)).mutation_journal)

    async def test_batch_similarity_reports_matches_and_remains_atomic_on_later_conflict(self):
        from tsm_agt.ports import ToolCall
        for filename in ('first.py', 'second.py'):
            (self.workspace / filename).write_text(BEFORE)
        patches = [{'path': filename, 'expected_hash': sha256(BEFORE), 'edits': [
            {'old_text': BEFORE.replace('/api/items', '/api.items'), 'new_text': BEFORE.replace('value = 1', 'value = 2')}]
        } for filename in ('first.py', 'second.py')]
        patches[1]['edits'][0]['old_text'] = 'missing text'
        result = await self.approve(ToolCall('batch-fail', 'core.apply_patches', {'patches': patches}))
        self.assertEqual(result.error_code, 'PATCH_CONFLICT')
        self.assertEqual((self.workspace / 'first.py').read_text(), BEFORE)
        self.assertFalse((await self.application.kernel.get_task(self.task.task_id)).mutation_journal)
        patches[1]['edits'][0]['old_text'] = BEFORE.replace('/api/items', '/api.items')
        result = await self.approve(ToolCall('batch-ok', 'core.apply_patches', {'patches': patches}))
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data['mutations']), 2)
        for row in result.data['mutations']:
            self.assertTrue(row['patch_matches'][0]['approximate'])
            self.assertIn('+    value = 2', row['diff'])

    async def test_approval_and_execution_show_the_actual_diff(self):
        target = self.workspace / 'app.py'
        target.write_text(BEFORE)
        call = self.v4a_call('similarity', sha256(BEFORE), patch(BODY))
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(self.task.task_id, 'turn-similarity', call)
        request = caught.exception.request
        self.assertIn('anchored_similarity', request.preview)
        self.assertIn('     # endpoint: /api/items', request.preview)
        self.assertNotIn('/api.items', request.preview)
        self.assertIn('-    value = 1', request.preview)
        self.assertIn('+    value = 2', request.preview)
        self.assertEqual(target.read_text(), BEFORE)
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash, ApprovalDecision.APPROVE, 'approve actual diff')
        self.assertTrue(result.ok)
        self.assertTrue(result.data['patch_matches'][0]['approximate'])
        self.assertIn(result.data['diff'], request.preview)
        self.assertEqual(target.read_text(), BEFORE.replace('value = 1', 'value = 2'))
        self.assertEqual(len((await self.application.kernel.get_task(self.task.task_id)).mutation_journal), 1)

    async def test_hash_is_rechecked_after_approving_a_similarity_preview(self):
        target = self.workspace / 'app.py'
        target.write_text(BEFORE)
        call = self.v4a_call('race', sha256(BEFORE), patch(BODY))
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(self.task.task_id, 'turn-race', call)
        request = caught.exception.request
        external_change = BEFORE.replace('value = 1', 'value = 100')
        target.write_text(external_change)
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash, ApprovalDecision.APPROVE, 'approved')
        self.assertEqual(result.error_code, 'CONFLICT')
        self.assertEqual(target.read_text(), external_change)
        self.assertFalse((await self.application.kernel.get_task(self.task.task_id)).mutation_journal)

    async def test_later_failure_prevents_an_earlier_similar_hunk_from_committing(self):
        target = self.workspace / 'app.py'
        target.write_text(BEFORE + 'tail = 1\n')
        result = await self.approve(self.v4a_call(
            'atomic', sha256(target.read_text()), patch(BODY + '\n@@\n-tail = 99\n+tail = 2')))
        self.assertEqual(result.error_code, 'PATCH_CONFLICT')
        self.assertEqual(target.read_text(), BEFORE + 'tail = 1\n')
        self.assertFalse((await self.application.kernel.get_task(self.task.task_id)).mutation_journal)
