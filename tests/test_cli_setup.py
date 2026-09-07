from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tsm_agt.bootstrap.model_configuration import (
    MODEL_ENV_NAMES, ModelConfigurationError, load_model_configuration,
)
from tsm_agt.cli import main
from tsm_agt.cli_setup import initialize_workspace, run_doctor


COMPLETE_ENV = {
    "TSM_AGT_MODEL_BASE_URL": (
        "https://user:password@models.example.test:8443/v1?token=hidden"
    ),
    "TSM_AGT_MODEL": "test-model",
    "TSM_AGT_MODEL_API_KEY": "sk-doctor-secret",
}


class FirstRunSetupTest(unittest.TestCase):
    def test_init_creates_private_template_without_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            with redirect_stdout(output):
                result = initialize_workspace(root)
            target = root / ".env"
            self.assertEqual(result, 0)
            self.assertTrue(target.is_file())
            self.assertFalse((root / ".agent").exists())
            self.assertEqual(
                set(line.split("=", 1)[0] for line in target.read_text().splitlines()
                    if line and not line.startswith("#")),
                set(MODEL_ENV_NAMES),
            )
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertIn("tsm-agt doctor", output.getvalue())

    def test_init_never_overwrites_existing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".env"
            target.write_text("original", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                initialize_workspace(root)
            self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_init_print_does_not_touch_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(initialize_workspace(root, print_only=True), 0)
            self.assertIn("TSM_AGT_MODEL_BASE_URL", output.getvalue())
            self.assertEqual(list(root.iterdir()), [])

    def test_configuration_error_is_actionable_and_never_prints_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            with self.assertRaises(ModelConfigurationError) as caught:
                load_model_configuration(
                    path, {"TSM_AGT_MODEL": "private-model"}
                )
            message = str(caught.exception)
            self.assertIn("tsm-agt init", message)
            self.assertIn("TSM_AGT_MODEL_API_KEY", message)
            self.assertNotIn("private-model", message)

    def test_generated_placeholders_are_not_accepted_as_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(io.StringIO()):
                initialize_workspace(root)
            with self.assertRaises(ModelConfigurationError) as caught:
                load_model_configuration(root / ".env", {})
            self.assertEqual(
                caught.exception.missing,
                ("TSM_AGT_MODEL", "TSM_AGT_MODEL_API_KEY"),
            )

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
            output = io.StringIO()
            with redirect_stdout(output):
                result = await run_doctor(
                    Path(directory), as_json=True, environment={}
                )
            report = json.loads(output.getvalue())
            self.assertEqual(result, 1)
            self.assertFalse(report["healthy"])
            configuration = next(
                check for check in report["checks"]
                if check["name"] == "model_configuration"
            )
            self.assertEqual(configuration["status"], "fail")
            self.assertEqual(
                configuration["details"]["missing"], list(MODEL_ENV_NAMES)
            )
            self.assertIn("tsm-agt init", configuration["remedy"])

    async def test_json_report_redacts_endpoint_and_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with redirect_stdout(output):
                result = await run_doctor(
                    Path(directory), as_json=True, environment=COMPLETE_ENV
                )
            encoded = output.getvalue()
            report = json.loads(encoded)
            self.assertEqual(result, 0)
            self.assertTrue(report["healthy"])
            self.assertNotIn(COMPLETE_ENV["TSM_AGT_MODEL_API_KEY"], encoded)
            self.assertNotIn("password", encoded)
            self.assertNotIn("/v1", encoded)
            configuration = next(
                check for check in report["checks"]
                if check["name"] == "model_configuration"
            )
            self.assertEqual(
                configuration["details"]["endpoint_origin"],
                "https://models.example.test:8443",
            )
            self.assertTrue(
                configuration["details"]["credentials_configured"]
            )

    async def test_model_check_uses_injected_probe_and_reports_success(self) -> None:
        seen = []

        async def probe(configuration):
            seen.append((configuration.endpoint_origin, configuration.model))

        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with redirect_stdout(output):
                result = await run_doctor(
                    Path(directory), as_json=True, model_check=True,
                    environment=COMPLETE_ENV, model_probe=probe,
                )
            report = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(seen, [("https://models.example.test:8443", "test-model")])
        connectivity = next(
            check for check in report["checks"]
            if check["name"] == "model_connectivity"
        )
        self.assertEqual(connectivity["status"], "pass")

    async def test_model_check_failure_redacts_secrets(self) -> None:
        async def failing_probe(configuration):
            raise RuntimeError(
                f"bad {configuration.api_key} at {configuration.base_url}"
            )

        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with redirect_stdout(output):
                result = await run_doctor(
                    Path(directory), as_json=True, model_check=True,
                    environment=COMPLETE_ENV, model_probe=failing_probe,
                )
            encoded = output.getvalue()
        self.assertEqual(result, 1)
        self.assertNotIn(COMPLETE_ENV["TSM_AGT_MODEL_API_KEY"], encoded)
        self.assertNotIn(COMPLETE_ENV["TSM_AGT_MODEL_BASE_URL"], encoded)
        self.assertIn("<redacted>", encoded)


if __name__ == "__main__":
    unittest.main()
