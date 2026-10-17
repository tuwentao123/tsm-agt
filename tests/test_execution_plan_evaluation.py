from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_execution_plan_behavior import _file_hash


class ExecutionPlanEvaluationTest(unittest.TestCase):
    def test_file_checks_detect_changes_and_reject_outside_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            self.assertIsNone(_file_hash(workspace, "example.txt"))
            (workspace / "example.txt").write_text("before", encoding="utf-8")
            before = _file_hash(workspace, "example.txt")
            (workspace / "example.txt").write_text("after", encoding="utf-8")
            self.assertNotEqual(before, _file_hash(workspace, "example.txt"))
            with self.assertRaises(ValueError):
                _file_hash(workspace, "../outside.txt")
