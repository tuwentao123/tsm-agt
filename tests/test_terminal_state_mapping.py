"""The CLI exit code is the only client-facing state mapping left.

A verdict -> terminal-state table used to live here too (AG-16), together with
``Kernel.finalize_acceptance``. Both are gone with the acceptance layer: a Task
now reaches its terminal state through the single completion path, so what a
client still decides is only how to report a *finished* Task to a shell.
"""

from __future__ import annotations

import unittest

from tsm_agt.cli import _exit_code_for
from tsm_agt.core import TaskState


class CliExitCodeTest(unittest.TestCase):
    def test_cli_exit_codes_distinguish_review_from_failure(self) -> None:
        self.assertEqual(_exit_code_for(TaskState.SUCCEEDED), 0)
        self.assertEqual(_exit_code_for(TaskState.FAILED), 1)
        self.assertEqual(_exit_code_for(TaskState.NEEDS_REVIEW), 2)
        self.assertNotEqual(
            _exit_code_for(TaskState.NEEDS_REVIEW),
            _exit_code_for(TaskState.FAILED),
        )


if __name__ == "__main__":
    unittest.main()
