"""Web approval ingress: an explicit click, carried into the suspended Turn."""

from __future__ import annotations

import importlib
import json
import unittest
from dataclasses import dataclass

from fastapi import HTTPException

from tsm_agt.core import AgentLoopLimitExceeded, ApprovalDecision
from tsm_agt.sdk.runtime import RuntimeTaskResult

web_app = importlib.import_module("tsm_agt.web.app")


@dataclass
class _PendingApproval:
    """Only the identity the route is allowed to act on."""

    request_id: str


def _request(request_id: str = "approval-1") -> _PendingApproval:
    return _PendingApproval(request_id)


@dataclass
class _FakeTask:
    pending_approval: _PendingApproval | None


class _FakeKernel:
    def __init__(self, task: _FakeTask | None) -> None:
        self._task = task

    async def get_task(self, task_id: str) -> _FakeTask:
        if self._task is None:
            raise LookupError(f"task not found: {task_id}")
        return self._task


class _FakeApplication:
    def __init__(self, kernel: _FakeKernel) -> None:
        self.kernel = kernel


class _FakeClient:
    def __init__(self, kernel: _FakeKernel) -> None:
        self.application = _FakeApplication(kernel)
        self.resolved: list[tuple[str, ApprovalDecision, str, str]] = []

    async def resolve_approval(
        self, request_id: str, decision: ApprovalDecision, reason: str, *,
        command_id: str,
    ) -> str:
        self.resolved.append((request_id, decision, reason, command_id))
        return "resolved"


class _FakeRuntimeServer:
    def __init__(self, task: _FakeTask | None) -> None:
        self._client = _FakeClient(_FakeKernel(task))
        self.background: list[object] = []

    def _call(self, awaitable, *, timeout: float = 70):
        return _Sync(awaitable).result()

    def submit_background(self, awaitable, *, on_error=None):
        # The test drives the coroutine itself; production schedules it on the
        # Runtime loop so the request does not hold the connection open.
        self.background.append(awaitable)
        return awaitable


class WebApprovalIngressTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous = web_app.runtime_server

    def tearDown(self) -> None:
        web_app.runtime_server = self.previous

    async def _post(self, payload: dict, task: _FakeTask | None = None):
        server = _FakeRuntimeServer(task)
        server._call = lambda awaitable, timeout=70: _Sync(awaitable).result()
        web_app.runtime_server = server
        response = await web_app.resolve_approval(payload)
        return server, response

    async def test_pending_request_is_resolved_with_the_clicked_decision(
        self,
    ) -> None:
        server, response = await self._post(
            {
                "task_id": "task-web", "request_id": "approval-1",
                "decision": "APPROVE", "command_id": "cmd-1",
            },
            _FakeTask(_request()),
        )

        self.assertEqual(response.status_code, 202)
        body = json.loads(response.body)
        self.assertTrue(body["accepted"])
        self.assertEqual(body["decision"], "APPROVE")
        # The continuation runs as Runtime work, not inside the request.
        self.assertEqual(len(server.background), 1)
        await server.background[0]
        self.assertEqual(
            server._client.resolved,
            [(
                "approval-1", ApprovalDecision.APPROVE,
                "approved from the web UI after reviewing the exact action",
                "cmd-1",
            )],
        )

    async def test_denial_is_carried_through_with_its_own_reason(self) -> None:
        server, response = await self._post(
            {
                "task_id": "task-web", "request_id": "approval-1",
                "decision": "DENY", "command_id": "cmd-2",
            },
            _FakeTask(_request()),
        )

        self.assertEqual(response.status_code, 202)
        await server.background[0]
        self.assertEqual(
            server._client.resolved[0][1], ApprovalDecision.DENY
        )
        self.assertEqual(
            server._client.resolved[0][2], "rejected from the web UI"
        )

    async def test_stale_button_cannot_resolve_a_request_that_moved_on(
        self,
    ) -> None:
        with self.assertRaises(HTTPException) as caught:
            await self._post(
                {
                    "task_id": "task-web", "request_id": "approval-old",
                    "decision": "APPROVE", "command_id": "cmd-3",
                },
                _FakeTask(_request("approval-new")),
            )
        self.assertEqual(caught.exception.status_code, 409)

    async def test_missing_pending_approval_is_a_conflict_not_a_resume(
        self,
    ) -> None:
        with self.assertRaises(HTTPException) as caught:
            await self._post(
                {
                    "task_id": "task-web", "request_id": "approval-1",
                    "decision": "APPROVE", "command_id": "cmd-4",
                },
                _FakeTask(None),
            )
        self.assertEqual(caught.exception.status_code, 409)

    async def test_unknown_decision_and_missing_fields_are_rejected(self) -> None:
        for payload in (
            {"task_id": "t", "request_id": "r", "command_id": "c",
             "decision": "MAYBE"},
            {"task_id": "t", "request_id": "r", "decision": "APPROVE"},
            {"request_id": "r", "command_id": "c", "decision": "APPROVE"},
        ):
            with self.assertRaises(HTTPException) as caught:
                await self._post(payload, _FakeTask(_request()))
            self.assertEqual(caught.exception.status_code, 400, payload)


class _Sync:
    """Run one coroutine to completion inside an already-running loop."""

    def __init__(self, awaitable) -> None:
        self._awaitable = awaitable

    def result(self):
        coroutine = self._awaitable
        try:
            coroutine.send(None)
        except StopIteration as stop:
            return stop.value
        raise AssertionError("fake kernel coroutines must not await")


class WaitingProjectionTest(unittest.TestCase):
    def test_pending_approval_becomes_a_waiting_event(self) -> None:
        result = RuntimeTaskResult(
            task_id="task-web", state="AWAITING_APPROVAL",
            phase1_state="WAITING", status="awaiting_approval", cursor=5,
            approval={"request_id": "approval-1", "action": "core.apply_patch"},
        )

        waiting = web_app._waiting_payload(result)

        self.assertEqual(waiting["kind"], "APPROVAL")
        self.assertEqual(waiting["approval"]["request_id"], "approval-1")

    def test_awaiting_user_reports_its_own_kind(self) -> None:
        result = RuntimeTaskResult(
            task_id="task-web", state="AWAITING_USER", phase1_state="WAITING",
            status="awaiting_user", cursor=6,
            clarification={"kind": "CONTINUATION", "question": None},
        )

        waiting = web_app._waiting_payload(result)

        self.assertEqual(waiting["kind"], "CONTINUATION")

    def test_running_task_reports_no_wait(self) -> None:
        result = RuntimeTaskResult(
            task_id="task-web", state="EXECUTING", phase1_state="RUNNING",
            status="running", cursor=7,
        )

        self.assertIsNone(web_app._waiting_payload(result))


class LoopLimitReportingTest(unittest.TestCase):
    def test_spent_budget_is_described_with_its_cause(self) -> None:
        detail = web_app._describe_loop_limit(AgentLoopLimitExceeded(
            "turn-web", "max_model_calls", 15, model_calls=15, tool_calls=17,
            last_tool_error="PERMISSION_DENIED", max_model_calls=15,
            max_tool_calls=40,
        ))

        self.assertIn("15/15", detail)
        self.assertIn("17/40", detail)
        self.assertIn("PERMISSION_DENIED", detail)
        # The user needs to know continuing is possible; a bare 500 said nothing.
        self.assertIn("继续", detail)


class ApprovalUiContractTest(unittest.TestCase):
    def test_ui_renders_an_explicit_approve_and_deny_control(self) -> None:
        self.assertIn("payload.waiting", web_app.INDEX_HTML)
        self.assertIn("appendApproval(", web_app.INDEX_HTML)
        self.assertIn("decideApproval(message, 'APPROVE')", web_app.INDEX_HTML)
        self.assertIn("decideApproval(message, 'DENY')", web_app.INDEX_HTML)
        self.assertIn("fetch('/approvals'", web_app.INDEX_HTML)

    def test_ui_shows_the_exact_action_before_asking_for_consent(self) -> None:
        for field in ("approval.action", "approval.target", "approval.preview"):
            self.assertIn(field, web_app.INDEX_HTML)

    def test_approval_resolution_is_bound_to_the_exact_request(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("requestId: approval.request_id, decision", html)
        self.assertIn(
            "resolution?.requestId === approvalRequest.request_id", html
        )
        self.assertIn(
            "approvalResolution.requestId === state.approval.request_id", html
        )
        # A later approval must not inherit the previous request's "已允许".
        self.assertNotIn("task.approvalResolved", html)

    def test_ordinary_text_ingress_is_not_reused_for_approval(self) -> None:
        # Approval must travel on its own route; chat text never grants consent.
        self.assertNotIn("'/session-input', { decision", web_app.INDEX_HTML)
        self.assertIn("command_id: crypto.randomUUID()", web_app.INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
