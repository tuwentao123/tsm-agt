"""``Kernel.dispatch_input_event`` dispatches by table, not by an if/elif ladder.

Hermes bans ``if/elif`` ladders of four or more branches keyed on a kind
(``AGENTS.md:249``) and prescribes a table -> handler. The ladder this replaced
had six branches, and its failure mode was silent: adding a seventh input kind
without a branch fell through to whatever ``isinstance`` matched next.

A table trades that for two *loud* failure modes, both pinned here:

* a new member of the ``RuntimeInputEvent`` union with no row -- caught by
  comparing the table's keys against the union itself, so the test does not need
  updating when a kind is added;
* a row naming a handler that does not exist -- caught by binding it.

The remaining check is the one the ladder provided implicitly: an event that is
not registered at all must raise rather than return ``None``.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import get_args

from tsm_agt.core.kernel import Kernel, _INPUT_HANDLERS
from tsm_agt.core.runtime_input import RuntimeInputEvent


class InputDispatchTableTest(unittest.TestCase):
    def test_every_input_kind_has_exactly_one_handler(self) -> None:
        kinds = set(get_args(RuntimeInputEvent))
        self.assertTrue(kinds, "RuntimeInputEvent must enumerate its members")
        self.assertEqual(
            set(_INPUT_HANDLERS), kinds,
            "every RuntimeInputEvent member needs a row, and every row needs a "
            "member; an unlisted kind is the ladder's silent fall-through again",
        )

    def test_every_row_binds_to_a_real_handler(self) -> None:
        for kind, name in _INPUT_HANDLERS.items():
            with self.subTest(kind=kind.__name__):
                self.assertTrue(
                    hasattr(Kernel, name),
                    f"{kind.__name__} dispatches to Kernel.{name}, which is gone",
                )

    def test_an_unregistered_event_raises_instead_of_returning_none(self) -> None:
        kernel = Kernel.__new__(Kernel)

        class Unregistered:
            pass

        with self.assertRaises(TypeError) as caught:
            # ``dispatch_input_event`` must stay total, so it is driven directly
            # rather than through a Task.
            asyncio.run(kernel.dispatch_input_event(Unregistered()))
        self.assertIn("unsupported runtime input event", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
