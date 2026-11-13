from __future__ import annotations

import importlib
import io
import json
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import yaml

from tsm_agt.bootstrap import (
    compose_openai_compatible_engineering_application_from_config,
    compose_openai_compatible_readonly_application_from_config,
)
from tsm_agt.bootstrap.model_configuration import (
    ModelConfigurationError,
    discover_model_profiles,
    load_model_configuration,
)
from tsm_agt.cli_setup import initialize_configuration
from tsm_agt.ports import Message, MessageRole, ModelRequest, TextBlock


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

    def test_profile_can_declare_a_window_above_the_adapter_default(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.yaml"
            path.write_text(_configuration() + """      capabilities:
        context_window: 300000
    fallback:
      provider: provider
      model: fallback-model
""", encoding="utf-8")

            application = compose_openai_compatible_engineering_application_from_config(
                path, root / "runtime.db",
            )

        model = application.kernel.dependencies.model
        self.assertEqual(model.capabilities_for("primary").context_window, 300_000)
        self.assertEqual(model.capabilities_for("fallback").context_window, 128_000)

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


_DISCOVERY_PROVIDER = """    kiro:
      base_url: http://127.0.0.1:5580
      auth: {api_key: mock-kiro-key}
      discover_models: true
"""


class GatewayModelDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_reaches_web_and_session_selection_reaches_gateway(self) -> None:
        web = importlib.import_module("tsm_agt.web.app")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.yaml"
            config = "version: 1\nmodel:\n  providers:\n" + _DISCOVERY_PROVIDER
            path.write_text(config, encoding="utf-8")
            body = json.dumps({"data": [
                {"id": "model-a"}, {"id": "model-b"}, {"id": "model-b"}, {"name": "no-id"},
            ]}).encode()
            with patch("tsm_agt.adapters.openai_compatible.model_catalog.build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(body)
                application = compose_openai_compatible_engineering_application_from_config(
                    path, root / "runtime.db",
                )
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:5580/v1/models")
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), "Bearer mock-kiro-key")
            self.assertEqual(path.read_text(encoding="utf-8"), config)

            server = SimpleNamespace(_client=SimpleNamespace(application=application))
            with patch.object(web, "runtime_server", server):
                catalog = await web.list_model_profiles()
            primary, selected = catalog["profiles"]
            self.assertEqual([primary["model"], selected["model"]], ["model-a", "model-b"])
            self.assertEqual(catalog["default_profile"], primary["id"])
            self.assertEqual(selected["protocol"], "openai_chat")
            self.assertEqual(selected["status"], "unverified")
            self.assertNotIn("mock-kiro-key", json.dumps(catalog))

            await application.start()
            try:
                session = await application.kernel.create_session("Kiro model")
                updated = await application.kernel.update_session_model_profile(
                    session.session_id, selected["id"],
                )
                provider = application.kernel.dependencies.model
                with patch(
                    "tsm_agt.adapters.http_json.UrllibHttpJsonTransport.post_json",
                    return_value={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]},
                ) as post:
                    response = await provider.complete(ModelRequest(
                        "turn-kiro",
                        (Message("user", MessageRole.USER, (TextBlock("Hello"),)),),
                        model_profile_id=updated.model_profile_id,
                    ))
                endpoint, headers, payload, _ = post.call_args.args
                self.assertEqual(endpoint, "http://127.0.0.1:5580/v1/chat/completions")
                self.assertEqual(headers["Authorization"], "Bearer mock-kiro-key")
                self.assertEqual(payload["model"], "model-b")
                self.assertEqual(response.message.text, "OK")
            finally:
                await application.registry.stop_all()

    async def test_discovery_failure_does_not_disable_existing_profiles(self) -> None:
        config = _configuration().replace("  profiles:", _DISCOVERY_PROVIDER + "  profiles:")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.yaml"
            path.write_text(config, encoding="utf-8")
            with patch("tsm_agt.adapters.openai_compatible.model_catalog.build_opener") as opener:
                opener.return_value.open.side_effect = HTTPError(
                    "http://127.0.0.1:5580/v1/models", 401, "Unauthorized",
                    {}, io.BytesIO(b"mock-kiro-key"),
                )
                application = compose_openai_compatible_engineering_application_from_config(
                    path, root / "runtime.db",
                )
            catalog = application.kernel.dependencies.model.profile_catalog()
            self.assertEqual(catalog["default_profile"], "primary")
            self.assertEqual([row["id"] for row in catalog["profiles"]], ["primary"])
            self.assertEqual(application.configuration_issues, ("kiro：读取模型列表失败（HTTP 401）",))

    async def test_invalid_discovery_configuration_only_disables_that_provider(self) -> None:
        invalid_settings = (
            ({"auth": None}, "auth must be a mapping"),
            ({"base_url": "http://[secret-address"}, "base_url 必须是有效的 HTTP(S) 地址"),
            ({"discover_models": "true"}, "discover_models must be true or false"),
            ({"options": {"streaming": "true"}}, "options.streaming must be true or false"),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.yaml"
            for index, (settings, detail) in enumerate(invalid_settings):
                with self.subTest(settings=settings):
                    config = _configuration("anthropic_messages").replace(
                        "  profiles:", _DISCOVERY_PROVIDER + "  profiles:",
                    )
                    document = yaml.safe_load(config)
                    document["model"]["providers"]["kiro"].update(settings)
                    path.write_text(yaml.safe_dump(document), encoding="utf-8")
                    with patch("tsm_agt.bootstrap.composition.fetch_model_ids", return_value=("remote-model",)):
                        application = compose_openai_compatible_engineering_application_from_config(
                            path, root / f"runtime-{index}.db",
                        )
                    await application.start()
                    try:
                        catalog = application.kernel.dependencies.model.profile_catalog()
                        self.assertEqual(catalog["default_profile"], "primary")
                        self.assertEqual(
                            [(row["id"], row["protocol"]) for row in catalog["profiles"]],
                            [("primary", "anthropic_messages")],
                        )
                        issue, = application.configuration_issues
                        self.assertTrue(issue.startswith("kiro："))
                        self.assertIn(detail, issue)
                        self.assertNotIn("secret-address", issue)
                        self.assertNotIn("mock-kiro-key", issue)
                    finally:
                        await application.registry.stop_all()

    def test_discovery_defaults_only_apply_to_generated_profiles(self) -> None:
        document = yaml.safe_load(
            _configuration().replace("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000")
            .replace("        strict_tool_schema: false\n", ""),
        )
        original = load_model_configuration(document=document)
        document["model"]["providers"]["provider"]["discover_models"] = True

        fetch = Mock(return_value=("local-model", "remote-model"))
        expanded, issues = discover_model_profiles(document, fetch)
        fetch.assert_called_once_with("http://127.0.0.1:8000/v1", "local-secret")
        self.assertEqual(issues, ())
        self.assertEqual(load_model_configuration(document=expanded), original)
        discovered_id, = expanded["model"]["profiles"].keys() - {"primary"}
        discovered = load_model_configuration(document=expanded, profile_id=discovered_id)
        self.assertEqual(discovered.model, "remote-model")
        self.assertEqual(discovered.base_url, "http://127.0.0.1:8000/v1")
        self.assertFalse(discovered.strict_tool_schema)

        fetch.side_effect = ValueError("目录不可用")
        offline, issues = discover_model_profiles(document, fetch)
        self.assertEqual(issues, ("provider：目录不可用",))
        self.assertIs(offline, document)
        self.assertEqual(load_model_configuration(document=offline), original)

    def test_discovery_only_configuration_reports_its_own_error(self) -> None:
        document = yaml.safe_load("version: 1\nmodel:\n  providers:\n" + _DISCOVERY_PROVIDER)
        document["model"]["providers"]["kiro"].pop("auth")
        fetch = Mock()
        with self.assertRaisesRegex(ValueError, "kiro：model.providers.kiro.auth must be a mapping"):
            discover_model_profiles(document, fetch)
        fetch.assert_not_called()
