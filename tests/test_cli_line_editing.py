from __future__ import annotations

import os
import pty
import select
import sys
import time
import unittest
from pathlib import Path


@unittest.skipIf(os.name == "nt", "POSIX pseudo-terminal keyboard contract")
class CliLineEditingPtyTest(unittest.TestCase):
    project_root = Path(__file__).resolve().parents[1]

    def _run_pty(
        self, source: str, interactions: list[tuple[bytes, bytes, float]],
        timeout: float = 5.0,
    ) -> tuple[str, bytes]:
        master, slave = pty.openpty()
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(self.project_root / "src")
        os.close(master)
        os.close(slave)
        child_pid, master = pty.fork()
        if child_pid == 0:
            os.chdir(self.project_root)
            os.execve(sys.executable, [sys.executable, "-c", source], environment)
        output = bytearray()
        exit_status: int | None = None
        try:
            for expected, keys, delay in interactions:
                if expected:
                    self._read_until(master, output, expected, timeout)
                if delay:
                    time.sleep(delay)
                os.write(master, keys)
            deadline = time.monotonic() + timeout
            while exit_status is None and time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.1)
                if readable:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        pass
                    else:
                        if chunk:
                            output.extend(chunk)
                waited, status = os.waitpid(child_pid, os.WNOHANG)
                if waited == child_pid:
                    exit_status = status
            if exit_status is None:
                raise TimeoutError("line-editing child did not exit")
            while True:
                readable, _, _ = select.select([master], [], [], 0)
                if not readable:
                    break
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
        finally:
            if exit_status is None:
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass
                os.waitpid(child_pid, 0)
            os.close(master)
        rendered = output.decode("utf-8", errors="replace").replace("\r", "")
        self.assertTrue(os.WIFEXITED(exit_status), rendered)
        self.assertEqual(os.WEXITSTATUS(exit_status), 0, rendered)
        return rendered, bytes(output)

    @staticmethod
    def _read_until(
        descriptor: int, output: bytearray, expected: bytes, timeout: float
    ) -> None:
        deadline = time.monotonic() + timeout
        while expected not in output and time.monotonic() < deadline:
            readable, _, _ = select.select([descriptor], [], [], 0.1)
            if readable:
                chunk = os.read(descriptor, 4096)
                if not chunk:
                    break
                output.extend(chunk)
        if expected not in output:
            raise AssertionError(
                f"prompt {expected!r} not observed: {bytes(output)!r}"
            )

    def test_left_arrow_backspace_and_chinese_inline_editing(self) -> None:
        rendered, raw_output = self._run_pty(
            "from tsm_agt.cli_input import platform_line_input; "
            "print('RESULT=' + platform_line_input()('you> '))",
            [
                (b"you> ", "你好worXd".encode(), 0.15),
                ("你好worXd".encode(), b"\x1b[D", 0.15),
                (b"", b"\x7f", 0.15),
                (b"", b"l\n", 0.15),
            ],
        )
        self.assertIn("RESULT=你好world", rendered)
        screen = _AnsiScreen(100, 12)
        screen.feed(raw_output)
        self.assertIn("RESULT=你好world", screen.text())
        self.assertFalse(any(
            "你 好" in line or "好 w" in line for line in screen.lines
        ), screen.text())

    def test_long_input_can_be_deleted_past_terminal_width(self) -> None:
        initial = b"a" * 160
        rendered, _ = self._run_pty(
            "from tsm_agt.cli_input import platform_line_input; "
            "print('LENGTH=' + str(len(platform_line_input()('you> '))))",
            [
                (b"you> ", initial, 0.15),
                (initial[-20:], b"\x7f" * 150 + b"\n", 0.15),
            ],
        )
        self.assertIn("LENGTH=10", rendered)

    def test_up_arrow_recalls_previous_input(self) -> None:
        rendered, _ = self._run_pty(
            "from tsm_agt.cli_input import platform_line_input; r=platform_line_input(); "
            "a=r('one> '); b=r('two> '); print('RESULT=' + a + '|' + b)",
            [
                (b"one> ", b"history-entry\n", 0.15),
                (b"two> ", b"\x1b[A", 0.15),
                (b"history-entry", b"\n", 0.10),
            ],
        )
        self.assertIn("RESULT=history-entry|history-entry", rendered)

    def test_full_chat_receives_unicode_text_after_inline_deletion(self) -> None:
        source = (
            "import asyncio,tempfile; from pathlib import Path; "
            "from tsm_agt.bootstrap import compose_fixture_application; "
            "from tsm_agt.adapters.fixture import EchoModelProvider; "
            "from tsm_agt.cli import _chat; "
            "d=tempfile.TemporaryDirectory(); root=Path(d.name); "
            "app=compose_fixture_application(model_adapter=EchoModelProvider(),"
            "tool_adapters=()); "
            "raise SystemExit(asyncio.run(_chat(root,"
            "application_factory=lambda:app)))"
        )
        rendered, raw_output = self._run_pty(
            source,
            [
                (b"you> ", "你好worXd".encode(), 0.15),
                ("你好worXd".encode(), b"\x1b[D", 0.15),
                (b"", b"\x7f", 0.15),
                (b"", b"l\n", 0.15),
                (b"agent> ", b"/exit\n", 0.15),
            ],
            timeout=8.0,
        )
        self.assertIn("agent> 你好world", rendered)
        screen = _AnsiScreen(100, 24)
        screen.feed(raw_output)
        self.assertFalse(any(
            "你 好" in line or "好 w" in line for line in screen.lines
        ), screen.text())


class _AnsiScreen:
    """Minimal VT100 screen used to assert the visible result, not raw bytes."""

    def __init__(self, columns: int, rows: int) -> None:
        self.columns = columns
        self.rows = rows
        self.cells = [[" " for _ in range(columns)] for _ in range(rows)]
        self.row = 0
        self.column = 0
        self._decoder = __import__("codecs").getincrementaldecoder("utf-8")()

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple("".join(row).rstrip() for row in self.cells)

    def text(self) -> str:
        return "\n".join(line for line in self.lines if line)

    def feed(self, data: bytes) -> None:
        index = 0
        while index < len(data):
            byte = data[index]
            if byte == 0x1B:
                index = self._escape(data, index + 1)
                continue
            if byte == 13:
                self.column = 0
            elif byte == 10:
                self.row = min(self.rows - 1, self.row + 1)
            elif byte == 8:
                self.column = max(0, self.column - 1)
            elif byte >= 32:
                character = self._decoder.decode(bytes((byte,)), final=False)
                if character:
                    width = 2 if __import__("unicodedata").east_asian_width(character) in {"W", "F"} else 1
                    if self.column + width > self.columns:
                        self.row = min(self.rows - 1, self.row + 1)
                        self.column = 0
                    self.cells[self.row][self.column] = character
                    for offset in range(1, width):
                        self.cells[self.row][self.column + offset] = ""
                    self.column = min(self.columns - 1, self.column + width)
            index += 1

    def _escape(self, data: bytes, index: int) -> int:
        if index >= len(data) or data[index] != ord("["):
            return index + 1
        index += 1
        start = index
        while index < len(data) and not 0x40 <= data[index] <= 0x7E:
            index += 1
        if index >= len(data):
            return index
        final = chr(data[index])
        raw = data[start:index].decode("ascii", errors="ignore")
        params = raw.lstrip("?").split(";") if raw.lstrip("?") else []
        amount = int(params[0] or "1") if params and params[0].isdigit() else 1
        if final == "A":
            self.row = max(0, self.row - amount)
        elif final == "B":
            self.row = min(self.rows - 1, self.row + amount)
        elif final == "C":
            self.column = min(self.columns - 1, self.column + amount)
        elif final == "D":
            self.column = max(0, self.column - amount)
        elif final == "G":
            self.column = max(0, min(self.columns - 1, amount - 1))
        elif final in {"H", "f"}:
            row = int(params[0] or "1") if params else 1
            column = int(params[1] or "1") if len(params) > 1 else 1
            self.row = max(0, min(self.rows - 1, row - 1))
            self.column = max(0, min(self.columns - 1, column - 1))
        elif final == "K":
            mode = int(params[0] or "0") if params else 0
            if mode == 2:
                self.cells[self.row] = [" " for _ in range(self.columns)]
            elif mode == 1:
                for column in range(self.column + 1):
                    self.cells[self.row][column] = " "
            else:
                for column in range(self.column, self.columns):
                    self.cells[self.row][column] = " "
        elif final == "J" and params and params[0] == "2":
            self.cells = [[" " for _ in range(self.columns)] for _ in range(self.rows)]
        return index + 1


if __name__ == "__main__":
    unittest.main()
