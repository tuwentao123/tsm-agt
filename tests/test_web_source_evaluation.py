from __future__ import annotations

import unittest

from tsm_agt.ports import RuntimeEvent
from tsm_agt.web.app import _turn_progress_item


class SourceEvaluationProgressTest(unittest.TestCase):
    def test_all_visible_exposure_and_review_survive_event_projection(self) -> None:
        started = _turn_progress_item(RuntimeEvent(
            "event-1", "task-1", 1, "turn.started", {
                "turn_id": "turn-1",
                "input_character_count": 30,
                "tool_exposure_profile": "all-visible",
                "visible_tools": ["web.search", "source.github.get_file"],
            }, turn_id="turn-1",
        ))
        reviewed = _turn_progress_item(RuntimeEvent(
            "event-2", "task-1", 2, "turn.completion_review_completed", {
                "turn_id": "turn-1", "action": "CONTINUE",
                "reason_code": "source_content_not_read",
            }, turn_id="turn-1",
        ))

        self.assertEqual(started["tool_exposure_profile"], "all-visible")
        self.assertEqual(started["visible_tool_count"], 2)
        self.assertEqual(reviewed["kind"], "completion_review")
        self.assertEqual(reviewed["review_action"], "CONTINUE")
