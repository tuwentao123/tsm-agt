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
    def _load(self, yaml_text: str = "", environment=None):
        import os
        import yaml
        from unittest.mock import patch
        with patch.dict(os.environ, environment or {}, clear=True):
            return load_search_configuration({"web": {"search": yaml.safe_load(yaml_text) or {}}})

    def test_defaults_to_off(self):
        configuration = self._load()
        self.assertEqual(configuration.tavily_mode, "off")
        self.assertEqual(configuration.tavily_api_key, "")

    def test_yaml_enables_keyless(self):
        configuration = self._load("tavily_mode: keyless\n")
        self.assertEqual(configuration.tavily_mode, "keyless")
        self.assertEqual(
            configuration.sources["search.tavily_mode"], "yaml"
        )

    def test_inline_comment_is_not_part_of_the_value(self):
        # `tavily_mode: keyless  # note` is a natural YAML setting; a
        # comment must not turn into an invalid mode and break startup.
        configuration = self._load(
            "tavily_mode: keyless   # 免 key 试用\n"
        )
        self.assertEqual(configuration.tavily_mode, "keyless")

    def test_quoted_value_keeps_its_hash(self):
        configuration = self._load(
            'tavily_api_key: "tvly-a#b"\n'
            "tavily_mode: key\n"
        )
        self.assertEqual(configuration.tavily_api_key, "tvly-a#b")

    def test_environment_does_not_override_yaml(self):
        configuration = self._load("tavily_mode: keyless", {"TSM_AGT_SEARCH_TAVILY_MODE": "off"})
        self.assertEqual(configuration.tavily_mode, "keyless")
        self.assertEqual(configuration.sources["search.tavily_mode"], "yaml")

    def test_key_mode_without_a_key_degrades_and_reports(self):
        # Search is optional: a bad value must not take the Runtime (and the
        # whole Web UI) down with it.
        configuration = self._load("tavily_mode: key\n")
        self.assertEqual(configuration.tavily_mode, "off")
        self.assertTrue(configuration.issues)
        self.assertIn("web.search.tavily_api_key", configuration.issues[0])

    def test_unknown_mode_degrades_and_reports(self):
        configuration = self._load("tavily_mode: always\n")
        self.assertEqual(configuration.tavily_mode, "off")
        self.assertTrue(configuration.issues)
        self.assertIn("always", configuration.issues[0])

    def test_valid_configuration_has_no_issues(self):
        configuration = self._load("tavily_mode: keyless\n")
        self.assertEqual(configuration.issues, ())

    def test_sources_never_expose_the_secret(self):
        configuration = self._load(
            "tavily_mode: key\n"
            "tavily_api_key: tvly-super-secret\n"
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


class DocumentCacheConfigurationTest(unittest.TestCase):
    def test_document_cache_settings_load_from_yaml(self):
        configuration = load_search_configuration({"web": {"search": {
            "document_cache_max_entries": 9, "document_cache_max_bytes": 8192,
            "reference_cache_max_entries": 11, "document_cache_ttl_seconds": 42.5,
        }}})
        self.assertEqual(configuration.document_cache_max_entries, 9)
        self.assertEqual(configuration.document_cache_max_bytes, 8192)
        self.assertEqual(configuration.reference_cache_max_entries, 11)
        self.assertEqual(configuration.document_cache_ttl_seconds, 42.5)
        self.assertEqual(configuration.sources["web.document_cache_max_entries"], "yaml")

    def test_invalid_document_cache_values_fall_back_without_startup_failure(self):
        configuration = load_search_configuration({"web": {"search": {"document_cache_max_entries": 0}}})
        self.assertEqual(configuration.document_cache_max_entries, 32)
        self.assertIn("web.search.document_cache_max_entries", configuration.issues[0])
