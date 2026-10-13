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
            # The sandbox allowlist accepts ``gh`` by name, so the governed PATH
            # has to be able to find it; on this platform it ships with Homebrew.
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


if __name__ == "__main__":
    unittest.main()
