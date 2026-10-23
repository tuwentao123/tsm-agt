"""Run rendered browser JS, including the actual upload completion callback."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tsm_agt.web.app import INDEX_HTML


class SkillInstallFrontendTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute browser JS")
    def test_real_page_installation_callbacks(self):
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", INDEX_HTML, re.S)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "page.js"
            source.write_text("\n".join(scripts), encoding="utf-8")
            fixtures = [Path(__file__).parent / "fixtures" / name for name in ("skill_install.test.cjs", "task_stream.test.cjs", "workspace_resizer.test.cjs", "todo_dock.test.cjs")]
            completed = subprocess.run(
                [shutil.which("node"), "--test", *(str(fixture) for fixture in fixtures)],
                env={**os.environ, "TSM_SKILL_TEST_SCRIPT": str(source)},
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
