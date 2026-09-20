from __future__ import annotations

import json
import os
import re
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _HangingSseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    response_started = threading.Event()
    release_response = threading.Event()

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler API
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        chunk = {
            "id": "chat-hanging",
            "choices": [{
                "delta": {"content": "partial output"},
                "finish_reason": None,
            }],
        }
        self.wfile.write(
            ("data: " + json.dumps(chunk) + "\n\n").encode("utf-8")
        )
        self.wfile.flush()
        self.response_started.set()
        self.release_response.wait(10.0)

    def log_message(self, _format: str, *args: object) -> None:
        pass


@unittest.skipIf(os.name == "nt", "POSIX SIGINT subprocess contract")
class ChatSigintIntegrationTest(unittest.TestCase):
    def test_sigint_while_waiting_for_input_saves_session_without_traceback(self):
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".env").write_text(
                "\n".join((
                    "TSM_AGT_MODEL_BASE_URL=http://127.0.0.1:1",
                    "TSM_AGT_MODEL=test-model",
                    "TSM_AGT_MODEL_API_KEY=test-key",
                )),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(project_root / "src")
            process = subprocess.Popen(
                [
                    sys.executable, "-m", "tsm_agt.cli", "chat",
                    "--workspace", str(workspace), "--title", "idle SIGINT",
                ],
                cwd=project_root, env=environment, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(b"n\n")
            process.stdin.flush()
            prefix = bytearray()
            deadline = time.monotonic() + 8.0
            while b"you> " not in prefix and time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], 0.1)
                if ready:
                    prefix.extend(os.read(process.stdout.fileno(), 4096))
                if process.poll() is not None:
                    break
            self.assertIn(b"you> ", prefix, prefix.decode(errors="replace"))
            started = time.monotonic()
            process.send_signal(signal.SIGINT)
            remaining_stdout, raw_stderr = process.communicate(timeout=3.0)
            stdout = (bytes(prefix) + remaining_stdout).decode(errors="replace")
            stderr = raw_stderr.decode(errors="replace")
            elapsed = time.monotonic() - started
            self.assertEqual(process.returncode, 0, (stdout, stderr))
            self.assertLess(elapsed, 2.0)
            self.assertIn("session saved:", stdout)
            self.assertNotIn("Traceback", stdout + stderr)
            self.assertNotIn("CancelledError", stdout + stderr)

    def test_one_sigint_exits_quickly_without_traceback_and_preserves_task(self):
        _HangingSseHandler.response_started.clear()
        _HangingSseHandler.release_response.clear()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HangingSseHandler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        project_root = Path(__file__).resolve().parents[1]
        try:
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                (workspace / ".env").write_text(
                    "\n".join((
                        f"TSM_AGT_MODEL_BASE_URL=http://127.0.0.1:{server.server_port}",
                        "TSM_AGT_MODEL=test-model",
                        "TSM_AGT_MODEL_API_KEY=test-key",
                    )),
                    encoding="utf-8",
                )
                environment = os.environ.copy()
                environment["PYTHONPATH"] = str(project_root / "src")
                process = subprocess.Popen(
                    [
                        sys.executable, "-m", "tsm_agt.cli", "chat",
                        "--workspace", str(workspace), "--title", "SIGINT test",
                    ],
                    cwd=project_root, env=environment, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                assert process.stdin is not None
                process.stdin.write("n\nwrite a long answer\n")
                process.stdin.flush()
                self.assertTrue(
                    _HangingSseHandler.response_started.wait(8.0),
                    "CLI did not reach the streaming model",
                )
                time.sleep(0.2)
                started = time.monotonic()
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=3.0)
                elapsed = time.monotonic() - started

                self.assertEqual(process.returncode, 130, (stdout, stderr))
                self.assertLess(elapsed, 2.0)
                # Planner/provider protocol fragments are internal state. A
                # user interrupt must preserve the resumable checkpoint without
                # leaking an incomplete planning response into the transcript.
                self.assertNotIn("partial output", stdout)
                self.assertIn("interrupted safely; resume with:", stdout)
                self.assertNotIn("Traceback", stdout + stderr)
                self.assertNotIn("CancelledError", stdout + stderr)
                task_match = re.search(r"task: (task-[a-f0-9]+)", stdout)
                self.assertIsNotNone(task_match, stdout)
                task_id = task_match.group(1)
                with sqlite3.connect(workspace / ".agent" / "runtime.db") as db:
                    row = db.execute(
                        "SELECT data_json FROM runtime_tasks WHERE task_id = ?",
                        (task_id,),
                    ).fetchone()
                self.assertIsNotNone(row)
                task_data = json.loads(row[0])
                self.assertEqual(task_data["state"], "INTERRUPTED")
                self.assertIsNotNone(task_data["active_agent_checkpoint"])
        finally:
            _HangingSseHandler.release_response.set()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
