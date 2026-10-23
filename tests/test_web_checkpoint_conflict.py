"""A failed approval resume must be visible, not left as waiting for consent."""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from tsm_agt.ports import RuntimeEvent
from tests import test_web_session_restore as web_fixtures

web_app = importlib.import_module("tsm_agt.web.app")


def javascript_function(name):
    source = web_app.INDEX_HTML
    start = source.index(f"function {name}(")
    if source[max(0, start - 6):start] == "async ":
        start -= 6
    return source[start:source.index("\n}\n", start) + 2]


class WebCheckpointConflictTest(unittest.IsolatedAsyncioTestCase):
    async def test_reloaded_old_conflict_links_invalidated_approval_by_exact_request(self):
        events = [
            RuntimeEvent("started", "task-new", 1, "turn.started", {"turn_id": "turn-new"}),
            RuntimeEvent("approval", "task-new", 2, "approval.requested", {
                "turn_id": "turn-new", "request_id": "approval-new", "action": "Apply patch",
            }),
            RuntimeEvent("conflict", "task-new", 3, "checkpoint.conflict", {
                "turn_id": "turn-new", "conflicts": ["policy_hash"],
            }),
            # Old rows have no turn_id. Link through the request, never the
            # currently visible Turn, which may belong to another request.
            RuntimeEvent("invalidated", "task-new", 4, "approval.invalidated", {
                "request_id": "approval-new", "reason": "checkpoint_identity_conflict",
            }),
            RuntimeEvent("unrelated", "task-new", 5, "approval.invalidated", {
                "request_id": "unknown-old-request", "reason": "old request",
            }),
        ]
        server = web_fixtures.SessionRestoreEndpointTest._server(
            phase1_state="INTERRUPTED", task_events={"task-new": events},
            input_turns=({"task_id": "task-new", "turn_id": "turn-new"},),
        )
        with patch.object(web_app, "runtime_server", server):
            result = await web_app.get_session("session-new")
        turn = next(item for item in result["turns"] if item["turn_id"] == "turn-new")
        progress = turn["progress"]
        self.assertEqual([item["kind"] for item in progress],
                         ["turn_started", "waiting", "turn_conflict", "approval_invalidated"])
        self.assertIn("policy_hash", progress[2]["detail"])
        self.assertEqual(progress[3]["request_id"], "approval-new")
        self.assertIsNone(result["waiting"])

    async def test_sse_terminal_conflict_delivers_failure_not_just_phase1_interrupted(self):
        server = web_fixtures._StreamServer(phase1_state="INTERRUPTED", status="interrupted")
        reason = web_app._checkpoint_conflict_reason(["policy_hash"])
        with patch.object(web_app, "runtime_server", server), patch.object(
            web_app, "_task_failure_reason", return_value=reason,
        ):
            response = await web_app.stream("task-stream", after=9)
            frame = await anext(response.body_iterator)
            if isinstance(frame, bytes):
                frame = frame.decode()
            data = json.loads(frame.removeprefix("data: ").strip())
            self.assertTrue(data["final"])
            self.assertEqual(data["failure_reason"], reason)
            self.assertEqual(data["status"], "interrupted")
            with self.assertRaises(StopAsyncIteration):
                await anext(response.body_iterator)

    async def test_polling_endpoint_also_delivers_the_conflict_reason(self):
        server = web_fixtures._StreamServer(phase1_state="INTERRUPTED", status="interrupted")
        with patch.object(web_app, "runtime_server", server), patch.object(
            web_app, "_task_failure_reason", return_value="policy conflict",
        ):
            data = await web_app.get_task("task-stream")
        self.assertEqual(data["failure_reason"], "policy conflict")


class CheckpointConflictUiTest(unittest.TestCase):
    def test_polling_conflict_does_not_append_the_old_answer_before_finalization(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js unavailable")
        source = web_app.INDEX_HTML
        start = source.index("const reconcile = async () => {")
        reconcile = source[start:source.index("\n  };", start) + 5]
        script = """
const taskId = 'task'; const conversationId = 'session'; const card = {task: {}};
const answers = []; let finished = false;
const fetch = async () => ({ok: true, json: async () => ({state: 'CONFLICT',
  phase1_state: 'INTERRUPTED', status: 'interrupted', assistant_text: 'STALE ANSWER'})});
const taskMessage = () => card;
const appendTaskAssistantText = (_task, text) => answers.push(text);
const applyTaskProjection = () => {}; const applyTaskProtocol = () => {};
const clearApprovalMessage = () => {};
const TERMINAL_STATES = ['INTERRUPTED'];
const finish = async () => { finished = true; };
""" + reconcile + """
reconcile().then(() => process.stdout.write(JSON.stringify({answers, finished})));
"""
        completed = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["answers"], [])
        self.assertTrue(result["finished"])

    def test_javascript_marks_invalidated_wait_as_closed_and_conflict_as_error(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js unavailable")
        items = [
            {"kind": "waiting", "waiting_kind": "approval", "request_id": "a"},
            {"kind": "turn_conflict", "detail": "policy_hash changed"},
            {"kind": "approval_invalidated", "request_id": "a"},
        ]
        script = javascript_function("turnProgressLines") + (
            "\nprocess.stdout.write(JSON.stringify(turnProgressLines(" + json.dumps(items) + ")));"
        )
        completed = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
        lines = json.loads(completed.stdout)
        self.assertNotEqual(lines[0]["tone"], "active")
        self.assertIn("已处理", lines[0]["text"])
        self.assertEqual(lines[1]["tone"], "failed")
        self.assertIn("policy_hash", lines[1]["text"])
        self.assertIn("已失效", lines[2]["text"])

    def test_final_error_is_not_hidden_by_previous_turn_answer(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js unavailable")
        script = """
const activeConversationId = 'session'; const errors = []; const answers = [];
const card = {task: {}};
const taskMessage = () => card;
const appendTaskAssistantText = (_task, text) => answers.push(text);
const appendTaskProgress = () => {};
const showError = (text) => errors.push(text);
const describeFailure = (text) => text;
const applyTaskProjection = () => {}; const applyTaskProtocol = () => {};
const clearApprovalMessage = () => {}; const updateComposerButtonState = () => {};
const refreshConversationHistory = async () => true;
""" + javascript_function("renderFinalResult") + """
renderFinalResult('task', {state: 'CONFLICT', phase1_state: 'INTERRUPTED',
  status: 'interrupted', assistant_text: 'STALE PREVIOUS ANSWER', failure_reason: 'policy_hash changed'},
  'session').then(() => process.stdout.write(JSON.stringify({errors, answers, task: card.task})));
"""
        completed = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["answers"], [])
        self.assertIn("policy_hash changed", result["errors"])
        self.assertIsNone(result["task"]["waiting"])
        self.assertEqual(result["task"]["status"], "interrupted")
