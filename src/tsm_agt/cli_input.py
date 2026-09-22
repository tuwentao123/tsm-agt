"""Platform-aware interactive input for the CLI entry adapter."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext

os.environ.setdefault("PROMPT_TOOLKIT_NO_CPR", "1")

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout


LineInput = Callable[[str], str]


class TerminalLineInput:
    """One reusable Unicode-aware line editor for a CLI process.

    Prompt Toolkit owns terminal rendering, display-width calculation, IME text
    commits, cursor keys, deletion, bracketed paste and history.  Non-interactive
    pipes deliberately fall back to CPython input so scripts and tests keep their
    ordinary stdin semantics.
    """

    def __init__(self) -> None:
        self._session: PromptSession[str] | None = None

    @property
    def supports_live_input(self) -> bool:
        """Whether input can be awaited without blocking the Agent loop.

        Prompt Toolkit provides this for a real terminal on macOS, Linux and
        Windows.  A redirected pipe uses blocking ``input`` and must retain the
        sequential script contract.
        """
        return sys.stdin.isatty() and sys.stdout.isatty()

    def output_context(self) -> AbstractContextManager[None]:
        """Coordinate background output with an active editable prompt.

        Prompt Toolkit redraws the current input line whenever the screen
        changes.  Plain ``print`` calls made while ``prompt_async`` is active
        can otherwise be overwritten by that redraw, most visibly losing the
        final fragment of a streamed model answer.  ``patch_stdout`` routes
        complete output lines above the prompt and then restores the user's
        in-progress input.
        """
        if not self.supports_live_input:
            return nullcontext()
        # ``raw=True`` keeps streamed fragments byte-for-byte intact.  Each
        # explicit flush from the CLI is still rendered above the prompt and
        # followed by a safe prompt redraw.
        return patch_stdout(raw=True)

    def __call__(self, prompt: str) -> str:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return input(prompt)
        if self._session is None:
            self._session = PromptSession(
                history=InMemoryHistory(),
                enable_history_search=True,
                multiline=False,
                enable_open_in_editor=False,
                complete_while_typing=False,
            )
        return self._session.prompt(prompt)

    async def read(self, prompt: str) -> str:
        """Read inside the CLI's existing asyncio event loop."""
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return input(prompt)
        if self._session is None:
            self._session = PromptSession(
                history=InMemoryHistory(),
                enable_history_search=True,
                multiline=False,
                enable_open_in_editor=False,
                complete_while_typing=False,
            )
        return await self._session.prompt_async(prompt)


def platform_line_input() -> LineInput:
    return TerminalLineInput()
