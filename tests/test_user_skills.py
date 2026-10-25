from __future__ import annotations

import importlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core.skills import SkillRegistry
from tsm_agt.ports import RuntimeStorePort, ToolResultBlock
from tests.test_conversation_loop import _executing, ScriptedModel, text_response, tool_response


def manifest(name="demo-plan", description="Use for demo documents") -> bytes:
    return (
        f"---\nname: {name}\ndescription: {description}\n---\n"
        "Write a demo document with Goal, Steps, and Acceptance sections.\n"
        "Read references/example.md for the example.\n"
    ).encode()


class UserSkillsStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = UserSkillsStore(self.root / "home")

    def test_install_persists_name_and_resources_and_survives_new_registry(self):
        skill = self.store.install_upload({
            "original-folder/SKILL.md": manifest(),
            "original-folder/references/example.md": b"example content",
        })
        expected = self.root / "home/.tsm-agent/skills/demo-plan/SKILL.md"
        self.assertEqual(Path(skill.plan_path), expected.resolve())
        registry = SkillRegistry()
        UserSkillsStore(self.root / "home").refresh(registry)
        self.assertEqual([s.name for s in registry.list_all()], ["demo-plan"])
        self.assertEqual(registry.get_session_enabled("a-new-chat"), ())
        self.assertEqual(self.store.read(registry.get("demo-plan"), "references/example.md")["content"], "example content")
        with self.assertRaises(FileExistsError):
            self.store.install_upload({"SKILL.md": manifest()})
        self.assertEqual(expected.read_bytes(), manifest())

    def test_identical_retry_is_successful_but_changed_content_never_overwrites(self):
        uploaded = {"demo/SKILL.md": manifest(), "demo/references/example.md": b"original"}
        skill = self.store.install_upload(uploaded)
        before = Path(skill.plan_path).stat().st_mtime_ns
        repeated = self.store.install_upload(uploaded)
        self.assertEqual(repeated, skill)
        self.assertEqual(Path(skill.plan_path).stat().st_mtime_ns, before)
        with self.assertRaisesRegex(FileExistsError, "内容不同"):
            self.store.install_upload({**uploaded, "demo/references/example.md": b"changed"})
        self.assertEqual((Path(skill.plan_path).parent / "references/example.md").read_bytes(), b"original")

    def test_finder_metadata_is_ignored_and_template_assets_are_preserved(self):
        skill = self.store.install_upload({
            "demo/SKILL.md": manifest(), "demo/.DS_Store": b"Finder",
            "demo/.git/objects/ignored": b"history", "__MACOSX/._demo": b"metadata",
            "demo/.templates/example.md": b"template",
        })
        target = Path(skill.plan_path).parent
        self.assertFalse((target / ".DS_Store").exists())
        self.assertFalse((target / ".git").exists())
        self.assertEqual((target / ".templates/example.md").read_bytes(), b"template")

    def test_original_yaml_only_demo_installs_and_is_discovered(self):
        legacy = (
            "---\nid: plan.local_demo\nname: local.demo.plan\n"
            "description: 原始 demo PLAN skill\nkind: plan\n"
            "tags:\n  - demo\n  - plan\n"
            "steps:\n  - tool: echo\n    args:\n      text: demo\n"
        ).encode()
        skill = self.store.install_upload({"demo_plan/SKILL.md": legacy})
        self.assertEqual(skill.name, "local.demo.plan")
        self.assertEqual(Path(skill.plan_path).read_bytes(), legacy)
        registry = SkillRegistry()
        self.store.refresh(registry)
        self.assertEqual(registry.get("local.demo.plan"), skill)
        self.assertEqual(self.store.read(skill)["content"], legacy.decode())

    def test_unclosed_markdown_frontmatter_reports_uploaded_filename_and_fix(self):
        broken = b"---\nname: demo-plan\ndescription: Demo\n\n# Instructions\nWrite a demo document.\n"
        with self.assertRaisesRegex(ValueError, r"demo/SKILL.md: .*---"):
            self.store.install_upload({"demo/SKILL.md": broken})
        self.assertEqual(list(self.store.install_directory.iterdir()), [])

    def test_frontmatter_accepts_utf8_bom_crlf_and_yaml_document_end(self):
        for terminator in ("---", "..."):
            with self.subTest(terminator=terminator):
                path = self.root / "SKILL.md"
                path.write_bytes((
                    "\ufeff---\r\nname: demo-plan\r\ndescription: >\r\n"
                    f"  Demo with folded description\r\n{terminator}\r\n\r\n"
                    "# Instructions\r\nWrite a demo document.\r\n"
                ).encode("utf-8"))
                skill = self.store.parse(path)
                self.assertEqual(skill.description, "Demo with folded description")

    def test_repository_demo_installs_as_a_standard_instruction_skill(self):
        source = Path(__file__).resolve().parents[1] / "skills/demo_plan"
        skill = self.store.install_path(source / "SKILL.md")
        content = self.store.read(skill)["content"]
        self.assertEqual(skill.name, "local.demo.plan")
        self.assertIn("## 执行步骤", content)
        self.assertTrue((Path(skill.plan_path).parent / "README.md").is_file())

    def test_multiple_sources_deduplicate_paths_and_keep_external_files_unchanged(self):
        kiro = self.root / ".kiro/skills"
        other = self.root / "other-skills"
        for directory, name in ((kiro, "kiro-demo"), (other, "other-demo")):
            path = directory / name / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_bytes(manifest(name))
        self.store.save_directories([str(kiro), str(other), str(kiro)])
        restored = UserSkillsStore(self.root / "home")
        self.assertEqual(len(restored.discovery_directories()), 3)
        self.assertEqual({s.name for s in restored.discover()}, {"kiro-demo", "other-demo"})
        self.assertFalse(restored.install_directory.exists())
        self.assertEqual((kiro / "kiro-demo/SKILL.md").read_bytes(), manifest("kiro-demo"))
        registry = SkillRegistry()
        restored.refresh(registry)
        restored.save_directories([str(kiro)])
        restored.refresh(registry)
        self.assertIsNone(registry.get("other-demo"))
        self.assertTrue((other / "other-demo/SKILL.md").exists())

    def test_duplicate_names_use_install_directory_and_report_bad_manifests(self):
        self.store.install_upload({"SKILL.md": manifest()})
        external = self.root / "external"
        external.mkdir()
        (external / "SKILL.md").write_bytes(manifest(description="different"))
        bad = external / "invalid/SKILL.md"
        bad.parent.mkdir()
        bad.write_text("---\nname: [oops\n---\n")
        self.store.save_directories([str(external)])
        skills = self.store.discover()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].description, "Use for demo documents")
        self.assertEqual(len(self.store.issues), 2)

    def test_external_symlink_catalog_is_discovered_without_recursion_cycles(self):
        source = self.root / "shared/demo"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_bytes(manifest())
        external = self.root / "kiro/skills"
        external.mkdir(parents=True)
        (external / "demo").symlink_to(source, target_is_directory=True)
        (source / "loop").symlink_to(external, target_is_directory=True)
        self.store.save_directories([str(external)])
        self.assertEqual([s.name for s in self.store.discover()], ["demo-plan"])

    def test_reject_upload_traversal_and_resource_symlink_escape(self):
        with self.assertRaises(ValueError):
            self.store.install_upload({"../SKILL.md": manifest()})
        self.assertFalse((self.root / "SKILL.md").exists())
        skill = self.store.install_upload({"SKILL.md": manifest()})
        outside = self.root / "private.txt"
        outside.write_text("outside")
        (Path(skill.plan_path).parent / "reference.md").symlink_to(outside)
        for resource in ("../private.txt", "reference.md", str(outside)):
            with self.assertRaises(ValueError):
                self.store.read(skill, resource)

    def test_settings_preserve_unrelated_values_and_reject_invalid_paths(self):
        self.store.directory.mkdir(parents=True)
        self.store.settings_path.write_text('{"theme":"light"}')
        self.store.save_directories([str(self.root)])
        self.assertEqual(self.store.settings()["theme"], "light")
        before = self.store.settings_path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.save_directories([str(self.root / "missing")])
        self.assertEqual(self.store.settings_path.read_bytes(), before)


class UserSkillsRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_sessions_load_user_and_external_skills_without_enablement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = UserSkillsStore(root / "home")
            store.install_upload({"SKILL.md": manifest(), "references/example.md": b"example"})
            external = root / "kiro/skills/kiro-demo"
            external.mkdir(parents=True)
            (external / "SKILL.md").write_bytes(manifest("kiro-demo"))
            store.save_directories([str(external.parent)])
            model = ScriptedModel([
                tool_response("load-demo", "load-1", "skill.load", {"name": "demo-plan"}),
                tool_response("load-ref", "load-2", "skill.load", {"name": "demo-plan", "resource": "references/example.md"}),
                text_response("done-1", "Goal, Steps, Acceptance"),
                tool_response("load-kiro", "load-3", "skill.load", {"name": "kiro-demo"}),
                text_response("done-2", "Loaded external skill"),
            ])
            with patch("tsm_agt.bootstrap.composition.UserSkillsStore", return_value=store):
                app = compose_fixture_application(enable_skills=True, model_adapter=model, tool_adapters=())
            await app.registry.start_all()
            self.addAsyncCleanup(app.registry.stop_all)
            for index, name in enumerate(("demo-plan", "kiro-demo")):
                task = await _executing(app, root, f"skill-chat-{index}")
                session = await app.kernel.get_session(task.session_id)
                self.assertEqual(session.enabled_skills, ())
                await app.kernel.run_agent_turn(task.task_id, f"Use {name}")
                events = await app.registry.require(RuntimeStorePort).read_events(task.task_id)
                loaded = [event for event in events if event.event_type == "tool.completed" and event.payload.get("result", {}).get("meta", {}).get("runtime_kind") == "skill"]
                self.assertTrue(loaded)
            advertised = next(tool for tool in model.requests[0].tools if tool.name == "skill.load")
            self.assertIn("demo-plan", advertised.description)
            self.assertIn("kiro-demo", advertised.description)
            results = [block.result for request in model.requests for message in request.messages for block in message.content if isinstance(block, ToolResultBlock)]
            self.assertTrue(any(result.ok and result.data.get("content") == "example" for result in results))


class UserSkillsWebTest(unittest.IsolatedAsyncioTestCase):
    async def test_upload_registers_skill_for_the_next_model_request_without_restart(self):
        web = importlib.import_module("tsm_agt.web.app")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = UserSkillsStore(root / "home")
            model = ScriptedModel([
                tool_response("load", "load-uploaded", "skill.load", {"name": "demo-plan"}),
                text_response("done", "Loaded uploaded instructions"),
            ])
            with patch("tsm_agt.bootstrap.composition.UserSkillsStore", return_value=store):
                application = compose_fixture_application(enable_skills=True, model_adapter=model, tool_adapters=())
            await application.registry.start_all()
            self.addAsyncCleanup(application.registry.stop_all)

            class Server:
                _client = type("Client", (), {"application": application})()

                def _call(self, coroutine):
                    # Catalog refresh has no suspension points or DB work.
                    try:
                        coroutine.send(None)
                    except StopIteration as result:
                        return result.value
                    raise AssertionError("unexpected suspension")

            with patch.object(web, "runtime_server", Server()):
                result = await web.upload_skill_directory(files=[UploadFile(filename="demo/SKILL.md", file=io.BytesIO(manifest()))])
                self.assertTrue(result["installed"])
                self.assertIsNotNone(application.kernel._dependencies.skill_registry.get("demo-plan"))
            task = await _executing(application, root)
            await application.kernel.run_agent_turn(task.task_id, "Use demo-plan")
            results = [block.result for request in model.requests for message in request.messages for block in message.content if isinstance(block, ToolResultBlock)]
            self.assertTrue(any(result.ok and "Write a demo document" in result.data.get("content", "") for result in results))

    async def test_refresh_failure_after_commit_preserves_installation_receipt(self):
        web = importlib.import_module("tsm_agt.web.app")
        with tempfile.TemporaryDirectory() as directory:
            store = UserSkillsStore(Path(directory))
            with patch.object(web, "user_skills_store", return_value=store), patch.object(web, "refresh_skill_catalog", side_effect=RuntimeError("catalog unavailable")):
                result = await web.upload_skill_directory(files=[UploadFile(filename="demo/SKILL.md", file=io.BytesIO(manifest()))])
                self.assertTrue(result["installed"])
                self.assertTrue(Path(result["skill"]["plan_path"]).exists())
                self.assertTrue(result["warnings"])

    async def test_unclosed_frontmatter_returns_actionable_json_error(self):
        web = importlib.import_module("tsm_agt.web.app")
        with tempfile.TemporaryDirectory() as directory:
            store = UserSkillsStore(Path(directory))
            with patch.object(web, "runtime_server", None), patch.object(web, "user_skills_store", return_value=store):
                with self.assertRaises(HTTPException) as caught:
                    await web.upload_skill_directory(files=[UploadFile(
                        filename="demo/SKILL.md",
                        file=io.BytesIO(b"---\nname: demo-plan\ndescription: Demo\nInstructions without a closing delimiter.\n"),
                    )])
                self.assertEqual(caught.exception.status_code, 400)
                self.assertIn("demo/SKILL.md", caught.exception.detail)
                self.assertIn("---", caught.exception.detail)

    async def test_upload_and_settings_are_user_scoped_and_persistent(self):
        web = importlib.import_module("tsm_agt.web.app")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = UserSkillsStore(root / "home")
            external = root / "kiro/skills"
            other = root / "other"
            external.mkdir(parents=True)
            other.mkdir()
            with patch.object(web, "runtime_server", None), patch.object(web, "user_skills_store", return_value=store):
                data = await web.upload_skill_directory(files=[UploadFile(filename="demo/SKILL.md", file=io.BytesIO(manifest()))])
                self.assertTrue(data["installed"])
                self.assertEqual(data["skill"]["name"], "demo-plan")
                self.assertIn(".tsm-agent/skills/demo-plan", data["plan_path"])
                settings = await web.save_skill_settings({"discovery_directories": [str(external), str(other)]})
                self.assertEqual(len(settings["discovery_directories"]), 3)
                self.assertEqual(settings["skills"][0]["name"], "demo-plan")
                self.assertEqual(UserSkillsStore(root / "home").settings(), store.settings())
                repeated = await web.upload_skill_directory(files=[UploadFile(filename="demo/SKILL.md", file=io.BytesIO(manifest()))])
                self.assertTrue(repeated["installed"])
                self.assertEqual(repeated["skill"]["plan_path"], data["plan_path"])
                with self.assertRaises(HTTPException) as caught:
                    await web.upload_skill_directory(files=[UploadFile(filename="demo/SKILL.md", file=io.BytesIO(manifest(description="changed")))])
                self.assertEqual(caught.exception.status_code, 409)
                self.assertIn("可发现 Skills 目录", web.INDEX_HTML)
                self.assertIn("pickSkillDirectories()", web.INDEX_HTML)

    async def test_picker_supports_multiple_paths_and_cancel(self):
        web = importlib.import_module("tsm_agt.web.app")
        result = type("Result", (), {"returncode": 0, "stdout": "/a/skills/\n/b/skills/\n", "stderr": ""})()
        with patch.object(web.sys, "platform", "darwin"), patch.object(web.subprocess, "run", return_value=result) as run:
            self.assertEqual(web.choose_skill_directories(), ["/a/skills/", "/b/skills/"])
            self.assertIn("multiple selections allowed", run.call_args.args[0][-1])


if __name__ == "__main__":
    unittest.main()
