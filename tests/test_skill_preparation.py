from __future__ import annotations

import asyncio
import importlib
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import UploadFile

from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.adapters.builtin.skill_tools import CoreSkillToolProvider
from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.adapters.skill_installations import installation_prompt
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core.skills import SkillRegistry
from tsm_agt.ports import ToolCall, ToolInvocationContext
from tsm_agt.sdk import EngineeringAgentClient
from tests.test_conversation_loop import ScriptedModel, tool_response, text_response
from tests.test_user_skills import manifest

web = importlib.import_module("tsm_agt.web.app")


class SameLoopServer:
    """Exercise the real SDK without a socket or a second SQLite thread."""
    def __init__(self, client):
        self._client = client
        self.launches = []

    def _call(self, coroutine):
        try:
            coroutine.send(None)
        except StopIteration as result:
            return result.value
        coroutine.close()
        raise AssertionError("unexpected asynchronous I/O in read projection")

    def submit_background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.launches.append(task)
        return task


class SkillPreparationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = UserSkillsStore(self.root / "home")
        self.first = self.root / "first"
        self.recent = self.root / "recent"
        self.first.mkdir()
        self.recent.mkdir()
        self.workspaces = [
            {"id": "recent", "name": "Recent", "path": str(self.recent), "last_used_at": "2000-01-02T11:00:00+00:00"},
            {"id": "first", "name": "First", "path": str(self.first), "last_used_at": "2000-01-01T11:00:00+00:00"},
        ]
        for target, value in (
            ("workspace_registry", self.workspaces), ("session_workspace_bindings", {}),
            ("user_skills_store", lambda: self.store),
            ("workspace_registry_path", lambda: self.root / "workspaces.json"),
            ("session_workspace_bindings_path", lambda: self.root / "bindings.json"),
        ):
            patcher = patch.object(web, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def client(self, model, tools=()):
        with patch("tsm_agt.bootstrap.composition.UserSkillsStore", return_value=self.store):
            app = compose_fixture_application(model_adapter=model, tool_adapters=tools)
        client = EngineeringAgentClient(self.first, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        return client

    async def upload(self):
        return await web.upload_skill_directory(files=[UploadFile(filename="demo/SKILL.md", file=io.BytesIO(manifest()))])

    async def complete_launch(self, server, job):
        await asyncio.gather(*server.launches)
        updated = self.store.installations.get(job["id"])
        if updated.get("task_id"):
            await server._client.wait_task(updated["task_id"], timeout=5)
        return self.store.installations.get(job["id"])

    async def test_directory_install_creates_independent_chat_in_recent_workspace_and_reports_ready(self):
        class Model(ScriptedModel):
            async def complete(inner, request):
                if len(inner.requests) == 1:
                    job = self.store.installations.list()[-1]
                    inner._responses.insert(0, tool_response("finish", "finish-call", "skill.installation_finish", {
                        "installation_id": job["id"], "status": "ready",
                        "summary": "无前置环境，指令可用", "checks": ["已读取 SKILL.md；本 Skill 无环境依赖"],
                    }))
                return await super().complete(request)

        model = Model([
            tool_response("load", "load-call", "skill.load", {"name": "demo-plan"}),
            text_response("done", "Skill 安装成功，无需安装环境。"),
        ])
        client = await self.client(model)
        server = SameLoopServer(client)
        with patch.object(web, "runtime_server", server):
            receipt = await self.upload()
            job = await self.complete_launch(server, receipt["installation"])
            result = await client.get_task_result(job["task_id"])
            events = await client.application.kernel._dependencies.store.read_events(job["task_id"])
            self.assertEqual(job["status"], "ready", (job, result, [e.payload for e in events if e.event_type in {"tool.failed", "turn.failed"}]))
            self.assertEqual(job["workspace_id"], "recent")
            task = await client.application.kernel.get_task(job["task_id"])
            self.assertEqual(Path(task.workspace), self.recent.resolve())
            self.assertEqual(task.session_id, job["session_id"])
            session = await client.application.kernel.get_session(job["session_id"])
            self.assertEqual(session.title, "安装 Skill：demo-plan")
            repeated = await self.upload()
            self.assertEqual(repeated["installation"]["id"], job["id"])
            self.assertEqual(len(server.launches), 1)
            self.assertEqual((await web.get_skill_installation(job["id"]))["status"], "ready")
        restored = UserSkillsStore(self.root / "home").installations.get(job["id"])
        self.assertEqual(restored["checks"], job["checks"])
        self.assertIn("不要假定每个 Skill 都需要安装环境", installation_prompt(job))
        self.assertTrue(all(tool.name != "core.process_start" for request in model.requests for tool in request.tools))

    async def test_final_answer_alone_does_not_mark_environment_ready(self):
        client = await self.client(ScriptedModel([text_response("done", "安装好了")]))
        server = SameLoopServer(client)
        with patch.object(web, "runtime_server", server):
            job = (await self.upload())["installation"]
            job = await self.complete_launch(server, job)
            status = await web.get_skill_installation(job["id"])
            self.assertEqual(status["status"], "unverified")
            self.assertTrue(status["task_id"])

    async def test_preparation_commands_use_normal_runtime_approval(self):
        client = await self.client(ScriptedModel([
            tool_response("load", "load-call", "skill.load", {"name": "demo-plan"}),
            tool_response("check", "check-call", "core.run_command", {"argv": ["node", "--version"], "cwd": "."}),
        ]), tools=(CoreProcessToolProvider(),))
        server = SameLoopServer(client)
        with patch.object(web, "runtime_server", server):
            job = await self.complete_launch(server, (await self.upload())["installation"])
            result = await web.get_skill_installation(job["id"])
            events = await client.application.kernel._dependencies.store.read_events(job["task_id"])
            self.assertEqual(result["status"], "awaiting_approval", [e.payload for e in events if e.event_type in {"tool.failed", "turn.failed"}])
            self.assertIn("审批", result["summary"])
            task = await client.get_task_result(job["task_id"])
            self.assertIsNotNone(task.approval)
            self.assertNotEqual(self.store.installations.get(job["id"])["status"], "ready")

    async def test_offline_or_missing_workspace_keeps_pending_receipt_and_retry_starts(self):
        with patch.object(web, "runtime_server", None):
            job = (await self.upload())["installation"]
            self.assertEqual(job["status"], "pending")
            self.assertNotIn("session_id", job)
        client = await self.client(ScriptedModel([text_response("done", "请继续验证")]))
        server = SameLoopServer(client)
        with patch.object(web, "runtime_server", server), patch.object(web, "workspace_registry", []):
            pending = await web.retry_skill_preparation(job["id"])
            self.assertEqual(pending["status"], "pending")
            self.assertEqual(len(server.launches), 0)
        with patch.object(web, "runtime_server", server):
            started = await web.retry_skill_preparation(job["id"])
            self.assertEqual(started["status"], "starting")
            done = await self.complete_launch(server, started)
            self.assertTrue(done["task_id"])

    async def test_activation_recency_survives_registry_reload(self):
        await web.activate_workspace("first")
        self.assertGreater(self.workspaces[1]["last_used_at"], self.workspaces[0]["last_used_at"])
        web.load_workspace_registry()
        self.assertEqual(len(web.workspace_registry), 2)
        self.assertIn("last_used_at", web.workspace_registry[1])

    async def test_result_tool_rejects_other_session_and_ready_without_loading(self):
        skill = self.store.install_upload({"demo/SKILL.md": manifest()})
        job = self.store.installations.create(skill)
        self.store.installations.update(job["id"], session_id="owner")
        class RuntimeStore:
            session_id = "other"
            async def load_task(inner, task_id):
                return SimpleNamespace(data={"session_id": inner.session_id})
            async def read_events(inner, task_id):
                return ()
        runtime = RuntimeStore()
        provider = CoreSkillToolProvider(SkillRegistry(), self.store, runtime)
        context = ToolInvocationContext("invoke", "task", "turn", self.first, datetime.now(timezone.utc))
        call = ToolCall("finish", "skill.installation_finish", {
            "installation_id": job["id"], "status": "ready", "summary": "done", "checks": ["checked"],
        })
        wrong = await provider._finish_installation(call, context)
        self.assertFalse(wrong.ok)
        runtime.session_id = "owner"
        unloaded = await provider._finish_installation(call, context)
        self.assertFalse(unloaded.ok)
        self.assertIn("SKILL.md", unloaded.message)
        blocked = await provider._finish_installation(ToolCall("blocked", call.name, {**call.arguments, "status": "blocked", "summary": "等待用户登录"}), context)
        self.assertTrue(blocked.ok)
        self.assertEqual(self.store.installations.get(job["id"])["status"], "blocked")

    async def test_runtime_waits_and_failure_restore_without_false_success(self):
        skill = self.store.install_upload({"demo/SKILL.md": manifest()})
        job = self.store.installations.create(skill)
        job = self.store.installations.update(job["id"], status="running", session_id="owner", task_id="task")
        class Client:
            state = "awaiting_approval"
            async def get_task_result(inner, task_id):
                return SimpleNamespace(status=inner.state, task_id=task_id)
        server = SameLoopServer(Client())
        with patch.object(web, "runtime_server", server):
            for status, expected in (("awaiting_approval", "awaiting_approval"), ("awaiting_user", "awaiting_user"),
                                     ("failed", "failed"), ("interrupted", "interrupted"), ("completed", "unverified")):
                self.store.installations.update(job["id"], status="running")
                server._client.state = status
                restored = UserSkillsStore(self.root / "home")
                result = web.refresh_skill_installation(restored, restored.installations.get(job["id"]))
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["session_id"], "owner")

    async def test_stale_launch_after_restart_can_retry_and_projection_cannot_overwrite_ready(self):
        skill = self.store.install_upload({"demo/SKILL.md": manifest()})
        job = self.store.installations.create(skill)
        job = self.store.installations.update(job["id"], status="starting", session_id="lost")
        class Kernel:
            async def get_session(inner, session_id):
                raise LookupError(session_id)
        server = SameLoopServer(SimpleNamespace(application=SimpleNamespace(kernel=Kernel())))
        with patch.object(web, "runtime_server", server):
            pending = web.refresh_skill_installation(self.store, job)
            self.assertEqual(pending["status"], "pending")
        self.store.installations.update(job["id"], status="ready", summary="验证通过")
        stale = self.store.installations.update(job["id"], expected_status="starting", status="running")
        self.assertEqual(stale["status"], "ready")
