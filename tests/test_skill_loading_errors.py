"""Replay empty-resource failure, actionable errors, and correction via the model loop."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tsm_agt.adapters.builtin.skill_tools import CoreSkillToolProvider
from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.adapters.skill_catalog import SkillCatalog
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core.skills import SkillRegistry
from tsm_agt.core.tool import InvalidToolArguments, validate_tool_arguments
from tsm_agt.ports import ToolCall, ToolRecoveryKind, ToolResultBlock
from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response


class SkillLoadingErrorsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = UserSkillsStore(self.root / "home")
        self.store.install_upload({
            "SKILL.md": b"---\nname: user-demo\ndescription: Demo instructions\n---\nDemo body\n",
            "references/valid.md": b"Reference body",
            "references/binary.txt": b"\xff\xfe\x00",
            "references/large.md": b"x" * 256_001,
        })
        self.provider = CoreSkillToolProvider(SkillRegistry(), self.store, catalog=SkillCatalog(self.store))
        await self.provider.start(None)

    async def load(self, name="verify-change", **args):
        return await self.provider.invoke(ToolCall("load", "skill.load", {"name": name, **args}), None)

    async def test_empty_resource_is_invalid_and_default_still_loads_for_both_sources(self):
        spec = next(t for t in await self.provider.list_tools() if t.name == "skill.load")
        for name in ["verify-change", "user-demo"]:
            for resource in ["", "   ", None, 7]:
                with self.subTest(name=name, resource=resource):
                    args = {"name": name, "resource": resource}
                    with self.assertRaises(InvalidToolArguments) as raised:
                        validate_tool_arguments(spec, args)
                    self.assertIn("resource", str(raised.exception))
                    self.assertIn("SKILL.md", str(raised.exception))
                    result = await self.load(name, resource=resource)
                    self.assertEqual(result.error_code, "INVALID_PARAM")
                    self.assertEqual(result.effective_recovery_kind, ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS)
                    self.assertIn("SKILL.md", result.hint)
                    self.assertIsNone(result.data)
            implicit, explicit = await self.load(name), await self.load(name, resource="SKILL.md")
            self.assertTrue(implicit.ok)
            self.assertEqual(implicit.data, explicit.data)

    async def test_missing_file_directory_and_forbidden_paths_are_distinct(self):
        for name in ["verify-change", "user-demo"]:
            for resource, code in [("references/missing.md", "SKILL_RESOURCE_NOT_FOUND"),
                                   ("references", "SKILL_RESOURCE_NOT_FILE"),
                                   ("../SKILL.md", "SKILL_RESOURCE_FORBIDDEN"),
                                   (".hidden", "SKILL_RESOURCE_FORBIDDEN"),
                                   ("/etc/passwd", "SKILL_RESOURCE_FORBIDDEN"),
                                   ("references\\valid.md", "SKILL_RESOURCE_FORBIDDEN")]:
                with self.subTest(name=name, resource=resource):
                    result = await self.load(name, resource=resource)
                    self.assertEqual(result.error_code, code)
                    self.assertTrue(result.hint)
                    if code.endswith("FORBIDDEN"):
                        self.assertEqual(result.effective_recovery_kind, ToolRecoveryKind.TERMINAL)

    async def test_symlink_escape_remains_forbidden(self):
        skill = next(s for s in self.provider.registry.list_all() if s.name == "user-demo")
        outside = self.root / "outside.txt"
        outside.write_text("must not be returned")
        (Path(skill.plan_path).parent / "escape.md").symlink_to(outside)
        result = await self.load("user-demo", resource="escape.md")
        self.assertEqual(result.error_code, "SKILL_RESOURCE_FORBIDDEN")
        self.assertNotIn("must not be returned", str(result.to_model_data()))

    async def test_encoding_size_permissions_and_os_errors_have_explicit_results(self):
        for resource, code in [("references/binary.txt", "SKILL_RESOURCE_ENCODING_ERROR"),
                               ("references/large.md", "SKILL_RESOURCE_TOO_LARGE")]:
            result = await self.load("user-demo", resource=resource)
            self.assertEqual(result.error_code, code)
            self.assertTrue(result.hint)
        for error, code, recovery in [
            (PermissionError("unreadable"), "SKILL_RESOURCE_PERMISSION_DENIED", ToolRecoveryKind.USER_ACTION_REQUIRED),
            (OSError("disk unavailable"), "SKILL_RESOURCE_READ_FAILED", ToolRecoveryKind.TERMINAL),
        ]:
            with patch.object(self.provider.catalog, "read", side_effect=error):
                result = await self.load()
            self.assertEqual(result.error_code, code)
            self.assertEqual(result.effective_recovery_kind, recovery)

    async def test_unknown_skill_and_changed_catalog_offer_current_catalog(self):
        result = await self.load("missing")
        self.assertEqual(result.error_code, "SKILL_NOT_FOUND")
        self.assertTrue(result.data["skills"])
        result = await self.load(catalog_revision="stale")
        self.assertEqual(result.error_code, "SKILL_CATALOG_CHANGED")
        self.assertTrue(result.data["revision"])
        self.assertTrue(result.hint)

    async def test_model_receives_empty_resource_error_and_can_correct_the_next_call(self):
        model = ScriptedModel([
            tool_response("bad", "bad-load", "skill.load", {"name": "verify-change", "resource": ""}),
            tool_response("fixed", "fixed-load", "skill.load", {"name": "verify-change", "resource": "SKILL.md"}),
            text_response("done", "Instructions loaded after correcting resource."),
        ])
        with patch("tsm_agt.bootstrap.composition.UserSkillsStore", return_value=self.store):
            app = compose_fixture_application(enable_skills=True, model_adapter=model, tool_adapters=())
        await app.start()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing(app, self.root)
        await app.kernel.run_agent_turn(task.task_id, "Read the verification instructions")
        errors = [b.result for m in model.requests[1].messages for b in m.content
                  if isinstance(b, ToolResultBlock) and b.result.call_id == "bad-load"]
        self.assertEqual(len(errors), 1)
        failure = errors[0]
        self.assertEqual(failure.error_code, "INVALID_PARAM")
        self.assertEqual(failure.effective_recovery_kind, ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS)
        self.assertIn("resource", failure.to_model_data()["message"])
        self.assertIn("SKILL.md", failure.to_model_data()["message"])
        results = [b.result for m in model.requests[2].messages for b in m.content
                   if isinstance(b, ToolResultBlock) and b.result.call_id == "fixed-load"]
        self.assertTrue(results[0].ok)
        self.assertIn("# Verify a change", results[0].data["content"])
