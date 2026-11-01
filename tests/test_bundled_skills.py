from __future__ import annotations

import json
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tsm_agt.adapters.builtin.skill_tools import CoreSkillToolProvider
from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.adapters.skill_catalog import SkillCatalog
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import PromptTemplate
from tsm_agt.core.context import ContextWindowManager
from tsm_agt.core.skills import SkillRegistry
from tsm_agt.ports import Message, MessageRole, TextBlock, ToolCall, ToolResultBlock
from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response


class BundledSkillsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = UserSkillsStore(self.root / "home")
        self.provider = CoreSkillToolProvider(SkillRegistry(), self.store, catalog=SkillCatalog(self.store))

    async def test_fresh_home_loads_shipped_skill_and_reference_without_installation(self):
        await self.provider.start(None)
        spec = next(t for t in await self.provider.list_tools() if t.name == "skill.load")
        self.assertEqual(spec.parameters["properties"]["name"]["enum"], ["verify-change"])
        revision = spec.model_context["revision"]
        result = await self.provider.invoke(ToolCall("load", "skill.load", {
            "name": "verify-change", "catalog_revision": revision,
        }), None)
        self.assertTrue(result.ok)
        self.assertIn("# Verify a change", result.data["content"])
        self.assertIn("references/workflow.md", result.data["content"])
        reference = await self.provider.invoke(ToolCall("ref", "skill.load", {
            "name": "verify-change", "resource": "references/workflow.md",
        }), None)
        self.assertTrue(reference.ok)
        self.assertIn("terminal", reference.data["content"])
        self.assertEqual(result.data["source"], "builtin")
        self.assertFalse(self.store.directory.exists(), "reading shipped workflows must not write to the user's home")
        self.assertEqual(self.store.installations.list(), [])

    async def test_user_override_disable_restart_and_refresh_have_consistent_sources(self):
        self.store.install_upload({"SKILL.md": b"---\nname: verify-change\ndescription: User verification\n---\nUser instructions\n"})
        skills = self.provider.refresh_catalog()
        selected = next(s for s in skills if s.name == "verify-change")
        self.assertEqual(selected.source, "user")
        self.assertEqual(selected.overrides, ("builtin",))
        self.assertTrue(self.provider.catalog.issues)
        self.store.save_disabled_builtin_skills(["verify-change"])
        restarted = SkillCatalog(UserSkillsStore(self.root / "home"))
        self.assertEqual(restarted.discover()[0].source, "user")
        (Path(selected.plan_path)).unlink()
        self.assertEqual(restarted.discover(), ())
        self.store.save_disabled_builtin_skills([])
        self.assertEqual(self.provider.refresh_catalog()[0].source, "builtin")
        self.assertEqual(self.provider.refresh_catalog()[0].source, "builtin")

    async def test_catalog_revision_prevents_silent_switch_to_another_instruction_body(self):
        await self.provider.start(None)
        old = (await self.provider.list_tools())[0].model_context["revision"]
        self.store.install_upload({"SKILL.md": b"---\nname: verify-change\ndescription: User workflow\n---\nOverride\n"})
        result = await self.provider.invoke(ToolCall("load", "skill.load", {
            "name": "verify-change", "catalog_revision": old,
        }), None)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "SKILL_CATALOG_CHANGED")
        self.assertNotEqual(result.data["revision"], old)

    async def test_builtin_resources_reject_traversal_and_invalid_targets(self):
        await self.provider.start(None)
        for resource in ["../SKILL.md", "/etc/passwd", "references/../../SKILL.md", "references\\file", ".hidden", "references"]:
            result = await self.provider.invoke(ToolCall("bad", "skill.load", {
                "name": "verify-change", "resource": resource,
            }), None)
            self.assertFalse(result.ok, resource)

    async def test_request_boundary_exposes_catalog_separately_without_loading_or_execution(self):
        model = ScriptedModel([text_response("answer", "这轮只分析，不修改。")])
        with patch("tsm_agt.bootstrap.composition.UserSkillsStore", return_value=self.store):
            app = compose_fixture_application(enable_skills=True, model_adapter=model, tool_adapters=())
        await app.start()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing(app, self.root)
        await app.kernel.run_agent_turn(task.task_id, "仅分析验证策略，不修改")
        request = model.requests[0]
        spec = next(t for t in request.tools if t.name == "skill.load")
        catalog = next(m for m in request.messages if m.message_id == "tool-catalog-context-skill.load")
        self.assertIs(catalog.role, MessageRole.USER)
        self.assertIn(spec.model_context["revision"], catalog.text)
        self.assertIn("verify-change", catalog.text)
        self.assertIn("skill-workflow-guidance", " ".join(m.message_id for m in request.messages))
        self.assertNotIn("# Verify a change", " ".join(m.text for m in request.messages))
        self.assertEqual(len(model.requests), 1, "the runtime must not force a continuation or verification call")

    async def test_compaction_regenerates_catalog_from_the_same_advertised_spec(self):
        spec = (await self.provider.list_tools())[0]
        template = PromptTemplate.default()
        history = [Message("user", MessageRole.USER, (TextBlock("修复错误"),))]
        for i in range(16):
            history += [Message(f"u{i}", MessageRole.USER, (TextBlock("old context " * 240),)),
                        Message(f"a{i}", MessageRole.ASSISTANT, (TextBlock("old answer " * 240),))]
        manager = ContextWindowManager()
        prepared = manager.prepare(conversation=tuple(history), tools=(spec,), prompt_template=template,
                                   context_window=22000, max_output_tokens=1000)
        assembly = template.assemble(prepared.messages, (spec,))
        self.assertIsNotNone(prepared.compaction)
        catalog = next(m for m in assembly.messages if m.message_id == "tool-catalog-context-skill.load")
        data = json.loads(catalog.text.split("\n", 1)[1])
        self.assertEqual(data, dict(spec.model_context))
        budget = next(x for x in prepared.budget.allocations if x.name == "tool_catalog")
        self.assertTrue(budget.protected)

    async def test_core_can_load_both_shipped_resources_through_the_normal_tool_path(self):
        model = ScriptedModel([
            tool_response("load", "load-call", "skill.load", {"name": "verify-change"}),
            tool_response("ref", "ref-call", "skill.load", {"name": "verify-change", "resource": "references/workflow.md"}),
            text_response("done", "已读取验证流程。"),
        ])
        with patch("tsm_agt.bootstrap.composition.UserSkillsStore", return_value=self.store):
            app = compose_fixture_application(enable_skills=True, model_adapter=model, tool_adapters=())
        await app.start()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing(app, self.root)
        await app.kernel.run_agent_turn(task.task_id, "读取验证流程")
        self.assertEqual(len(model.requests), 3)
        results = [b.result for m in model.requests[-1].messages for b in m.content if isinstance(b, ToolResultBlock)]
        self.assertTrue(any(r.ok and "# Select evidence, execute, observe" in str(r.data) for r in results))

    async def test_settings_refresh_disable_and_restart_share_the_runtime_catalog(self):
        import importlib
        web = importlib.import_module("tsm_agt.web.app")
        with patch.object(web, "runtime_server", None), patch.object(web, "user_skills_store", return_value=self.store):
            initial = await web.get_skill_settings()
            builtin = next(s for s in initial["skills"] if s["name"] == "verify-change")
            self.assertEqual(builtin["source"], "builtin")
            self.assertEqual(initial["installations"], [])
            disabled = await web.save_skill_settings({"discovery_directories": [], "disabled_builtin_skills": ["verify-change"]})
            self.assertEqual(disabled["skills"], [])
            self.assertEqual(SkillCatalog(UserSkillsStore(self.root / "home")).discover(), ())
            enabled = await web.save_skill_settings({"discovery_directories": [], "disabled_builtin_skills": []})
            self.assertEqual(enabled["skills"][0]["source"], "builtin")

    def test_online_web_refresh_keeps_the_provider_catalog_and_user_store_separate(self):
        import importlib
        web = importlib.import_module("tsm_agt.web.app")
        dependencies = SimpleNamespace(skill_registry=self.provider.registry, tools=(self.provider,))
        kernel = SimpleNamespace(_dependencies=dependencies, dependencies=dependencies)
        runtime = SimpleNamespace(_client=SimpleNamespace(application=SimpleNamespace(kernel=kernel)), _call=asyncio.run)
        with patch.object(web, "runtime_server", runtime):
            self.assertEqual(web.refresh_skill_catalog(self.store)[0].source, "builtin")
            self.store.save_directories([])
            self.assertEqual(web.refresh_skill_catalog(self.store)[0].source, "builtin")
            self.assertIs(self.provider.store, self.store)
            self.store.save_disabled_builtin_skills(["verify-change"])
            self.assertEqual(web.refresh_skill_catalog(self.store), ())

    async def test_changed_catalog_recovers_on_next_model_request_without_choosing_a_workflow(self):
        class ChangingModel(ScriptedModel):
            async def complete(model, request):
                if not model.requests:
                    old = next(t for t in request.tools if t.name == "skill.load").model_context["revision"]
                    self.store.install_upload({"SKILL.md": b"---\nname: verify-change\ndescription: New user workflow\n---\nUser workflow\n"})
                    model._responses[0] = tool_response("load", "stale", "skill.load", {
                        "name": "verify-change", "catalog_revision": old,
                    })
                return await super().complete(request)
        model = ChangingModel([text_response("placeholder", ""), text_response("done", "目录已更新。")])
        with patch("tsm_agt.bootstrap.composition.UserSkillsStore", return_value=self.store):
            app = compose_fixture_application(enable_skills=True, model_adapter=model, tool_adapters=())
        await app.start()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing(app, self.root)
        await app.kernel.run_agent_turn(task.task_id, "查看工作流")
        old = next(t for t in model.requests[0].tools if t.name == "skill.load").model_context
        new = next(t for t in model.requests[1].tools if t.name == "skill.load").model_context
        self.assertNotEqual(new["revision"], old["revision"])
        self.assertEqual(new["skills"][0]["source"], "user")
        self.assertIn(new["revision"], next(m.text for m in model.requests[1].messages if m.message_id == "tool-catalog-context-skill.load"))
