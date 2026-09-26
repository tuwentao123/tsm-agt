from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import compose_openai_compatible_readonly_application
from tsm_agt.bootstrap.search_configuration import (
    SearchConfiguration, load_search_configuration,
)
from tsm_agt.web.app import _root_cause_message


class SearchConfigurationTest(unittest.TestCase):
    def _load(self, env_file_text: str = "", environment=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            if env_file_text:
                path.write_text(env_file_text, encoding="utf-8")
            return load_search_configuration(path, environment or {})

    def test_defaults_to_off(self):
        configuration = self._load()
        self.assertEqual(configuration.tavily_mode, "off")
        self.assertEqual(configuration.tavily_api_key, "")

    def test_env_file_enables_keyless(self):
        configuration = self._load("TSM_AGT_SEARCH_TAVILY_MODE=keyless\n")
        self.assertEqual(configuration.tavily_mode, "keyless")
        self.assertEqual(
            configuration.sources["search.tavily_mode"], "env_file"
        )

    def test_inline_comment_is_not_part_of_the_value(self):
        # `MODE=keyless  # note` is a very natural thing to write in .env; a
        # comment must not turn into an invalid mode and break startup.
        configuration = self._load(
            "TSM_AGT_SEARCH_TAVILY_MODE=keyless   # 免 key 试用\n"
        )
        self.assertEqual(configuration.tavily_mode, "keyless")

    def test_quoted_value_keeps_its_hash(self):
        configuration = self._load(
            'TSM_AGT_SEARCH_TAVILY_API_KEY="tvly-a#b"\n'
            "TSM_AGT_SEARCH_TAVILY_MODE=key\n"
        )
        self.assertEqual(configuration.tavily_api_key, "tvly-a#b")

    def test_exported_environment_overrides_env_file(self):
        configuration = self._load(
            "TSM_AGT_SEARCH_TAVILY_MODE=keyless\n",
            {"TSM_AGT_SEARCH_TAVILY_MODE": "off"},
        )
        self.assertEqual(configuration.tavily_mode, "off")
        self.assertEqual(
            configuration.sources["search.tavily_mode"], "environment"
        )

    def test_key_mode_without_a_key_degrades_and_reports(self):
        # Search is optional: a bad value must not take the Runtime (and the
        # whole Web UI) down with it.
        configuration = self._load("TSM_AGT_SEARCH_TAVILY_MODE=key\n")
        self.assertEqual(configuration.tavily_mode, "off")
        self.assertTrue(configuration.issues)
        self.assertIn("TSM_AGT_SEARCH_TAVILY_API_KEY", configuration.issues[0])

    def test_unknown_mode_degrades_and_reports(self):
        configuration = self._load("TSM_AGT_SEARCH_TAVILY_MODE=always\n")
        self.assertEqual(configuration.tavily_mode, "off")
        self.assertTrue(configuration.issues)
        self.assertIn("always", configuration.issues[0])

    def test_valid_configuration_has_no_issues(self):
        configuration = self._load("TSM_AGT_SEARCH_TAVILY_MODE=keyless\n")
        self.assertEqual(configuration.issues, ())

    def test_sources_never_expose_the_secret(self):
        configuration = self._load(
            "TSM_AGT_SEARCH_TAVILY_MODE=key\n"
            "TSM_AGT_SEARCH_TAVILY_API_KEY=tvly-super-secret\n"
        )
        self.assertEqual(configuration.tavily_api_key, "tvly-super-secret")
        self.assertNotIn(
            "tvly-super-secret", " ".join(configuration.sources.values())
        )
        self.assertNotIn(
            "tvly-super-secret", " ".join(configuration.issues)
        )


class SearchConfigurationWiringTest(unittest.TestCase):
    def test_application_carries_configuration_issues(self):
        application = compose_openai_compatible_readonly_application(
            base_url="https://models.example.test/v1",
            model="test-model", api_key="test-key",
            search_configuration=SearchConfiguration(issues=("bad mode",)),
        )
        self.assertEqual(application.configuration_issues, ("bad mode",))

    def test_default_application_has_no_issues(self):
        application = compose_openai_compatible_readonly_application(
            base_url="https://models.example.test/v1",
            model="test-model", api_key="test-key",
        )
        self.assertEqual(application.configuration_issues, ())

    def test_root_cause_message_unwraps_the_chain(self):
        try:
            try:
                raise ValueError("TSM_AGT_X is invalid")
            except ValueError as inner:
                raise RuntimeError("failed to start local Runtime") from inner
        except RuntimeError as outer:
            self.assertEqual(
                _root_cause_message(outer), "TSM_AGT_X is invalid"
            )


if __name__ == "__main__":
    unittest.main()
