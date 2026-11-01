from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from scripts.evaluate_diagnosis_behavior import (
    FIXTURES, file_state, load_fixture, permitted, read_template, run_tests,
)
from tsm_agt.core import PromptTemplate


class DiagnosisEvaluationTest(unittest.TestCase):
    def fixture(self, case, root):
        fixture = load_fixture(FIXTURES / f"diagnosis-{case}.json")
        for name, content in fixture["files"].items():
            (root / name).write_text(content, encoding="utf-8")
        return fixture

    def test_partial_dataflow_detects_missing_hops_and_accepts_complete_wiring(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture("partial-dataflow", root)
            self.assertFalse(run_tests(root)["passed"])
            oracle = load_fixture(FIXTURES / "diagnosis-already-satisfied.json")
            # Updating just one caller must still leave the behavior broken.
            (root / "endpoint.py").write_text(oracle["files"]["endpoint.py"])
            self.assertFalse(run_tests(root)["passed"])
            (root / "pipeline.py").write_text(oracle["files"]["pipeline.py"])
            self.assertTrue(run_tests(root)["passed"])

    def test_transient_fix_detects_reappearance_and_accepts_source_state_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture("transient-fix", root)
            self.assertFalse(run_tests(root)["passed"])
            (root / "actions.py").write_text(
                "from view import refresh\n\ndef finish(job, widget):\n"
                "    job.status = 'completed'\n    refresh(job, widget)\n"
            )
            self.assertTrue(run_tests(root)["passed"])

    def test_satisfied_control_passes_without_source_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture("already-satisfied", root)
            before = file_state(root)
            self.assertTrue(run_tests(root)["passed"])
            self.assertEqual(file_state(root), before)

    def test_approval_excludes_protected_files_and_arbitrary_commands(self):
        fixture = load_fixture(FIXTURES / "diagnosis-partial-dataflow.json")
        def call(name, **arguments):
            return SimpleNamespace(name=name, arguments=arguments)
        self.assertTrue(permitted(call("core.apply_patch", path="pipeline.py"), fixture))
        self.assertFalse(permitted(call("core.apply_patch", path="test_flow.py"), fixture))
        self.assertFalse(permitted(call("core.apply_patch", path="../pipeline.py"), fixture))
        self.assertFalse(permitted(call("core.delete_file", path="pipeline.py"), fixture))
        self.assertFalse(permitted(call("core.run_command", cwd=".", argv=["python", "-c", "pass"]), fixture))
        self.assertFalse(permitted(call("core.run_command", cwd="..", argv=["python", "-m", "unittest"]), fixture))

    def test_serialized_baseline_preserves_manifest_and_capability_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "template.json"
            template = PromptTemplate.default()
            path.write_text(json.dumps(asdict(template)))
            self.assertEqual(read_template(path), template)
            self.assertEqual(read_template(path).manifest(), template.manifest())


if __name__ == "__main__":
    unittest.main()
