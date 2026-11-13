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
            fixtures = [Path(__file__).parent / "fixtures" / name for name in ("composer_cancel_button.test.cjs", "skill_install.test.cjs", "task_stream.test.cjs", "text_presentation.test.cjs", "workspace_resizer.test.cjs", "todo_dock.test.cjs", "model_selection.test.cjs", "clarification_recovery.test.cjs", "chat_navigation.test.cjs", "files_pane.test.cjs", "approval_redirect.test.cjs", "auto_approve.test.cjs", "slash_skill_menu.test.cjs", "slash_menu_scroll.test.cjs", "tool_card.test.cjs")]
            completed = subprocess.run(
                [shutil.which("node"), "--test", *(str(fixture) for fixture in fixtures)],
                env={**os.environ, "TSM_SKILL_TEST_SCRIPT": str(source)},
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_composer_offers_the_per_conversation_auto_approve_switch(self):
        # The switch is a checkbox above the textarea, and turning it on has to
        # reach the same approval path a click uses.
        self.assertIn('id="auto-approve"', INDEX_HTML)
        self.assertIn('帮我自动授权', INDEX_HTML)
        self.assertIn('autoApprovePendingApprovals', INDEX_HTML)
        self.assertIn('/auto-approve', INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
