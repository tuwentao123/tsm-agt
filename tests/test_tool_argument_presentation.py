from __future__ import annotations

import unittest

from tsm_agt.adapters.adaptive_tool_presentation import (
    AdaptiveToolArgumentPresenter,
)
from tsm_agt.cli import _render_exploration_progress
from tsm_agt.core import AgentProgress, AgentProgressKind


class AdaptiveToolArgumentPresenterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.presenter = AdaptiveToolArgumentPresenter()

    @staticmethod
    def patch(path: str, old: str, new: str) -> dict:
        return {
            "path": path, "expected_hash": "a" * 64,
            "edits": [{"old_text": old, "new_text": new}],
        }

    def test_small_patch_displays_every_edit_in_full(self) -> None:
        presentation = self.presenter.present(
            "core.apply_patches", {"patches": [
                self.patch("first.py", "before_one", "after_one"),
                self.patch("second.py", "before_two", "after_two"),
            ]}
        )
        self.assertEqual(presentation.mode, "full")
        self.assertIn("完整补丁", presentation.text)
        self.assertIn("before_one", presentation.text)
        self.assertIn("after_two", presentation.text)

    def test_large_patch_lists_all_files_but_only_sample_edits(self) -> None:
        patches = [
            self.patch(
                f"src/file_{index}.py",
                f"OLD_UNIQUE_{index}\n" * 10,
                f"NEW_UNIQUE_{index}\n" * 10,
            )
            for index in range(5)
        ]
        presentation = self.presenter.present(
            "core.apply_patches", {"patches": patches}
        )
        self.assertEqual(presentation.mode, "summary")
        for index in range(5):
            self.assertIn(f"src/file_{index}.py", presentation.text)
        self.assertIn("代表性改动示例（3/5）", presentation.text)
        self.assertIn("其余 2 处改动未在终端展开", presentation.text)
        self.assertIn("OLD_UNIQUE_0", presentation.text)
        self.assertNotIn("OLD_UNIQUE_4", presentation.text)
        self.assertNotIn("expected_hash", presentation.text)
        self.assertNotIn("old_text", presentation.text)

    def test_single_large_edit_uses_summary_and_bounded_excerpt(self) -> None:
        marker = "tail-must-not-be-visible"
        body = "\n".join([f"line-{index}" for index in range(60)] + [marker])
        presentation = self.presenter.present(
            "core.apply_patch", self.patch("large.txt", body, "replacement")
        )
        self.assertEqual(presentation.mode, "summary")
        self.assertIn("line-0", presentation.text)
        self.assertNotIn(marker, presentation.text)

    def test_progress_renderer_uses_presentation_not_raw_patch_arguments(self) -> None:
        raw_secret = "raw-unselected-edit-must-not-print"
        progress = AgentProgress(
            AgentProgressKind.EXPLORATION, phase="tool_result",
            operation="core.apply_patches",
            operation_arguments={"patches": [{"old_text": raw_secret}]},
            operation_presentation="5 files, 8 edits; three examples shown",
            operation_presentation_mode="summary",
        )
        rendered = "\n".join(_render_exploration_progress(progress))
        self.assertNotIn("three examples shown", rendered)
        self.assertNotIn(raw_secret, rendered)

    def test_generic_arguments_still_hide_credentials(self) -> None:
        presentation = self.presenter.present(
            "fixture.network", {"url": "https://example.test", "api_key": "secret"}
        )
        self.assertIn("https://example.test", presentation.text)
        self.assertNotIn("secret", presentation.text)


if __name__ == "__main__":
    unittest.main()
