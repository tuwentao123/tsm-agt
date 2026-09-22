"""A wrongly routed ANSWER must be correctable at execution time.

Routing decides ANSWER from one glance at one message, and the three fields it
relies on are all self-reported by the model. When it misjudges, the answering
model ends up holding no tools in front of a request that plainly needs them.
Its only recourse used to be prose, which the user received as an apology and
Runtime could not act on without pattern-matching text. These tests pin the
structured alternative: the model reports the route is unusable, and the same
message is re-routed into a Task.
"""

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    InvalidModelResponse, SessionAnswerRequiresTask, SessionRouteDisposition,
    SessionTaskRelation, TaskState,
)
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, HealthState, HealthStatus, Message,
    MessageRole, ModelResponse, ModelUsage, ProviderCapabilities, TextBlock,
    ToolCall, ToolCallBlock,
)
from tsm_agt.sdk import EngineeringAgentClient

ESCALATION_TOOL = "session.requires_agent_task"


class AnswerRouteResolver:
    """Force the exact misjudgement seen in production: implement work as ANSWER."""

    descriptor = AdapterDescriptor(
        "fixture.answer-route", "1.0", "SessionInputResolverPort", "1.0",
    )

    async def start(self, context): pass
    async def health(self): return HealthStatus(HealthState.HEALTHY)
    async def stop(self, deadline): pass

    async def resolve_session_input(self, _text, _context):
        return {
            "disposition": "ANSWER",
            "relation": "INDEPENDENT",
            "source_task_id": None,
            "resolved_goal": None,
            "input_grounding": "SELF_CONTAINED",
            "confidence": 0.98,
            "reason_code": "self_contained_ui_recommendation_question",
            "clarification": None,
            "candidate_task_ids": [],
        }


class EscalatingModel(EchoModelProvider):
    """Answers normally until it is asked to answer without tools."""

    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    def __init__(self, reason: str = "needs to edit web/app.py") -> None:
        super().__init__()
        self.reason = reason
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        offered = {tool.name for tool in request.tools}
        if ESCALATION_TOOL in offered:
            return ModelResponse(
                Message(
                    "answer-escalation", MessageRole.ASSISTANT,
                    (ToolCallBlock(ToolCall(
                        "escalate", ESCALATION_TOOL, {"reason": self.reason},
                    )),),
                ), FinishReason.TOOL_CALL, ModelUsage(1, 1),
            )
        return await super().complete(request)


class ForeignToolModel(EchoModelProvider):
    """Calls a tool that was never offered to the answer route."""

    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request):
        return ModelResponse(
            Message(
                "answer-foreign-tool", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "sneak", "core.read_file", {"path": "secrets.txt"},
                )),),
            ), FinishReason.TOOL_CALL, ModelUsage(1, 1),
        )


class LengthThenStopModel(EchoModelProvider):
    """Simulates a provider splitting one answer at its token boundary."""

    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    def __init__(self) -> None:
        super().__init__()
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            text, finish = "第一部分没有结束", FinishReason.LENGTH
        else:
            text, finish = "，第二部分补全并结束。", FinishReason.STOP
        return ModelResponse(
            Message(
                f"answer-part-{len(self.requests)}", MessageRole.ASSISTANT,
                (TextBlock(text),),
            ), finish, ModelUsage(1, 1),
        )


class SessionAnswerEscalationTest(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, root: Path, model, *, resolver=None):
        app = compose_fixture_application(
            model_adapter=model, tool_adapters=(),
            session_input_resolver_adapter=resolver,
        )
        client = EngineeringAgentClient(root, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        return client

    @staticmethod
    async def seed_history(client, root: Path, session_id: str) -> None:
        """Give the Session one catalog entry.

        With an empty catalog, routing short-circuits to CREATE_TASK before the
        resolver is consulted, so the ANSWER path under test is never reached.
        """
        task = await client.application.kernel.create_task(
            "earlier work", root, session_id=session_id,
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING, TaskState.VERIFYING,
            TaskState.FINALIZING, TaskState.SUCCEEDED,
        ):
            await client.application.kernel.transition_task(
                task.task_id, state, state.value
            )

    async def test_direct_answer_continues_after_provider_length_limit(self) -> None:
        """A provider length stop must not persist an unfinished sentence."""
        with tempfile.TemporaryDirectory() as directory:
            model = LengthThenStopModel()
            client = await self.make_client(Path(directory), model)
            session = await client.application.kernel.create_session("length")

            answer = await client.application.kernel.answer_session_message(
                session.session_id, "解释一下部署验证"
            )

            self.assertEqual(answer, "第一部分没有结束，第二部分补全并结束。")
            self.assertEqual(len(model.requests), 2)
            self.assertEqual(model.requests[0].max_output_tokens, 4096)
            self.assertIn(
                "Continue exactly from its final unfinished point",
                model.requests[1].messages[-1].text,
            )

    async def test_kernel_reports_an_unusable_answer_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = await self.make_client(
                Path(directory), EscalatingModel("needs to edit web/app.py")
            )
            session = await client.application.kernel.create_session("escalate")

            with self.assertRaises(SessionAnswerRequiresTask) as caught:
                await client.application.kernel.answer_session_message(
                    session.session_id, "按这个改吧，直接实施代码",
                )

            self.assertEqual(caught.exception.reason, "needs to edit web/app.py")

    async def test_answer_route_still_rejects_an_unoffered_tool(self) -> None:
        """The escape hatch must not become a general tool-call opening."""
        with tempfile.TemporaryDirectory() as directory:
            client = await self.make_client(Path(directory), ForeignToolModel())
            session = await client.application.kernel.create_session("foreign")

            with self.assertRaises(InvalidModelResponse):
                await client.application.kernel.answer_session_message(
                    session.session_id, "read a file for me",
                )

    async def test_session_text_re_routes_into_a_contextual_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = await self.make_client(
                Path(directory), EscalatingModel(),
                resolver=AnswerRouteResolver(),
            )
            kernel = client.application.kernel
            session = await kernel.create_session("re-route")
            await self.seed_history(client, Path(directory), session.session_id)

            result = await client.submit_session_text(
                session.session_id, "按这个改吧，直接实施代码",
                command_id="request-escalate",
            )

            data = result.result
            self.assertEqual(data["kind"], "task")
            self.assertIsNotNone(data["task"])
            self.assertEqual(
                data["decision"]["disposition"],
                SessionRouteDisposition.CREATE_TASK.value,
            )
            self.assertEqual(
                data["decision"]["relation"],
                SessionTaskRelation.CONTEXTUAL.value,
            )
            self.assertEqual(
                data["decision"]["reason_code"],
                "answer_route_self_corrected_to_task",
            )
            # The user's own words must survive as the Task goal.
            task = await kernel.get_task(data["task"]["task_id"])
            self.assertEqual(task.goal, "按这个改吧，直接实施代码")

    async def test_re_route_is_auditable(self) -> None:
        """Without an event, a corrected route looks like a correct one."""
        with tempfile.TemporaryDirectory() as directory:
            client = await self.make_client(
                Path(directory), EscalatingModel("needs a command run"),
                resolver=AnswerRouteResolver(),
            )
            kernel = client.application.kernel
            session = await kernel.create_session("audit")
            await self.seed_history(client, Path(directory), session.session_id)

            await client.submit_session_text(
                session.session_id, "跑一下测试", command_id="request-audit",
            )

            events = await kernel.dependencies.store.read_session_events(
                session.session_id
            )
            correction = next(
                event for event in events
                if event.event_type == "session.route_self_corrected"
            )
            self.assertEqual(correction.payload["from_disposition"], "ANSWER")
            self.assertEqual(correction.payload["to_disposition"], "CREATE_TASK")
            self.assertEqual(correction.payload["to_relation"], "CONTEXTUAL")
            self.assertEqual(correction.payload["reason"], "needs a command run")
            self.assertEqual(correction.payload["reported_by"], ESCALATION_TOOL)
            # Routing audit never stores the user's plaintext.
            self.assertNotIn("跑一下测试", str(correction.payload))
            self.assertIn("text_hash", correction.payload)

    async def test_a_genuine_question_is_still_answered_directly(self) -> None:
        """The escape hatch must not turn every question into a Task."""
        with tempfile.TemporaryDirectory() as directory:
            client = await self.make_client(
                Path(directory), EchoModelProvider(),
                resolver=AnswerRouteResolver(),
            )
            kernel = client.application.kernel
            session = await kernel.create_session("plain question")
            await self.seed_history(client, Path(directory), session.session_id)

            result = await client.submit_session_text(
                session.session_id, "什么是 Session？", command_id="request-plain",
            )

            self.assertEqual(result.result["kind"], "answer")
            self.assertIsNone(result.result["task"])
            events = await kernel.dependencies.store.read_session_events(
                session.session_id
            )
            self.assertFalse(any(
                event.event_type == "session.route_self_corrected"
                for event in events
            ))


if __name__ == "__main__":
    unittest.main()
