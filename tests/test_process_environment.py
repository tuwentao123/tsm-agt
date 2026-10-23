from __future__ import annotations

import os
import shutil
import unittest
from unittest.mock import patch

from tsm_agt.bootstrap.process_environment_configuration import (
    ProcessEnvironmentConfiguration,
)
from tsm_agt.ports import ProcessEnvironmentPolicy


class ProcessEnvironmentConfigurationTest(unittest.TestCase):
    def test_allowed_package_manager_tools_are_resolvable(self) -> None:
        environment = ProcessEnvironmentConfiguration().build_environment({})

        with patch.dict(os.environ, environment, clear=True):
            # Isolated execution retains the historical system/Homebrew fallback.
            self.assertEqual(shutil.which("gh"), "/opt/homebrew/bin/gh")

        # The system paths stay first, so an allowed tool keeps resolving to the
        # binary it always did rather than to a package-manager copy.
        self.assertTrue(
            environment["PATH"].startswith("/usr/bin:/bin:/usr/sbin:/sbin")
        )
        self.assertIn("/opt/homebrew/bin", environment["PATH"])
        self.assertIn("/usr/local/bin", environment["PATH"])

    def test_credential_like_host_variables_stay_out_by_default(self) -> None:
        environment = ProcessEnvironmentConfiguration().build_environment(
            {}, ProcessEnvironmentPolicy(), {"GH_TOKEN": "secret", "HOME": "/tmp"}
        )

        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("HOME", environment)

    def test_governed_path_preserves_user_tools_and_interpreter_order(self) -> None:
        host = {"PATH": "/custom/tools:/runtime/bin:/usr/bin", "GITHUB_TOKEN": "not-forwarded"}
        policy = ProcessEnvironmentPolicy(
            inherit_host_environment=True, allowed_host_variables=("PATH",),
            runtime_trust_mode="governed",
        )
        environment = ProcessEnvironmentConfiguration().build_environment({}, policy, host)
        self.assertEqual(environment["PATH"], host["PATH"])
        self.assertNotIn("GITHUB_TOKEN", environment)
        explicit = ProcessEnvironmentConfiguration().build_environment({"PATH": "/explicit/bin"}, policy, host)
        self.assertEqual(explicit["PATH"], "/explicit/bin")

    def test_explicitly_empty_host_does_not_read_real_host_variables(self) -> None:
        policy = ProcessEnvironmentPolicy(inherit_host_environment=True, allowed_host_variables=("PATH", "HOME"))
        with patch.dict(os.environ, {"PATH": "/should-not-be-inherited", "HOME": "/host-home"}):
            environment = ProcessEnvironmentConfiguration().build_environment({}, policy, {})
        self.assertNotIn("HOME", environment)
        self.assertNotIn("/should-not-be-inherited", environment["PATH"])


if __name__ == "__main__":
    unittest.main()
