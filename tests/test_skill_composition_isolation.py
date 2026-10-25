"""Skill composition must respect model capabilities and isolate fixtures."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tsm_agt.adapters.fixture import EchoModelProvider, ToolCallingModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.ports import ToolProviderPort
from tsm_agt.sdk import EngineeringAgentClient


class SkillCompositionIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_does_not_read_user_skill_files_by_default(self):
        with patch("tsm_agt.bootstrap.composition.UserSkillsStore") as factory:
            app = compose_fixture_application(model_adapter=ToolCallingModelProvider(), tool_adapters=())
            await app.start()
            try:
                self.assertFalse(any(tool.name.startswith("skill.") for tool in await app.kernel.list_tools()))
            finally:
                await app.registry.stop_all()
            factory.assert_not_called()

    async def test_text_only_model_never_receives_skill_tools_even_when_requested(self):
        with patch("tsm_agt.bootstrap.composition.UserSkillsStore") as factory:
            app = compose_fixture_application(model_adapter=EchoModelProvider(), tool_adapters=(), enable_skills=True)
            await app.start()
            try:
                self.assertEqual(await app.kernel.list_tools(), ())
                self.assertEqual(app.registry.all(ToolProviderPort), ())
            finally:
                await app.registry.stop_all()
            factory.assert_not_called()

    async def test_completed_runner_callback_is_safe_after_client_close(self):
        with TemporaryDirectory() as directory:
            client = EngineeringAgentClient(Path(directory))
            client._runner_finished("already-closed-task")
