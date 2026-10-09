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
all, and Hermes' one stop gate is bounded and defaulted OFF -- its nudges never
strand a turn. tsm-agt now does the same: the gate nudges at most once, then the
turn ends with the model's own answer, in **every** readiness mode. These tests
pin that.

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
    OrchestrationConfiguration,
    compose_openai_compatible_engineering_application,
)
from tsm_agt.core.single_loop import OrchestrationMode
from tsm_agt.cli import _finalize_standalone_agent_result
from tsm_agt.core import (
    AgentTurnResult, TaskState,
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
                orchestration_configuration=OrchestrationConfiguration(
                    mode=OrchestrationMode.LEGACY
                ),
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
                # Close the Task through the production one-shot entry point, not
                # by hand: this is what the CLI/SDK do with a finished turn.
                exit_code = (
                    await _finalize_standalone_agent_result(
                        application, task.task_id, result, lambda _line: None,
                    )
                    if isinstance(result, AgentTurnResult) else None
                )
                current = await kernel.get_task(task.task_id)
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                return result, current, events, exit_code
            finally:
                await application.registry.stop_all()

    async def test_a_blocked_question_no_longer_strands_the_turn(self) -> None:
        """The incident, fixed: a wrong guess must not park the Task.

        The gap is still unclosable by construction and the Runtime still reports
        it truthfully. What changed is that it no longer suspends: Hermes/Codex
        have no equivalent stop, so the model's own answer ends the turn.
        """

        result, current, events, exit_code = await self._run(
            CompletionReadinessMode.LEGACY_GATE
        )
        self.assertIsInstance(result, AgentTurnResult)
        # The whole point: the Task closes as SUCCEEDED instead of parking in
        # AWAITING_USER, and the exit code says so.
        self.assertEqual(exit_code, 0)
        self.assertIs(current.state, TaskState.SUCCEEDED)
        # The unprovable read is not turned into an obligation the model can
        # never discharge, and nothing suspends the turn to ask for it.
        self.assertFalse(any(
            event.event_type == "continuation.requested" for event in events
        ))
        self.assertFalse(any(
            event.event_type == "completion.readiness_evaluated"
            and (event.payload.get("gaps") or ())
            for event in events
        ))

    async def test_every_mode_closes_the_same_way(self) -> None:
        """The mode tunes in-turn nudges only; completion is mode-independent."""

        for mode in (
            CompletionReadinessMode.OBSERVE_ONLY,
            CompletionReadinessMode.AGENT_DECIDES,
        ):
            with self.subTest(mode=mode.value):
                result, current, events, exit_code = await self._run(mode)
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(exit_code, 0)
                self.assertIs(current.state, TaskState.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()


class _RecoveringHandler(BaseHTTPRequestHandler):
    """Plays the model recovering from its own wrong guess.

    It binds question Q1, guesses a path that cannot exist, and then -- instead
    of giving up -- answers the *same* question with a different OBSERVE tool.
    Re-binding Q1 is what re-opens it, so this is the mechanism the fix relies
    on: the failed call was not retryable, the question still is.
    """

    seen_tools: list[str] = []

    def log_message(self, *args):
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        names = [
            (item.get("function") or {}).get("name", "")
            for item in (body.get("tools") or [])
        ]
        results = [
            message for message in (body.get("messages") or ())
            if message.get("role") == "tool"
        ]
        planner = next((name for name in names if "submit_task_spec" in name), None)
        reader = next((name for name in names if "read_file" in name), None)
        lister = next((name for name in names if "list_files" in name), None)
        if planner is not None:
            payload = _tool_call(0, planner, _proposal())
        elif not results and reader is not None:
            type(self).seen_tools.append("core.read_file")
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
        elif lister is not None and "core.list_files" not in type(self).seen_tools:
            type(self).seen_tools.append("core.list_files")
            payload = _tool_call(2, lister, {
                "evidence_question": {
                    "question_id": "Q1",
                    "question": "意图识别的核心代码在哪里？",
                    "expected_scope": "src",
                },
                "tool_arguments": {"path": "src/tsm_agt/core", "limit": 50},
            })
        else:
            payload = _text("意图识别的核心代码在 src/tsm_agt/core/ 下。")
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class BlockedQuestionRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            self.server = ThreadingHTTPServer(
                ("127.0.0.1", 0), _RecoveringHandler,
            )
        except OSError as error:  # pragma: no cover - restricted sandbox
            self.skipTest(f"cannot bind a loopback listener: {error}")
        _RecoveringHandler.seen_tools = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    async def test_a_blocked_question_is_reopened_by_another_observe_tool(self) -> None:
        host, port = self.server.server_address
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[project]\nname='reopen'\n", encoding="utf-8",
            )
            (root / "src" / "tsm_agt" / "core").mkdir(parents=True)
            (root / "src" / "tsm_agt" / "core" / "__init__.py").write_text(
                "", encoding="utf-8",
            )
            application = compose_openai_compatible_engineering_application(
                base_url=f"http://{host}:{port}/v1", model="stub-model",
                api_key="stub-key",
                database_path=root / ".agent" / "runtime.db",
                model_streaming=False, model_strict_tool_schema=False,
                model_timeout_seconds=20.0, model_max_retries=0,
                model_retry_backoff_seconds=0.0,
                # The default mode: the one that used to strand the Task.
                completion_readiness_mode=CompletionReadinessMode.LEGACY_GATE,
                orchestration_configuration=OrchestrationConfiguration(
                    mode=OrchestrationMode.LEGACY
                ),
            )
            await application.registry.start_all()
            self.addAsyncCleanup(application.registry.stop_all)
            kernel = application.kernel
            session = await kernel.create_session("reopen")
            task = await kernel.create_task(
                GOAL, root, session_id=session.session_id,
            )
            for state in EXECUTION_PATH:
                task = await kernel.transition_task(
                    task.task_id, state, f"reopen {state.value.lower()}",
                )
            result = await kernel.run_agent_turn(
                task.task_id, GOAL, max_model_calls=8, max_tool_calls=12,
            )
            current = await kernel.get_task(task.task_id)

            # The wrong guess did not end the Task: the model was given the
            # question back together with tools that can still answer it.
            self.assertIsInstance(result, AgentTurnResult)
            self.assertIsNot(current.state, TaskState.AWAITING_USER)
            self.assertEqual(
                _RecoveringHandler.seen_tools,
                ["core.read_file", "core.list_files"],
            )
            self.assertTrue((root / "src" / "tsm_agt" / "core" / "__init__.py").exists())

            # And the gap the failed call created was closable, not a dead end.
            events = await application.registry.require(
                RuntimeStorePort
            ).read_events(task.task_id)
            blocked = [
                gap for event in events
                if event.event_type == "completion.readiness_evaluated"
                for gap in (event.payload.get("gaps") or ())
                if gap.get("kind") == "EVIDENCE_QUESTION"
            ]
            for gap in blocked:
                self.assertTrue(gap["required"])
                if gap["status"] == "BLOCKED":
                    self.assertTrue(gap["recoverable"])
                    self.assertTrue(gap["candidate_tools"])


class ReadinessEventPayloadTest(unittest.TestCase):
    """The readiness event payload stays readable without the proof layer.

    ``coverage_gap_ids`` existed only to mark which gaps the deleted coverage
    layer had contributed; with no producer the key is gone. The gap list itself
    is unchanged, because ``CompletionGap`` is still the readiness vocabulary.
    """

    def payload(self, gaps):
        from tsm_agt.core.kernel import _readiness_event_payload
        from tsm_agt.ports import (
            CompletionReadinessAction, CompletionReadinessDecision,
            CompletionReadinessMode, CompletionReadinessProbe,
            CompletionReadinessState, ToolEffect,
        )

        probe = CompletionReadinessProbe(
            goal="g", gaps=tuple(gaps), remaining_model_calls=5,
            remaining_tool_calls=5, available_read_tools=(),
            available_effects=frozenset({ToolEffect.OBSERVE}),
        )
        decision = CompletionReadinessDecision(
            CompletionReadinessAction.EXHAUSTED, "coverage_not_satisfied",
            CompletionReadinessState(), tuple(gaps),
        )
        return _readiness_event_payload(
            decision, probe, turn_id="t1",
            mode=CompletionReadinessMode.LEGACY_GATE, forced_wrap_up=False,
        )

    def ordinary_gap(self):
        from tsm_agt.ports import CompletionGap, ToolEffect

        return CompletionGap(
            gap_id="task-spec:AC-9", kind="TASK_SPEC_WORKSPACE_INTEGRITY",
            description="workspace differs from the journal", status="FAILED",
            required=True, required_effects=(ToolEffect.MUTATE,),
        )

    def test_payload_reports_every_gap(self) -> None:
        payload = self.payload((self.ordinary_gap(),))
        self.assertEqual(payload["gap_count"], 1)
        self.assertEqual(len(payload["gaps"]), 1)
        self.assertEqual(payload["gaps"][0]["gap_id"], "task-spec:AC-9")
        self.assertNotIn("coverage_gap_ids", payload)

    def test_payload_reports_mode_and_enforcement(self) -> None:
        from tsm_agt.ports import CompletionReadinessMode

        payload = self.payload(())
        self.assertEqual(payload["mode"], CompletionReadinessMode.LEGACY_GATE.value)
        self.assertTrue(payload["enforced"])
        self.assertEqual(payload["gap_count"], 0)
        self.assertEqual(payload["gaps"], [])
