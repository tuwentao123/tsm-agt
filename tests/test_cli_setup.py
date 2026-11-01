from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tsm_agt.bootstrap.model_configuration import (
    ModelConfigurationError, load_model_configuration,
)
from tsm_agt.cli import main
from tsm_agt.cli_setup import initialize_configuration, run_doctor


def _config(api_key: str = "sk-doctor-secret") -> str:
    return f"""version: 1
model:
  default_profile: primary
  providers:
    primary:
      protocol: openai_chat
      base_url: https://user:password@models.example.test:8443/v1?token=hidden
      auth:
        api_key: {api_key}
  profiles:
    primary:
      provider: primary
      model: test-model
"""


class FirstRunSetupTest(unittest.TestCase):
    def test_init_creates_private_yaml_without_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "private" / "config.yaml"
            output = io.StringIO()
            with redirect_stdout(output):
                result = initialize_configuration(target)
            self.assertEqual(result, 0)
            self.assertTrue(target.is_file())
            self.assertFalse((root / ".agent").exists())
            self.assertIn("model:", target.read_text(encoding="utf-8"))
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertIn("tsm-agt doctor", output.getvalue())

    def test_init_never_overwrites_existing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.yaml"
            target.write_text("original", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                initialize_configuration(target)
            self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_init_print_does_not_touch_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.yaml"
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(initialize_configuration(target, print_only=True), 0)
            self.assertIn("model:", output.getvalue())
            self.assertFalse(target.exists())

    def test_configuration_error_is_actionable_and_never_prints_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(_config("your-api-key"), encoding="utf-8")
            with self.assertRaises(ModelConfigurationError) as caught:
                load_model_configuration(path)
            message = str(caught.exception)
            self.assertIn("tsm-agt init", message)
            self.assertIn("auth.api_key", message)
            self.assertNotIn("test-model", message)

    def test_cli_version_uses_package_version(self) -> None:
        output = io.StringIO()
        with patch("sys.argv", ["tsm-agt", "--version"]), redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), "tsm-agt 0.1.0")


class DoctorTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_configuration_is_structured_and_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            with redirect_stdout(output):
                result = await run_doctor(
                    root, as_json=True, configuration_file=root / "missing.yaml",
                )
            report = json.loads(output.getvalue())
        self.assertEqual(result, 1)
        configuration = next(item for item in report["checks"] if item["name"] == "model_configuration")
        self.assertEqual(configuration["status"], "fail")
        self.assertEqual(configuration["details"]["missing"], ["configuration file"])
        self.assertIn("tsm-agt init", configuration["remedy"])

    async def test_json_report_redacts_endpoint_and_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.yaml"
            path.write_text(_config(), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                result = await run_doctor(root, as_json=True, configuration_file=path)
            encoded = output.getvalue()
            report = json.loads(encoded)
        self.assertEqual(result, 0)
        self.assertNotIn("sk-doctor-secret", encoded)
        self.assertNotIn("password", encoded)
        self.assertNotIn("/v1", encoded)
        configuration = next(item for item in report["checks"] if item["name"] == "model_configuration")
        self.assertEqual(configuration["details"]["endpoint_origin"], "https://models.example.test:8443")

    async def test_model_check_failure_redacts_secrets(self) -> None:
        async def failing_probe(configuration):
            raise RuntimeError(f"bad {configuration.api_key} at {configuration.base_url}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.yaml"
            path.write_text(_config(), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                result = await run_doctor(
                    root, as_json=True, model_check=True,
                    configuration_file=path, model_probe=failing_probe,
                )
            encoded = output.getvalue()
        self.assertEqual(result, 1)
        self.assertNotIn("sk-doctor-secret", encoded)
        self.assertNotIn("https://user:password", encoded)
        self.assertIn("<redacted>", encoded)
