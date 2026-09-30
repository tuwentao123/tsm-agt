"""The engineering loop end to end over a **real HTTP** model transport.

Every other offline suite injects a fake transport into the model adapter, so the
socket layer, the provider-side tool-name codec and the request/response shape are
never exercised together with the rest of the Runtime. This test keeps only the
model endpoint local: it is an OpenAI-compatible server on 127.0.0.1, and
everything else is production code -- the planner, the approval gate, the
workspace sandbox, the journaled ``core.apply_patch`` mutation, the SQLite ledger,
verification and the one terminal-state mapping.

That is the shape a real deployment runs, minus the vendor endpoint itself, so a
regression in any of those seams shows up here instead of in a user's session.
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
    AcceptanceStatus, AgentTurnResult, AgentTurnSuspended, ApprovalDecision,
    TaskState,
)

GOAL = "Create DELIVERABLE.md in this project recording that the change is done."
CONTENT = "# Deliverable\nstatus: done\n"


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
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
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
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def _proposal() -> dict:
    """One machine-checkable criterion and one delivery outcome.

    Deliberately no rubric: a judged criterion would need another model and would
    make the verdict depend on the stub's prose rather than on the mutation.
    """

    return {
        "schema_version": 1,
        "goal": GOAL,
        "scope": [],
        "constraints": [],
        "acceptance_criteria": [{
            "criterion_id": "ac-durable-change",
            "description": "the workspace change is durable",
            "verification_kind": "workspace_integrity",
        }],
        "outcomes": [{
            "outcome_id": "oc-deliverable",
            "description": "deliver DELIVERABLE.md",
            "kind": "ARTIFACT_DELIVERY",
            "required_effects": ["mutate"],
            "required": True,
        }],
        "continuation_policy": {"mode": "NONE"},
    }


class _StubModelHandler(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint that plays the three calls the loop makes."""

    requested_tools: list[list[str]] = []

    def log_message(self, *args):  # keep the test output clean
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        names = [
            (item.get("function") or {}).get("name", "")
            for item in (body.get("tools") or [])
        ]
        roles = [message.get("role") for message in (body.get("messages") or [])]
        type(self).requested_tools.append(names)

        planner = next((name for name in names if "submit_task_spec" in name), None)
        if planner is not None:
            payload = _tool_call(0, planner, _proposal())
        elif "tool" not in roles:
            patch = next(
                name for name in names
                if "apply_patch" in name and "patches" not in name
            )
            payload = _tool_call(1, patch, {
                "path": "DELIVERABLE.md",
                # A new file: no prior hash, and one edit with empty old_text.
                "expected_hash": None,
                "edits": [{"old_text": "", "new_text": CONTENT}],
            })
        else:
            payload = _text("Created DELIVERABLE.md recording the change.")
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class EngineeringLoopOverHttpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubModelHandler)
        except OSError as error:  # pragma: no cover - restricted sandbox
            self.skipTest(f"cannot bind a loopback listener: {error}")
        _StubModelHandler.requested_tools = []
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    async def test_plan_approve_mutate_verify_and_finalize(self) -> None:
        host, port = self.server.server_address
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[project]\nname='stub-e2e'\n", encoding="utf-8",
            )
            application = compose_openai_compatible_engineering_application(
                base_url=f"http://{host}:{port}/v1", model="stub-model",
                api_key="stub-key",
                database_path=root / ".agent" / "runtime.db",
                model_streaming=False, model_strict_tool_schema=False,
                model_timeout_seconds=20.0, model_max_retries=0,
                model_retry_backoff_seconds=0.0,
            )
            await application.registry.start_all()
            self.addAsyncCleanup(application.registry.stop_all)
            kernel = application.kernel

            session = await kernel.create_session("http loop")
            task = await kernel.create_task(
                GOAL, root, session_id=session.session_id,
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await kernel.transition_task(
                    task.task_id, state, f"http-loop {state.value.lower()}",
                )

            # The planner is reached from inside the turn, over the real socket.
            result = await kernel.run_agent_turn(
                task.task_id, GOAL, max_model_calls=8, max_tool_calls=12,
            )

            # The mutation is R1, so the turn must stop for approval first: the
            # file may not exist yet, and must exist afterwards.
            self.assertIsInstance(result, AgentTurnSuspended)
            assert isinstance(result, AgentTurnSuspended)
            self.assertFalse((root / "DELIVERABLE.md").exists())
            result = await kernel.resolve_agent_approval(
                result.approval_request_id, ApprovalDecision.APPROVE,
                "approve the stub patch",
            )
            self.assertIsInstance(result, AgentTurnResult)
            assert isinstance(result, AgentTurnResult)
            self.assertEqual(result.tool_calls, 1)
            self.assertEqual(
                (root / "DELIVERABLE.md").read_text(encoding="utf-8"), CONTENT,
            )

            spec = await kernel.get_task_spec(task.task_id)
            self.assertIn(
                "ac-durable-change",
                [item.criterion_id for item in spec.acceptance_criteria],
            )

            await kernel.transition_task(
                task.task_id, TaskState.VERIFYING, "verify the http loop",
            )
            verification = await kernel.verify_task_acceptance(task.task_id)
            self.assertIs(verification.status, AcceptanceStatus.PASSED)
            finalized = await kernel.finalize_acceptance(
                task.task_id, verification.status,
            )
            self.assertIs(finalized.state, TaskState.SUCCEEDED)

            # The provider saw the planner call and the agent's own tool schema,
            # which is what proves the codec round-tripped over the socket.
            # The planner has a tool list of its own, so the two are separate
            # requests -- exactly the shape the real endpoint would see.
            seen = _StubModelHandler.requested_tools
            self.assertTrue(
                any(
                    any("submit_task_spec" in name for name in names)
                    for names in seen
                ),
                seen,
            )
            self.assertTrue(
                any(
                    any("apply_patch" in name for name in names)
                    for names in seen
                ),
                seen,
            )


if __name__ == "__main__":
    unittest.main()
