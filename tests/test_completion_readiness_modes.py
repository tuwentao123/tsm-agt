"""Completion readiness must stay a diagnostic, not a trap.

`LEGACY_GATE` lets the readiness gate direct execution: a **required** gap with
no available effect makes the turn keep trying, and when the correction budget is
spent the turn ends suspended. That is correct for a gap the model can still
close. It is a trap when the gap is unclosable by construction -- which is what a
single wrong path guess produces:

    the model binds an evidence question, guesses a path that does not exist,
    ``read_file`` answers NOT_FOUND with ``retryable=false``, so
    ``EvidenceQuestionProjection.observe`` marks the question BLOCKED, and
    ``Kernel._completion_gaps`` then emits
    required=True / recoverable=False / required_effects=() / candidate_tools=().

Nothing can ever close that gap, so the Task parks in AWAITING_USER with the work
undone (real ledger: task-8d1ec232a0e54910852a029dcf11b03f, 2026-10-01).

Hermes and Codex both avoid this shape: neither has any obligation/gap record at
all, and Hermes' one stop gate is bounded and defaulted OFF. tsm-agt already ships
the same policy as the two non-gating modes, so these tests pin that choosing one
of them is what stops a wrong guess from stranding the turn.

The model endpoint is a local OpenAI-compatible stub, so the whole loop runs
offline while the transport, planner, tool batch and readiness evaluation are all
production code.
"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from tsm_agt.bootstrap import (
    compose_openai_compatible_engineering_application,
)
from tsm_agt.core import (
    AgentContinuationSuspended, AgentTurnResult, AgentTurnSuspended, TaskState,
)
from tsm_agt.ports import CompletionReadinessMode, RuntimeStorePort

GOAL = "分析下这个项目是如何做意图识别的"
#: The exact path the real run guessed. It cannot exist: ``core`` is a package.
MISSING_PATH = "src/tsm_agt/core.py"
EXECUTION_PATH = (
    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
)


def _tool_call(index: int, name: str, arguments: dict) -> dict:
    return {
        "id": "chatcmpl-stub", "object": "chat.completion", "created": 0,
        "model": "stub",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": f"call_{index}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _text(text: str) -> dict:
    return {
        "id": "chatcmpl-stub", "object": "chat.completion", "created": 0,
        "model": "stub",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _proposal() -> dict:
    return {
        "schema_version": 1, "goal": GOAL, "scope": [], "constraints": [],
        "acceptance_criteria": [{
            "criterion_id": "ac1",
            "description": "answer the intent-recognition question",
            "verification_kind": "rubric",
        }],
        "outcomes": [{
            "outcome_id": "o1", "description": "deliver the analysis",
            "kind": "ANSWER", "required_effects": [], "required": True,
        }],
        "continuation_policy": {"mode": "NONE"},
    }


class _WrongGuessHandler(BaseHTTPRequestHandler):
    """Plays the model: bind a question, guess a missing path, then answer."""

    def log_message(self, *args):
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        names = [
            (item.get("function") or {}).get("name", "")
            for item in (body.get("tools") or [])
        ]
        roles = [message.get("role") for message in (body.get("messages") or [])]
        planner = next((name for name in names if "submit_task_spec" in name), None)
        reader = next((name for name in names if "read_file" in name), None)
        if planner is not None:
            payload = _tool_call(0, planner, _proposal())
        elif reader is not None and "tool" not in roles:
            # The evidence question is bound to the call, and the call fails:
            # that pairing is what makes the question BLOCKED for good.
            payload = _tool_call(1, reader, {
                "evidence_question": {
                    "question_id": "Q1",
                    "question": "意图识别的核心代码在哪里？",
                    "expected_scope": "src",
                },
                "tool_arguments": {
                    "path": MISSING_PATH, "start_line": 1, "max_lines": 50,
                },
            })
        else:
            payload = _text("分析已完成（基于已读到的证据）。")
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class CompletionReadinessModeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            self.server = ThreadingHTTPServer(
                ("127.0.0.1", 0), _WrongGuessHandler,
            )
        except OSError as error:  # pragma: no cover - restricted sandbox
            self.skipTest(f"cannot bind a loopback listener: {error}")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    async def _run(self, mode: CompletionReadinessMode):
        host, port = self.server.server_address
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[project]\nname='readiness-modes'\n", encoding="utf-8",
            )
            application = compose_openai_compatible_engineering_application(
                base_url=f"http://{host}:{port}/v1", model="stub-model",
                api_key="stub-key",
                database_path=root / ".agent" / "runtime.db",
                model_streaming=False, model_strict_tool_schema=False,
                model_timeout_seconds=20.0, model_max_retries=0,
                model_retry_backoff_seconds=0.0,
                completion_readiness_mode=mode,
            )
            await application.registry.start_all()
            try:
                kernel = application.kernel
                session = await kernel.create_session("readiness modes")
                task = await kernel.create_task(
                    GOAL, root, session_id=session.session_id,
                )
                for state in EXECUTION_PATH:
                    task = await kernel.transition_task(
                        task.task_id, state, f"readiness {state.value.lower()}",
                    )
                result = await kernel.run_agent_turn(
                    task.task_id, GOAL, max_model_calls=8, max_tool_calls=12,
                )
                current = await kernel.get_task(task.task_id)
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                return result, current, events
            finally:
                await application.registry.stop_all()

    def _unclosable_gaps(self, events) -> list[tuple]:
        for event in events:
            if event.event_type != "completion.readiness_evaluated":
                continue
            return [
                (gap.get("kind"), gap.get("status"), gap.get("required"),
                 gap.get("recoverable"), tuple(gap.get("candidate_tools") or ()))
                for gap in (event.payload.get("gaps") or ())
                if gap.get("required")
                and not gap.get("required_effects")
                and not (gap.get("candidate_tools") or ())
            ]
        return []

    async def test_a_blocked_question_is_unclosable_by_construction(self) -> None:
        """Characterises the shape that stranded the real Task.

        A required gap with no effect and no candidate tool can never be closed,
        so this is not a "keep trying" signal no matter which mode reads it.
        """

        result, current, events = await self._run(CompletionReadinessMode.LEGACY_GATE)
        # LEGACY_GATE acts on it: the turn is suspended and the work is not done.
        # The suspension types are siblings, not a hierarchy, so name the one a
        # spent correction budget produces.
        self.assertIsInstance(result, AgentContinuationSuspended)
        self.assertNotIsInstance(result, AgentTurnResult)
        self.assertIs(current.state, TaskState.AWAITING_USER)
        self.assertEqual(
            self._unclosable_gaps(events),
            [("EVIDENCE_QUESTION", "BLOCKED", True, False, ())],
        )

    async def test_the_non_gating_modes_return_instead_of_stranding(self) -> None:
        """The remedy: report the same gap as a fact and let the Task proceed."""

        for mode in (
            CompletionReadinessMode.OBSERVE_ONLY,
            CompletionReadinessMode.AGENT_DECIDES,
        ):
            with self.subTest(mode=mode.value):
                result, current, events = await self._run(mode)
                self.assertIsInstance(result, AgentTurnResult)
                self.assertIsNot(current.state, TaskState.AWAITING_USER)


if __name__ == "__main__":
    unittest.main()
