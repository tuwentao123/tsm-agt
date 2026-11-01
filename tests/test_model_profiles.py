from __future__ import annotations

import stat
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tsm_agt.bootstrap import (
    compose_openai_compatible_readonly_application_from_config,
)
from tsm_agt.bootstrap.model_configuration import (
    ModelConfigurationError,
    load_model_configuration,
)
from tsm_agt.cli_setup import initialize_configuration


def _configuration(protocol: str = "openai_chat") -> str:
    return f"""version: 1
model:
  default_profile: primary
  providers:
    provider:
      protocol: {protocol}
      base_url: http://127.0.0.1:8000/v1
      auth:
        api_key: local-secret
  profiles:
    primary:
      provider: provider
      model: local-model
      options:
        streaming: false
        strict_tool_schema: false
"""


class YamlOnlyConfigurationTest(unittest.TestCase):
    def test_loads_openai_responses_without_environment_overlay(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(_configuration("openai_responses"), encoding="utf-8")

            configuration = load_model_configuration(path)

        assert configuration is not None
        self.assertEqual(configuration.model, "local-model")
        self.assertEqual(configuration.wire_protocol, "openai_responses")
        self.assertFalse(configuration.streaming)
        self.assertEqual(configuration.sources["model.model"], "yaml")

    def test_composition_reads_the_same_yaml_document(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.yaml"
            path.write_text(
                _configuration() + """
agent:
  max_model_calls: 12
exploration:
  max_tool_calls: 8
  max_actions: 8
  max_tool_seconds: 10
""",
                encoding="utf-8",
            )
            # A repository-local dotenv is intentionally irrelevant now.
            (root / ".env").write_text("THIS_IS_NOT_VALID_DOTENV", encoding="utf-8")

            application = compose_openai_compatible_readonly_application_from_config(
                path, root / "runtime.db",
            )

        snapshot = application.kernel.dependencies.configuration_metadata
        self.assertEqual(snapshot["model"]["model"], "local-model")
        self.assertEqual(snapshot["exploration_budget"]["agent_max_model_calls"], 12)

    def test_missing_template_values_are_reported_as_yaml_paths(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                _configuration().replace("local-secret", "your-api-key"),
                encoding="utf-8",
            )

            with self.assertRaises(ModelConfigurationError) as raised:
                load_model_configuration(path)

        self.assertIn("model.providers.provider.auth.api_key", raised.exception.missing)

    def test_init_writes_private_yaml_without_creating_workspace_files(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "config.yaml"
            self.assertEqual(initialize_configuration(path), 0)
            self.assertIn("model:", path.read_text(encoding="utf-8"))
            if __import__("os").name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
