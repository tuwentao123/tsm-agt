from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tsm_agt.adapters.builtin import CoreWorkspaceMutationToolProvider
from tsm_agt.bootstrap import compose_fixture_application, compose_readonly_application
from tsm_agt.core import (
    ApprovalDecision,
    ApprovalKind,
    ApprovalRequired,
    TaskSnapshot,
    TaskState,
)
from tsm_agt.ports import RuntimeStorePort, RuntimeUnitOfWork, ToolRecoveryKind
from tsm_agt.ports import ToolCall


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CoreApplyPatchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.application = compose_fixture_application(
            tool_adapters=(CoreWorkspaceMutationToolProvider(),)
        )
        await self.application.registry.start_all()
        task = await self.application.kernel.create_task(
            "apply an exact patch", self.workspace, "task-apply-patch"
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await self.application.kernel.transition_task(
                task.task_id, state, state.value
            )
        self.task = task

    async def asyncTearDown(self) -> None:
        await self.application.registry.stop_all()
        self.temporary.cleanup()

    def call(
        self, call_id: str, path: str, expected_hash: str | None,
        edits: list[dict[str, str]],
    ) -> ToolCall:
        return ToolCall(call_id, "core.apply_patch", {
            "path": path, "expected_hash": expected_hash, "edits": edits,
        })

    def v4a_call(
        self, call_id: str, expected_hash: str | None, patch: str,
    ) -> ToolCall:
        return ToolCall(call_id, "core.apply_patch_v4a", {
            "expected_hash": expected_hash, "patch": patch,
        })

    async def approve(self, call: ToolCall, turn_id: str = "turn-patch"):
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, turn_id, call
            )
        request = caught.exception.request
        return await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve this exact file patch",
        )

    async def test_modify_waits_for_approval_then_journals_exact_change(self) -> None:
        target = self.workspace / "app.py"
        target.write_text("name = 'old'\n", encoding="utf-8")
        call = self.call(
            "call-modify", "app.py", sha256("name = 'old'\n"),
            [{"old_text": "'old'", "new_text": "'new'"}],
        )

        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-modify", call
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "name = 'old'\n")
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "reviewed exact replacement",
        )

        self.assertTrue(result.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "name = 'new'\n")
        journal = (await self.application.kernel.get_task(self.task.task_id)).mutation_journal
        self.assertEqual(len(journal), 1)
        self.assertEqual(result.data, journal[0].to_data())
        self.assertNotIn("content", result.data)
        self.assertEqual(journal[0].step_id, request.invocation_id)

    async def test_v4a_patch_applies_a_multiline_update(self) -> None:
        target = self.workspace / "app.py"
        before = "title = 'old'\nbody = 'keep'\n"
        target.write_text(before, encoding="utf-8")
        patch = """*** Begin Patch
*** Update File: app.py
@@
-title = 'old'
+title = 'new'
*** End Patch"""

        result = await self.approve(
            self.v4a_call("call-v4a", sha256(before), patch), "turn-v4a"
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["patch_format"], "v4a")
        self.assertEqual(
            target.read_text(encoding="utf-8"), "title = 'new'\nbody = 'keep'\n"
        )

    async def test_v4a_context_only_anchor_sections_locate_a_trailing_insertion(self) -> None:
        # A real Codex/V4A patch uses several @@ sections as pure location
        # anchors (no +/- lines) before the section that actually changes the
        # file. The parser must treat context-only sections as anchors instead
        # of rejecting them as malformed hunks.
        target = self.workspace / "spec.md"
        before = (
            "# Title\n"
            "\n"
            "## Section Two\n"
            "\n"
            "export interface Summary {\n"
            "  id: string;\n"
            "  title: string;\n"
            "\n"
            "  members: Member[];\n"
            "}\n"
        )
        target.write_text(before, encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: spec.md\n"
            "@@\n"
            " ## Section Two\n"
            "@@\n"
            " export interface Summary {\n"
            "   id: string;\n"
            "@@\n"
            "   members: Member[];\n"
            " }\n"
            "+\n"
            "+### Extension\n"
            "+New appended content.\n"
            "*** End Patch"
        )

        result = await self.approve(
            self.v4a_call("call-anchor", sha256(before), patch), "turn-anchor"
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["patch_format"], "v4a")
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            before + "\n### Extension\nNew appended content.\n",
        )

    async def test_v4a_anchor_disambiguates_between_duplicate_regions(self) -> None:
        # Codex-style matching advances a cursor, so a @@ anchor before the
        # change selects the intended one of two identical regions instead of
        # refusing. The anchor "# second region" moves the cursor past the
        # first "x = 0", so the edit lands on the second one.
        target = self.workspace / "dup.py"
        before = "# first region\nx = 0\n# second region\nx = 0\n"
        target.write_text(before, encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: dup.py\n"
            "@@ # second region\n"
            "-x = 0\n"
            "+x = 1\n"
            "*** End Patch"
        )

        result = await self.approve(
            self.v4a_call("call-dup", sha256(before), patch), "turn-dup"
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "# first region\nx = 0\n# second region\nx = 1\n",
        )

    async def test_v4a_without_anchor_edits_the_first_match(self) -> None:
        # Without a disambiguating anchor, Codex matching applies at the first
        # occurrence from the cursor rather than rejecting. This is the chosen
        # tradeoff versus a strict unique-match applier.
        target = self.workspace / "dup2.py"
        before = "x = 0\ny = 1\nx = 0\n"
        target.write_text(before, encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: dup2.py\n"
            "@@\n"
            "-x = 0\n"
            "+x = 2\n"
            "*** End Patch"
        )

        result = await self.approve(
            self.v4a_call("call-dup2", sha256(before), patch), "turn-dup2"
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            target.read_text(encoding="utf-8"), "x = 2\ny = 1\nx = 0\n"
        )

    async def test_v4a_tolerates_blank_lines_the_patch_omitted(self) -> None:
        # A frequent model imperfection: the quoted context drops blank lines
        # that the file actually contains. The matcher skips and preserves
        # those file blanks instead of failing to locate.
        target = self.workspace / "blank.ts"
        before = (
            "interface A {\n"
            "  id: string;\n"
            "\n"
            "  name: string;\n"
            "}\n"
        )
        target.write_text(before, encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: blank.ts\n"
            "@@\n"
            " interface A {\n"
            " id: string;\n"
            " name: string;\n"
            " }\n"
            "+// appended\n"
            "*** End Patch"
        )

        result = await self.approve(
            self.v4a_call("call-blank", sha256(before), patch), "turn-blank"
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            target.read_text(encoding="utf-8"), before + "// appended\n"
        )

    async def test_v4a_conflict_names_the_unmatched_context_line(self) -> None:
        # When the model mis-quotes a context line (e.g. truncates it), the
        # failure must name that exact line so the next attempt can fix it,
        # rather than guessing a location.
        target = self.workspace / "quote.md"
        before = "alpha\nthe full original line of text here\nomega\n"
        target.write_text(before, encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: quote.md\n"
            "@@\n"
            " the full original line\n"
            "+inserted\n"
            "*** End Patch"
        )

        result = await self.approve(
            self.v4a_call("call-quote", sha256(before), patch), "turn-quote"
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PATCH_CONFLICT")
        self.assertIn("the full original line", result.message)
        self.assertIn("quote it exactly", result.message)
        self.assertEqual(target.read_text(encoding="utf-8"), before)

    async def test_v4a_tolerates_trailing_whitespace_differences(self) -> None:
        # The matcher falls back to whitespace-insensitive context matching so
        # a minor indentation drift in the quoted context still locates.
        target = self.workspace / "ws.py"
        before = "def run():\n    value = 1\n    return value\n"
        target.write_text(before, encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: ws.py\n"
            "@@\n"
            " def run():\n"
            "-    value = 1\n"
            "+    value = 2\n"
            "*** End Patch"
        )

        result = await self.approve(
            self.v4a_call("call-ws", sha256(before), patch), "turn-ws"
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "def run():\n    value = 2\n    return value\n",
        )

    async def test_v4a_rejects_patch_with_no_change_hunk(self) -> None:
        # A patch that is only context anchors changes nothing and must be
        # rejected without writing instead of silently succeeding.
        target = self.workspace / "noop.md"
        before = "alpha\nbeta\n"
        target.write_text(before, encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: noop.md\n"
            "@@\n"
            " alpha\n"
            "*** End Patch"
        )

        result = await self.approve(
            self.v4a_call("call-noop", sha256(before), patch), "turn-noop"
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PATCH_INVALID_V4A")
        self.assertEqual(target.read_text(encoding="utf-8"), before)

    async def test_literal_newline_escape_gets_a_specific_recovery_hint(self) -> None:
        target = self.workspace / "multiline.html"
        content = "<div>\n  <button>cancel</button>\n</div>\n"
        target.write_text(content, encoding="utf-8")

        result = await self.approve(
            self.call(
                "call-literal-newline", target.name, sha256(content),
                [{"old_text": r"<div>\n  <button>cancel</button>\n</div>",
                  "new_text": "updated"}],
            ),
            "turn-literal-newline",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PATCH_LITERAL_NEWLINE_ESCAPE")
        self.assertEqual(
            result.recovery_action["required_change"],
            "reissue_v4a_with_actual_newlines",
        )
        self.assertEqual(target.read_text(encoding="utf-8"), content)

    async def test_v4a_syntax_error_reports_no_mutation_and_corrected_call_needs_approval(self) -> None:
        target = self.workspace / "syntax.txt"
        before = "first\nsecond\n"
        target.write_text(before, encoding="utf-8")
        # An illegal line prefix is a genuine syntax error (a context-only @@
        # anchor is now valid, so it can no longer stand in for one here).
        invalid = "*** Begin Patch\n*** Update File: syntax.txt\n@@\n first\n!second\n*** End Patch"
        result = await self.approve(self.v4a_call("bad", sha256(before), invalid), "turn-syntax")
        self.assertEqual(result.error_code, "PATCH_INVALID_V4A")
        self.assertEqual(result.data["execution_state"], "not_started")
        self.assertIn("before any file mutation", result.hint)
        self.assertEqual(target.read_text(encoding="utf-8"), before)

        corrected = "*** Begin Patch\n*** Update File: syntax.txt\n@@\n first\n-second\n+changed\n*** End Patch"
        result = await self.approve(self.v4a_call("good", sha256(before), corrected), "turn-syntax")
        self.assertTrue(result.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "first\nchanged\n")

    async def test_v4a_literal_newline_escape_is_rejected_without_write(self) -> None:
        target = self.workspace / "literal-v4a.txt"
        content = "before\n"
        target.write_text(content, encoding="utf-8")
        escaped_patch = (
            "*** Begin Patch\\n*** Update File: literal-v4a.txt\\n@@\\n"
            "-before\\n+after\\n*** End Patch"
        )

        result = await self.approve(
            self.v4a_call("call-v4a-literal", sha256(content), escaped_patch),
            "turn-v4a-literal",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PATCH_LITERAL_NEWLINE_ESCAPE")
        self.assertEqual(target.read_text(encoding="utf-8"), content)

    async def test_creates_file_with_null_hash_and_empty_old_text(self) -> None:
        result = await self.approve(self.call(
            "call-create", "new.txt", None,
            [{"old_text": "", "new_text": "created\n"}],
        ))
        self.assertTrue(result.ok)
        self.assertEqual((self.workspace / "new.txt").read_text(), "created\n")
        self.assertEqual(result.data["operation"], "create")
        self.assertIsNone(result.data["backup_ref"])

    async def test_v4a_add_creates_after_approval_and_journals(self) -> None:
        patch = "*** Begin Patch\n*** Add File: nested/new.txt\n+first\n+\n+last\n*** End Patch"
        call = self.v4a_call("v4a-add", None, patch)
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(self.task.task_id, "turn-add", call)
        self.assertFalse((self.workspace / "nested/new.txt").exists())
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve creation",
        )
        self.assertTrue(result.ok)
        self.assertEqual((self.workspace / "nested/new.txt").read_text(), "first\n\nlast\n")
        self.assertEqual(result.data["operation"], "create")
        self.assertEqual(result.data["patch_format"], "v4a")
        self.assertEqual(len((await self.application.kernel.get_task(self.task.task_id)).mutation_journal), 1)

    async def test_v4a_add_does_not_overwrite_file_created_before_approval(self) -> None:
        patch = "*** Begin Patch\n*** Add File: existing.txt\n+replacement\n*** End Patch"
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-add-conflict", self.v4a_call("v4a-conflict", None, patch)
            )
        target = self.workspace / "existing.txt"
        target.write_text("original\n")
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve creation",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CONFLICT")
        self.assertEqual(target.read_text(), "original\n")
        self.assertEqual(len((await self.application.kernel.get_task(self.task.task_id)).mutation_journal), 0)

    async def test_v4a_add_rejects_invalid_body_and_wrong_hash(self) -> None:
        invalid = "*** Begin Patch\n*** Add File: new.txt\n+ok\n*** Update File: other.txt\n*** End Patch"
        result = await self.approve(self.v4a_call("v4a-bad-add", None, invalid), "turn-bad-add")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PATCH_INVALID_V4A")
        valid = "*** Begin Patch\n*** Add File: new.txt\n+ok\n*** End Patch"
        result = await self.approve(self.v4a_call("v4a-add-hash", "0" * 64, valid), "turn-add-hash")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")
        self.assertFalse((self.workspace / "new.txt").exists())

    async def test_v4a_add_supports_an_empty_file(self) -> None:
        patch_text = "*** Begin Patch\n*** Add File: empty.txt\n*** End Patch"
        result = await self.approve(self.v4a_call("v4a-empty", None, patch_text), "turn-empty")
        self.assertTrue(result.ok)
        self.assertEqual((self.workspace / "empty.txt").read_bytes(), b"")

    async def test_v4a_update_still_requires_a_hash(self) -> None:
        target = self.workspace / "update.txt"
        target.write_text("before\n")
        patch_text = "*** Begin Patch\n*** Update File: update.txt\n@@\n-before\n+after\n*** End Patch"
        result = await self.approve(self.v4a_call("v4a-update-null", None, patch_text), "turn-null-update")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")
        self.assertEqual(target.read_text(), "before\n")

    async def test_new_nested_path_is_not_misclassified_as_external_write(self) -> None:
        call = self.call(
            "call-create-nested", "docs/new/spec.md", None,
            [{"old_text": "", "new_text": "# spec\n"}],
        )
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-create-nested", call
            )

        # This is still an R1 patch approval, but it is not an imaginary
        # cross-workspace write merely because docs/new does not exist yet.
        self.assertIs(caught.exception.request.kind, ApprovalKind.TOOL_ACTION)
        result = await self.application.kernel.resolve_approval(
            self.task.task_id,
            caught.exception.request.request_id,
            caught.exception.request.payload_hash,
            ApprovalDecision.APPROVE,
            "approve the new in-workspace file",
        )
        self.assertTrue(result.ok)
        self.assertEqual(
            (self.workspace / "docs/new/spec.md").read_text(encoding="utf-8"),
            "# spec\n",
        )

    async def test_legacy_internal_workspace_write_approval_can_complete(self) -> None:
        call = self.call(
            "call-legacy-create", "docs/new/legacy.md", None,
            [{"old_text": "", "new_text": "# legacy\n"}],
        )
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-legacy-create", call
            )
        request = caught.exception.request
        store = self.application.registry.require(RuntimeStorePort)
        stored = await store.load_task(self.task.task_id)
        assert stored is not None
        # Reproduce a record created before create-path containment was fixed.
        legacy_request = replace(
            request,
            kind=ApprovalKind.WORKSPACE_WRITE,
            workspace_access_root=str(self.workspace / "docs/new"),
        )
        legacy_task = replace(
            TaskSnapshot.from_data(stored.data), pending_approval=legacy_request,
        )
        await store.commit(RuntimeUnitOfWork(
            self.task.task_id, stored.version, legacy_task.to_data(), (),
        ))

        result = await self.application.kernel.resolve_approval(
            self.task.task_id,
            request.request_id,
            request.payload_hash,
            ApprovalDecision.APPROVE,
            "approve the previously reviewed exact file patch",
        )
        self.assertTrue(result.ok)
        current = await self.application.kernel.get_task(self.task.task_id)
        self.assertEqual(current.workspace_access_grants, ())

    async def test_user_change_after_read_is_never_overwritten(self) -> None:
        target = self.workspace / "shared.txt"
        target.write_text("agent-read\n", encoding="utf-8")
        call = self.call(
            "call-stale", "shared.txt", sha256("agent-read\n"),
            [{"old_text": "agent-read", "new_text": "agent-write"}],
        )
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-stale", call
            )
        target.write_text("user-write\n", encoding="utf-8")
        request = caught.exception.request
        result = await self.application.kernel.resolve_approval(
            self.task.task_id, request.request_id, request.payload_hash,
            ApprovalDecision.APPROVE, "approve previously reviewed patch",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CONFLICT")
        self.assertIs(
            result.effective_recovery_kind,
            ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
        )
        self.assertEqual(
            result.recovery_action["required_change"],
            "refresh_resource_then_retry",
        )
        self.assertFalse(result.recovery_action["same_call_safe"])
        self.assertEqual(result.data, {"execution_state": "not_started"})
        self.assertIn("version mismatch", result.message)
        self.assertIn("stale or belong to another file", result.hint)
        self.assertIn("do not merely substitute", result.hint)
        self.assertEqual(target.read_text(encoding="utf-8"), "user-write\n")
        self.assertEqual(
            (await self.application.kernel.get_task(self.task.task_id)).mutation_journal, ()
        )

    async def test_wrong_file_hash_is_reported_and_corrected_patch_needs_approval(self) -> None:
        target = self.workspace / "target.txt"
        other = self.workspace / "other.txt"
        target.write_text("target old\n", encoding="utf-8")
        other.write_text("other content\n", encoding="utf-8")
        patch = (
            "*** Begin Patch\n*** Update File: target.txt\n@@\n"
            "-target old\n+target new\n*** End Patch"
        )
        result = await self.approve(
            self.v4a_call("wrong-hash", sha256("other content\n"), patch),
            "turn-wrong-hash",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CONFLICT")
        self.assertEqual(target.read_text(encoding="utf-8"), "target old\n")
        model_result = result.to_model_data()
        self.assertEqual(model_result["data"]["execution_state"], "not_started")
        self.assertEqual(model_result["error_details"]["resource"], "target.txt")
        self.assertEqual(model_result["error_details"]["actual_hash"], sha256("target old\n"))
        self.assertIn("Read the target's current content", model_result["hint"])
        self.assertNotIn("recovery_kind", model_result)
        self.assertNotIn("retryable", model_result)

        # A corrected call is a new mutation and independently needs approval.
        result = await self.approve(
            self.v4a_call("correct-hash", sha256(target.read_text(encoding="utf-8")), patch),
            "turn-wrong-hash",
        )
        self.assertTrue(result.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "target new\n")

    async def test_refreshed_hash_cannot_make_stale_patch_match(self) -> None:
        target = self.workspace / "target.txt"
        target.write_text("already updated\n", encoding="utf-8")
        patch = (
            "*** Begin Patch\n*** Update File: target.txt\n@@\n"
            "-old content\n+updated content\n*** End Patch"
        )
        result = await self.approve(
            self.v4a_call("stale-patch", sha256("already updated\n"), patch),
            "turn-stale-patch",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PATCH_CONFLICT")
        self.assertEqual(result.data, {"execution_state": "not_started"})
        self.assertIn("whether the change is still needed", result.hint)
        self.assertEqual(target.read_text(encoding="utf-8"), "already updated\n")

    async def test_missing_and_ambiguous_exact_text_are_recoverable_conflicts(self) -> None:
        for suffix, content, old_text, message in (
            ("missing", "alpha\n", "beta", "not found"),
            ("ambiguous", "same same\n", "same", "ambiguous"),
        ):
            with self.subTest(case=suffix):
                target = self.workspace / f"{suffix}.txt"
                target.write_text(content, encoding="utf-8")
                result = await self.approve(
                    self.call(
                        f"call-{suffix}", target.name, sha256(content),
                        [{"old_text": old_text, "new_text": "changed"}],
                    ),
                    f"turn-{suffix}",
                )
                self.assertFalse(result.ok)
                # A stale or ambiguous hunk is a correctable input error: the
                # caller re-reads the file and retries instead of the Task dying
                # on a terminal failure.
                self.assertEqual(result.error_code, "PATCH_CONFLICT")
                self.assertIs(
                    result.effective_recovery_kind,
                    ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
                )
                self.assertEqual(
                    result.recovery_action["required_change"],
                    "re_read_file_then_retry",
                )
                self.assertEqual(result.recovery_action["resource"], target.name)
                self.assertFalse(result.recovery_action["same_call_safe"])
                self.assertIn(message, result.message)
                self.assertEqual(target.read_text(encoding="utf-8"), content)

    async def test_empty_old_text_for_existing_file_is_recoverable_conflict(self) -> None:
        target = self.workspace / "existing.txt"
        target.write_text("already here\n", encoding="utf-8")

        result = await self.approve(
            self.call(
                "call-empty-old", target.name, sha256("already here\n"),
                [{"old_text": "", "new_text": "created"}],
            ),
            "turn-empty-old",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PATCH_CONFLICT")
        self.assertEqual(
            result.recovery_action["required_change"], "re_read_file_then_retry"
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "already here\n")

    async def test_multiple_edits_are_applied_sequentially(self) -> None:
        target = self.workspace / "sequence.txt"
        target.write_text("one two\n", encoding="utf-8")
        result = await self.approve(self.call(
            "call-sequence", target.name, sha256("one two\n"),
            [
                {"old_text": "one", "new_text": "first"},
                {"old_text": "first two", "new_text": "done"},
            ],
        ))
        self.assertTrue(result.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "done\n")

    async def test_forbidden_paths_are_rejected_without_journal(self) -> None:
        outside = self.workspace.parent / f"outside-{self.workspace.name}.txt"
        outside.write_text("outside\n", encoding="utf-8")
        link = self.workspace / "link.txt"
        os.symlink(outside, link)
        try:
            cases = (
                ("absolute", str(self.workspace / "absolute.txt"), None),
                ("escape", "../escape.txt", None),
                ("env", ".env", None),
                ("runtime", ".agent/data.txt", None),
                ("symlink", "link.txt", sha256("outside\n")),
            )
            for suffix, path, expected in cases:
                with self.subTest(path=path):
                    result = await self.approve(
                        self.call(
                            f"call-{suffix}", path, expected,
                            [{"old_text": "", "new_text": "bad"}],
                        ),
                        f"turn-{suffix}",
                    )
                    self.assertFalse(result.ok)
            self.assertEqual(outside.read_text(), "outside\n")
            self.assertEqual(
                (await self.application.kernel.get_task(self.task.task_id)).mutation_journal, ()
            )
        finally:
            link.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    async def test_completed_call_is_reused_without_applying_again(self) -> None:
        target = self.workspace / "once.txt"
        target.write_text("before\n", encoding="utf-8")
        call = self.call(
            "call-once", target.name, sha256("before\n"),
            [{"old_text": "before", "new_text": "after"}],
        )
        first = await self.approve(call, "turn-once")
        second = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-once", call
        )
        self.assertEqual(second, first)
        self.assertEqual(target.read_text(), "after\n")
        self.assertEqual(
            len((await self.application.kernel.get_task(self.task.task_id)).mutation_journal), 1
        )

    async def test_readonly_composition_does_not_expose_apply_patch(self) -> None:
        application = compose_readonly_application()
        await application.registry.start_all()
        try:
            names = [tool.name for tool in await application.kernel.list_tools()]
            self.assertNotIn("core.apply_patch", names)
        finally:
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()




class PatchConflictRecoveryWiringTest(unittest.IsolatedAsyncioTestCase):
    """A recoverable patch conflict must actually open a recovery barrier."""

    async def test_patch_conflict_enters_the_recoverable_batch(self) -> None:
        from tsm_agt.core.kernel import Kernel
        from tsm_agt.ports import (
            Message, MessageRole, ToolCallBlock, ToolResult, ToolResultBlock,
        )

        result = ToolResult(
            "call-stale-hunk", False, error_code="PATCH_CONFLICT",
            message="edits[0].old_text was not found in the current file",
            recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
            recovery_action={"required_change": "re_read_file_then_retry"},
        )
        messages = (
            Message("assistant", MessageRole.ASSISTANT, (ToolCallBlock(
                ToolCall(
                    "call-stale-hunk", "core.apply_patch", {"path": "app.py"},
                ),
            ),)),
            Message("results", MessageRole.TOOL, (ToolResultBlock(result),)),
        )

        batch = Kernel._latest_recoverable_tool_batch(messages)

        self.assertEqual([item.call_id for item in batch], ["call-stale-hunk"])
        self.assertEqual(
            batch[0].recovery_action["required_change"],
            "re_read_file_then_retry",
        )


class ToolSchemaBoundsTest(unittest.IsolatedAsyncioTestCase):
    """Enforced argument bounds must be advertised in the tool schema."""

    async def asyncSetUp(self) -> None:
        from datetime import datetime
        from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
        from tsm_agt.ports import AdapterContext

        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        provider = CoreReadOnlyToolProvider()
        await provider.start(AdapterContext({}, lambda _type, _payload: None))
        self.addAsyncCleanup(provider.stop, datetime.now())
        self.specs = {spec.name: spec for spec in await provider.list_tools()}

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_search_tools_advertise_context_and_match_bounds(self) -> None:
        for name in ("core.search_text", "core.grep_search"):
            with self.subTest(tool=name):
                properties = self.specs[name].parameters["properties"]
                self.assertEqual(properties["after_context"]["minimum"], 0)
                self.assertEqual(properties["after_context"]["maximum"], 20)
                self.assertEqual(properties["before_context"]["minimum"], 0)
                self.assertEqual(properties["before_context"]["maximum"], 20)
                self.assertEqual(properties["max_matches"]["minimum"], 1)
                self.assertEqual(properties["max_matches"]["maximum"], 500)

    def test_read_and_list_tools_advertise_bounds(self) -> None:
        read = self.specs["core.read_file"].parameters["properties"]
        self.assertEqual(read["start_line"]["minimum"], 1)
        self.assertEqual(read["max_lines"]["minimum"], 1)
        self.assertEqual(read["max_lines"]["maximum"], 1000)

        listing = self.specs["core.list_files"].parameters["properties"]
        self.assertEqual(listing["limit"]["minimum"], 1)
        self.assertEqual(listing["limit"]["maximum"], 1000)

        find = self.specs["core.find_files"].parameters["properties"]
        self.assertEqual(find["limit"]["minimum"], 1)
        self.assertEqual(find["limit"]["maximum"], 500)
