"""Patch diagnostics locate real content without changing matching semantics."""
from __future__ import annotations

import hashlib
import json
import unittest

from tsm_agt.adapters.anthropic_messages import AnthropicMessagesModelProvider
from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider
from tsm_agt.core.v4a import apply_v4a_update
from tsm_agt.core.workspace import WorkspacePatchConflict
from tsm_agt.ports import Message, MessageRole, ToolResultBlock
from tests import test_core_apply_patch as fixtures


sha256 = fixtures.sha256


def patch(body):
    return "*** Begin Patch\n*** Update File: app.py\n" + body + "\n*** End Patch"


class V4ADiagnosticsTest(unittest.TestCase):
    def conflict(self, original, body):
        with self.assertRaises(WorkspacePatchConflict) as caught:
            apply_v4a_update("app.py", original, patch(body))
        return caught.exception.diagnostics

    def test_wrong_import_locates_actual_line_but_does_not_fuzzy_apply(self):
        original = "from dataclasses import dataclass, field, replace\n\nvalue = 1\n"
        details = self.conflict(original, "@@\n-from dataclasses import dataclass, field\n+from dataclasses import dataclass, field")
        self.assertEqual(details["hunk_number"], 1)
        self.assertEqual(details["patch_line_number"], 4)
        self.assertFalse(details["hunk_changes_content"])
        self.assertEqual(details["snapshot_sha256"], sha256(original))
        self.assertEqual(details["candidate_locations"][0]["line_number"], 1)
        self.assertIn(", replace", details["candidate_locations"][0]["content"])
        self.assertTrue(details["candidates_are_suggestions"])

    def test_later_hunk_reports_original_file_and_correct_patch_line(self):
        original = "a = 1\nb = 2\n"
        details = self.conflict(original, "@@\n-a = 1\n+a = 3\n@@\n-b = 99\n+b = 4")
        self.assertEqual(details["hunk_number"], 2)
        self.assertEqual(details["patch_line_number"], 7)
        self.assertEqual(details["snapshot_sha256"], sha256(original))
        self.assertEqual(details["diagnostics_scope"], "first_conflict")

    def test_repeated_expected_line_uses_the_actual_failing_occurrence(self):
        details = self.conflict("start\nrepeat\nwrong\n", "@@\n start\n repeat\n-repeat\n+finish")
        self.assertEqual(details["patch_line_number"], 6)
        self.assertEqual(details["candidate_locations"][0]["match_kind"], "after_matched_context")
        self.assertEqual(details["candidate_locations"][0]["line_number"], 3)

    def test_deletion_typo_compares_old_side_with_observed_file_line(self):
        original = 'def route():\n    return "/api/items"\n'
        bad = '@@\n def route():\n-    return "/api.items"\n+    return "/api/v2/items"'
        details = self.conflict(original, bad)
        self.assertEqual(details["patch_line_kind"], "deletion")
        self.assertEqual(details["patch_marker"], "-")
        self.assertEqual(details["match_side"], "old_file")
        self.assertEqual(details["expected_context"], '    return "/api.items"')
        self.assertEqual(details["actual_line"], '    return "/api/items"')
        self.assertEqual(details["actual_line_number"], 2)
        self.assertEqual(details["actual_location_basis"], "partial_hunk_match")
        self.assertFalse(details["actual_line_at_eof"])

        # Repairing only the output side must still fail at the same old line.
        plus_only = bad.replace('+    return "/api/v2/items"', '+    return "/api/items/v2"')
        self.assertEqual(self.conflict(original, plus_only), details)
        corrected = plus_only.replace('-    return "/api.items"', '-    return "/api/items"')
        self.assertEqual(apply_v4a_update("app.py", original, patch(corrected)),
                         'def route():\n    return "/api/items/v2"\n')

    def test_context_typo_is_not_labelled_as_a_deletion(self):
        details = self.conflict("start\nlabel = 'actual'\nend\n",
                                "@@\n start\n label = 'wrong'\n-end\n+finish")
        self.assertEqual(details["patch_line_kind"], "context")
        self.assertEqual(details["patch_marker"], " ")
        self.assertEqual(details["actual_line"], "label = 'actual'")

    def test_partial_match_suppresses_unrelated_lexical_candidates(self):
        original = "start\nreturn actual\n\nreturn wrong-ish\nreturn unrelated\n"
        details = self.conflict(original, "@@\n start\n-return wrong\n+return fixed")
        self.assertEqual(len(details["candidate_locations"]), 1)
        self.assertEqual(details["candidate_locations"][0]["line_number"], 2)

    def test_end_of_file_is_not_reported_as_the_last_existing_line(self):
        details = self.conflict("start\n", "@@\n start\n-missing\n+finish")
        self.assertTrue(details["actual_line_at_eof"])
        self.assertIsNone(details["actual_line"])
        self.assertEqual(details["actual_line_number"], 2)

    def test_observed_long_line_has_its_own_truncation_flag(self):
        original = "start\nvalue = " + "x" * 600 + "\n"
        details = self.conflict(original, "@@\n start\n-value = old\n+value = new")
        self.assertEqual(len(details["actual_line"]), 512)
        self.assertTrue(details["actual_line_truncated"])

    def test_implicit_first_hunk_does_not_shift_subsequent_hunk_number(self):
        details = self.conflict("a = 1\nb = 2\n", "-a = 1\n+a = 3\n@@\n-b = 99\n+b = 4")
        self.assertEqual(details["hunk_number"], 2)
        self.assertEqual(details["patch_line_number"], 6)

    def test_out_of_order_hunk_reports_exact_context_before_cursor(self):
        details = self.conflict("a = 1\nb = 2\n", "@@\n-b = 2\n+b = 3\n@@\n-a = 1\n+a = 2")
        self.assertEqual(details["candidate_locations"][0]["match_kind"], "exact_text_before_cursor")
        self.assertEqual(details["candidate_locations"][0]["line_number"], 1)

    def test_missing_named_anchor_reports_header_line(self):
        details = self.conflict("def actual():\n    pass\n", "@@ def absent():\n-    pass\n+    return 1")
        self.assertEqual(details["patch_line_number"], 3)
        self.assertEqual(details["expected_context"], "def absent():")
        self.assertEqual(details["patch_line_kind"], "anchor")
        self.assertEqual(details["patch_marker"], "@@")
        self.assertIsNone(details["actual_line"])
        self.assertIsNone(details["actual_line_number"])

    def test_no_related_content_does_not_invent_a_location(self):
        details = self.conflict("{}\n", "@@\n-missing_xyz\n+added")
        self.assertEqual(details["candidate_locations"], [])

    def test_context_snippets_are_bounded_even_for_long_lines(self):
        original = "key = wrong " + "longvalue " * 400 + "\n"
        details = self.conflict(original, "@@\n-key = wrong\n+key = fixed")
        candidates = details["candidate_locations"]
        self.assertLessEqual(len(candidates), 3)
        self.assertTrue(candidates[0]["content_truncated"])
        self.assertLessEqual(len(candidates[0]["content"]), 1004)

    def test_long_expected_context_is_marked_incomplete(self):
        quoted = "value = " + "x" * 600
        details = self.conflict("unrelated = 1\n", "@@\n-" + quoted + "\n+value = 2")
        self.assertEqual(len(details["expected_context"]), 512)
        self.assertTrue(details["expected_context_truncated"])


class V4ADiagnosticsRuntimeTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CoreApplyPatchTest.asyncSetUp
    asyncTearDown = fixtures.CoreApplyPatchTest.asyncTearDown
    v4a_call = fixtures.CoreApplyPatchTest.v4a_call
    approve = fixtures.CoreApplyPatchTest.approve

    async def test_failed_second_hunk_is_atomic_and_corrected_patch_can_follow(self):
        before = "a = 1\nb = 2\n"
        target = self.workspace / "app.py"
        target.write_text(before)
        failed = await self.approve(self.v4a_call(
            "bad-second", sha256(before), patch("@@\n-a = 1\n+a = 3\n@@\n-b = 99\n+b = 4"),
        ))
        self.assertEqual(failed.error_code, "PATCH_CONFLICT")
        self.assertTrue(failed.data["no_edits_applied"])
        self.assertEqual(failed.data["patch_conflict"]["hunk_number"], 2)
        self.assertEqual(target.read_text(), before)
        self.assertFalse((await self.application.kernel.get_task(self.task.task_id)).mutation_journal)

        corrected = await self.approve(self.v4a_call(
            "fixed-second", sha256(before), patch("@@\n-a = 1\n+a = 3\n@@\n-b = 2\n+b = 4"),
        ))
        self.assertTrue(corrected.ok)
        self.assertEqual(target.read_text(), "a = 3\nb = 4\n")

    async def test_diagnostics_reach_both_provider_payloads(self):
        before = 'def route():\n    return "/api/items"\n'
        (self.workspace / "app.py").write_text(before)
        result = await self.approve(self.v4a_call(
            "provider-error", sha256(before),
            patch('@@\n def route():\n-    return "/api.items"\n+    return "/api/v2/items"'),
        ))
        message = Message("result", MessageRole.TOOL, (ToolResultBlock(result),))
        openai = OpenAICompatibleModelProvider._message_to_provider(message, {})
        anthropic = AnthropicMessagesModelProvider("https://example.test", "model", "key")._message_to_provider(
            message, {}, frozenset(),
        )
        for raw in (openai["content"], anthropic["content"][0]["content"]):
            payload = json.loads(raw)
            details = payload["data"]["patch_conflict"]
            self.assertEqual(details["candidate_locations"][0]["line_number"], 2)
            self.assertEqual(details["patch_line_kind"], "deletion")
            self.assertEqual(details["expected_context"], '    return "/api.items"')
            self.assertEqual(details["actual_line"], '    return "/api/items"')
            self.assertIn("Changing only '+'", payload["message"])
            self.assertEqual(details["snapshot_sha256"], hashlib.sha256(before.encode()).hexdigest())
            self.assertNotIn("recovery_kind", payload)

    async def test_wrong_plus_only_retry_stays_atomic_until_old_side_is_corrected(self):
        before = 'def route():\n    return "/api/items"\n'
        target = self.workspace / "app.py"
        target.write_text(before)
        body = '@@\n def route():\n-    return "/api.items"\n+    return "/api/v2/items"'
        for call_id, attempt in (("first", body), ("plus-only", body.replace("/api/v2/items", "/api/items/v2"))):
            failed = await self.approve(self.v4a_call(call_id, sha256(before), patch(attempt)))
            self.assertEqual(failed.error_code, "PATCH_CONFLICT")
            self.assertEqual(failed.data["patch_conflict"]["patch_line_kind"], "deletion")
            self.assertTrue(failed.data["no_edits_applied"])
            self.assertEqual(target.read_text(), before)
        self.assertFalse((await self.application.kernel.get_task(self.task.task_id)).mutation_journal)
        fixed = body.replace('-    return "/api.items"', '-    return "/api/items"')
        succeeded = await self.approve(self.v4a_call("fixed-old", sha256(before), patch(fixed)))
        self.assertTrue(succeeded.ok)
        self.assertEqual(target.read_text(), 'def route():\n    return "/api/v2/items"\n')
        self.assertEqual(len((await self.application.kernel.get_task(self.task.task_id)).mutation_journal), 1)

    async def test_hash_conflict_does_not_disclose_context_or_apply_anything(self):
        before = "private_content = 1\n"
        (self.workspace / "app.py").write_text(before)
        result = await self.approve(self.v4a_call(
            "wrong-hash", "0" * 64, patch("@@\n-private_content = 1\n+private_content = 2"),
        ))
        self.assertEqual(result.error_code, "CONFLICT")
        self.assertNotIn("patch_conflict", result.data or {})
        self.assertNotIn("private_content", json.dumps(result.to_model_data()))
