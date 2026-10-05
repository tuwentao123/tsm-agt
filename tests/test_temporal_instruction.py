"""The per-call "what day is it" anchor must also demand a lookup.

`task-d82d1708dc514fdd8776101533552305` answered "什么时候 nba常规赛" from training
data with `web.search` sitting in `visible_tools`, and told the user to visit
nba.com -- exactly the "answer degrades to stale recollection" the anchor's own
docstring warns about. Telling the model how to *interpret* dates is not enough
when it never fetched one; it has to be told to go and get them.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tsm_agt.core.kernel import _temporal_context_instruction


class TemporalContextInstructionTest(unittest.TestCase):
    def test_it_anchors_the_current_date(self) -> None:
        moment = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

        instruction = _temporal_context_instruction(moment)

        self.assertIn("Today is 2026-10-05", instruction)

    def test_it_requires_a_lookup_for_time_sensitive_questions(self) -> None:
        instruction = _temporal_context_instruction(
            datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
        )

        self.assertIn("must be looked up with the available tools", instruction)
        # Recalling it is named as the failure, and so is giving up in prose --
        # the model did both in the reported Task.
        self.assertIn("stale answer", instruction)
        self.assertIn("is not an answer", instruction)


if __name__ == "__main__":
    unittest.main()
