"""Skill visibility, bounded prompt text and recovery through real tool paths."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.builtin.skill_tools import CoreSkillToolProvider
from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.adapters.skill_catalog import SkillCatalog
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.bootstrap.context_configuration import load_context_configuration, context_window_manager
from tsm_agt.core.context_window import window_identity
from tsm_agt.core.conversation_loop.loop import select_model_tools
from tsm_agt.core.skills import SkillCatalogBudget, SkillRegistry, render_skill_catalog, skill_catalog_estimated_tokens
from tsm_agt.ports import ToolCall, ToolResultBlock
from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response


def catalog(count, description="检查接口兼容性，并执行相关测试。" * 40):
    return {"kind": "skill_catalog", "revision": "a" * 64,
            "skills": [{"name": f"task-{i:04}", "description": description,
                        "source": "user", "location": f"/private/skills/{i}/SKILL.md", "overrides": []}
                       for i in range(count)]}


class SkillCatalogBudgetTest(unittest.TestCase):
    def test_small_catalog_retains_full_purposes_without_bookkeeping(self):
        data = catalog(3, "检查接口兼容性")
        result = render_skill_catalog(data, 1000)
        self.assertFalse(result["has_more"])
        self.assertFalse(result["descriptions_shortened"])
        self.assertEqual(result["skills"], [{"name": row["name"], "description": row["description"]} for row in data["skills"]])
        self.assertIn("location", data["skills"][0], "projection must not mutate the registry snapshot")

    def test_descriptions_share_space_and_preserve_even_the_last_name(self):
        data = catalog(100)
        result = render_skill_catalog(data, 1800)
        self.assertFalse(result["has_more"])
        self.assertTrue(result["descriptions_shortened"])
        self.assertEqual([row["name"] for row in result["skills"]], [row["name"] for row in data["skills"]])
        lengths = [len(row["description"]) for row in result["skills"]]
        self.assertEqual(len(set(lengths)), 1)
        self.assertGreater(lengths[-1], 0)
        self.assertLess(lengths[0], len(data["skills"][0]["description"]))
        self.assertLessEqual(skill_catalog_estimated_tokens(result), 1800)

    def test_names_survive_when_descriptions_do_not_fit(self):
        data = catalog(100)
        minimum = render_skill_catalog(data, 800)
        self.assertEqual(len(minimum["skills"]), 100)
        self.assertFalse(minimum["has_more"])
        self.assertTrue(all("description" not in row for row in minimum["skills"]))

    def test_wrapper_boundary_does_not_overflow_when_has_more_becomes_false(self):
        data = catalog(5, "")
        for budget in range(70, 210):
            result = render_skill_catalog(data, budget)
            if result is not None:
                self.assertLessEqual(skill_catalog_estimated_tokens(result), budget)

    def test_extreme_catalog_reports_omissions_and_skips_one_oversized_name(self):
        data = catalog(1000)
        data["skills"].insert(0, {"name": "a" * 10000, "description": "oversized"})
        result = render_skill_catalog(data, 500)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["total_skills"], 1001)
        self.assertEqual(result["discovery_tool"], "skill.discover")
        self.assertEqual(result["skills"][0]["name"], "task-0000")
        self.assertLessEqual(skill_catalog_estimated_tokens(result), 500)
        self.assertIsNone(render_skill_catalog(data, 1))

    def test_chinese_ascii_and_escaping_are_bounded_using_serialized_text(self):
        for purpose in ["用途说明" * 300, 'ASCII "quoted" \\ description ' * 100, "😀检查\n兼容性\t" * 100]:
            for count in [0, 3, 100, 1000]:
                for budget in [1, 100, 800, 3000]:
                    with self.subTest(purpose=purpose[:10], count=count, budget=budget):
                        result = render_skill_catalog(catalog(count, purpose), budget)
                        if result is not None:
                            self.assertLessEqual(skill_catalog_estimated_tokens(result), budget)

    def test_configuration_scales_by_window_and_rejects_invalid_values(self):
        settings = load_context_configuration({"context": {"skill_catalog_ratio": 0.03, "skill_catalog_fallback_tokens": 3000}})
        policy = context_window_manager(settings).skill_catalog_budget
        self.assertEqual(policy.tokens_for(128000), 3840)
        self.assertEqual(policy.tokens_for(32000), 960)
        self.assertEqual(policy.tokens_for(0), 3000)
        self.assertEqual(settings.sources["context.skill_catalog_ratio"], "yaml")
        for key, values in {"skill_catalog_ratio": [0, -1, 1, True, "0.02", float("nan")],
                            "skill_catalog_fallback_tokens": [0, -1, True, 1.5]}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    load_context_configuration({"context": {key: value}})


class SkillCatalogRequestTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = UserSkillsStore(self.root / "home")
        for i in range(120):
            text = f"---\nname: task-{i:04}\ndescription: " + "检查兼容性并执行验证。" * 50 + f"\n---\nInstructions {i}\n"
            self.store.install_upload({"SKILL.md": text.encode()})
        self.provider = CoreSkillToolProvider(SkillRegistry(), self.store, catalog=SkillCatalog(self.store))
        await self.provider.start(None)

    async def test_projection_is_deterministic_profile_scoped_and_does_not_mutate_registry(self):
        raw = await self.provider.list_tools()
        before = json.dumps([tool.to_data() for tool in raw], ensure_ascii=False)
        first = select_model_tools(raw, user_text="first", model_call=1, context_window=32000)
        later = select_model_tools(raw, user_text="different request", model_call=90, context_window=32000)
        larger = select_model_tools(raw, user_text="first", model_call=1, context_window=128000)
        small_catalog = next(t.model_context for t in first if t.name == "skill.load")
        large_catalog = next(t.model_context for t in larger if t.name == "skill.load")
        self.assertEqual(first, later)
        self.assertEqual(small_catalog["budget_tokens"], 640)
        self.assertEqual(large_catalog["budget_tokens"], 2560)
        self.assertGreater(len(large_catalog["skills"]), len(small_catalog["skills"]))
        self.assertEqual(json.dumps([tool.to_data() for tool in raw], ensure_ascii=False), before)
        def identity(tools):
            return window_identity(task_id="task", profile_id=None, workspace="/workspace", manifest_hash="m", tools=tools, reasoning_effort=None)
        self.assertEqual(identity(first), identity(later))
        self.assertNotEqual(identity(first), identity(larger))
        changed = tuple(replace(t, model_context={**dict(t.model_context), "revision": "new"}) if t.name == "skill.load" else t for t in first)
        self.assertEqual(identity(first), identity(changed), "catalog revisions must not reset the window")

    async def test_visible_name_loads_directly_and_prefix_survives_next_sample(self):
        model = ScriptedModel([tool_response("load", "load", "skill.load", {"name": "task-0000"}), text_response("done", "Loaded.")])
        with patch("tsm_agt.bootstrap.composition.UserSkillsStore", return_value=self.store):
            app = compose_fixture_application(enable_skills=True, model_adapter=model, tool_adapters=())
        await app.start()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing(app, self.root)
        await app.kernel.run_agent_turn(task.task_id, "Use task-0000")
        self.assertEqual(len(model.requests), 2, "no discovery round is forced")
        first, second = model.requests
        spec = next(t for t in first.tools if t.name == "skill.load")
        self.assertEqual(spec.model_context["budget_tokens"], 640)
        actual_catalog = next(m for m in first.messages if m.message_id == "tool-catalog-context-skill.load")
        from tsm_agt.core.skills import SKILL_CATALOG_LABEL
        self.assertEqual(actual_catalog.text, SKILL_CATALOG_LABEL + json.dumps(dict(spec.model_context), ensure_ascii=False, sort_keys=True))
        self.assertNotIn("enum", spec.parameters["properties"]["name"])
        self.assertEqual(first.messages, second.messages[:len(first.messages)])
        self.assertEqual(first.tools, second.tools)
        loaded = [b.result for m in second.messages for b in m.content if isinstance(b, ToolResultBlock)]
        self.assertTrue(any(r.ok and "Instructions 0" in str(r.data) for r in loaded))

    async def test_omitted_name_is_discoverable_pageable_and_loadable(self):
        model = ScriptedModel([tool_response("discover", "find", "skill.discover", {"query": "task-0119"}),
                               tool_response("load", "load", "skill.load", {"name": "task-0119"}), text_response("done", "Loaded.")])
        with patch("tsm_agt.bootstrap.composition.UserSkillsStore", return_value=self.store):
            app = compose_fixture_application(enable_skills=True, model_adapter=model, tool_adapters=())
        await app.start()
        self.addAsyncCleanup(app.registry.stop_all)
        task = await _executing(app, self.root)
        await app.kernel.run_agent_turn(task.task_id, "Find task-0119")
        initial = next(t.model_context for t in model.requests[0].tools if t.name == "skill.load")
        self.assertNotIn("task-0119", [row["name"] for row in initial["skills"]])
        results = [b.result for m in model.requests[-1].messages for b in m.content if isinstance(b, ToolResultBlock)]
        found = next(r for r in results if r.call_id == "find")
        self.assertEqual(found.data["skills"][0]["name"], "task-0119")
        self.assertIn("location", found.data["skills"][0])
        self.assertTrue(any(r.ok and "Instructions 119" in str(r.data) for r in results))
        page = await self.provider.invoke(ToolCall("page", "skill.discover", {"query": "task-", "limit": 2}), None)
        self.assertEqual(page.data["next_offset"], 2)
        following = await self.provider.invoke(ToolCall("next", "skill.discover", {"query": "task-", "offset": 2, "limit": 2}), None)
        self.assertEqual(following.data["skills"][0]["name"], "task-0002")
        error = await self.provider.invoke(ToolCall("bad", "skill.load", {"name": "missing"}), None)
        self.assertNotIn("skills", error.data)
        self.assertEqual(error.data["discovery_tool"], "skill.discover")
