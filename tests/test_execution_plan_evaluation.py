"""Contracts for the real-model execution-plan evaluator's event metrics."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

from tsm_agt.ports import RuntimeEvent


_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "evaluate_execution_plan_behavior.py"
)
_SPEC = importlib.util.spec_from_file_location("execution_plan_eval", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


class ExecutionPlanEvaluationTest(unittest.TestCase):
    def test_requested_tool_names_are_extracted_from_runtime_events(self) -> None:
        events = (
            RuntimeEvent("evt-1", "task-1", 1, "turn.started", {}),
            RuntimeEvent("evt-2", "task-1", 2, "tool.requested", {
                "call": {"name": "core.todo_list"},
            }),
            RuntimeEvent("evt-3", "task-1", 3, "tool.requested", {
                "call": {"name": "core.apply_patch_v4a"},
            }),
        )
        self.assertEqual(
            _MODULE._tool_names(events),
            ["core.todo_list", "core.apply_patch_v4a"],
        )

    def test_mutation_and_verification_sets_do_not_overlap(self) -> None:
        self.assertFalse(_MODULE.MUTATION_TOOLS & _MODULE.VERIFICATION_TOOLS)
