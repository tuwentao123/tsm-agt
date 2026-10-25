"""A reply must be readable by the Planner, without becoming authoritative.

Real failure (``task-12d775944ea04890af9436fe1787640f``): the user answered the
assistant's own offer -- "如果你愿意...我可以...写文档" -- with the typo
"原因 直接写文档吧" (愿意). INV-8 scopes the Planner to this Task's lineage with
no background, so it saw exactly one message, could not know that "愿意" was the
candidate word, and wrote the literal reading into the contract: scope
"请直接写一份关于'原因'的文档" plus an acceptance criterion requiring the
document to be about 原因. The Agent obeyed and produced an essay about causes.

The fix keeps INV-8's boundary: the immediately preceding exchange is pulled in
by explicit message id -- ContextScope's own sanctioned "single outside fact"
escape hatch -- and delivered under its own NON_AUTHORITATIVE section. It may
resolve what the request refers to; it may never author the goal.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskState
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, HealthState, HealthStatus, Message,
    MessageRole, ModelResponse, ModelUsage, ProviderCapabilities, TextBlock,
)
from tsm_agt.sdk import EngineeringAgentClient


OFFER = "如果你愿意下一步落地，我可以把那份端到端讲解收敛成一份 Markdown。"


class CannedAnswerModel(EchoModelProvider):
    """Every Agent turn answers with the same text, so the session records it."""

    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request):
        return ModelResponse(
            Message(
                f"msg-{request.turn_id}", MessageRole.ASSISTANT,
                (TextBlock(OFFER),),
            ),
            FinishReason.STOP, ModelUsage(1, 1),
        )


class ContextualResolver:
    descriptor = AdapterDescriptor(
        "fixture.contextual-resolver", "1.0", "SessionInputResolverPort", "1.0"
    )

    async def start(self, _context) -> None:
        pass

    async def stop(self, _deadline) -> None:
        pass

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY)

    async def resolve_session_input(self, _text, _context):
        return {
            "disposition": "CREATE_TASK",
            "relation": "CONTEXTUAL",
            "source_task_id": None,
            "input_grounding": "CONTEXT_DEPENDENT",
            "confidence": 0.96,
            "reason_code": "fixture",
            "clarification": None,
            "candidate_task_ids": [],
        }


class PlannerInterpretationContextTest(unittest.IsolatedAsyncioTestCase):
    async def _client(self, root: Path):
        app = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(root / ".agent" / "runtime.db"),
            model_adapter=CannedAnswerModel(), tool_adapters=(),
            session_input_resolver_adapter=ContextualResolver(),
        )
        client = EngineeringAgentClient(root, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        return client

    async def _submit(self, client, session_id: str, text: str, index: int) -> str:
        result = await client.submit_session_text(
            session_id, text, command_id=f"request-{index}",
        )
        return result.result["task"]["task_id"]

    async def _say(self, client, session_id: str, text: str, index: int) -> str:
        """Submit, then actually run, so the exchange lands in the Session."""

        task_id = await self._submit(client, session_id, text, index)
        await client.run_task(task_id, text)
        return task_id

    async def _planning_context(self, client, task_id: str):
        """Read the context where production reads it: before the Task runs.

        Planning happens at creation. Reading it after the Task ran would also
        see this Task's own assistant reply, which is its own lineage rather
        than the preceding exchange under test.
        """

        task = await client.application.kernel.get_task(task_id)
        _request, context, _revision, _hash = (
            await client.application.kernel._task_spec_planning_context(task)
        )
        return context

    async def test_the_preceding_exchange_reaches_the_planner_non_authoritatively(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = await self._client(root)
            session = await client.application.kernel.create_session("interp")

            await self._say(client, session.session_id, "把 Codex 系统整理一下", 1)
            replying = await self._submit(
                client, session.session_id, "原因 直接写文档吧", 2,
            )

            session_plan = (
                await self._planning_context(client, replying)
            )["session"]

            # Authoritative: this Task's own request, and nothing else.
            self.assertEqual(
                [item["text"] for item in session_plan["scoped_messages"]
                 if item["role"] == "user"][-1:],
                ["原因 直接写文档吧"],
            )
            # Interpretation-only: the exchange the request is answering.
            texts = [
                item["text"] for item in session_plan["interpretation_context"]
            ] + [item["text"] for item in session_plan["scoped_messages"]
                 if item["role"] == "assistant"]
            self.assertTrue(
                any(OFFER in text for text in texts),
                "the Planner must see the offer the user replied to",
            )
            self.assertTrue(all(
                item["authority"] == "NON_AUTHORITATIVE"
                for item in session_plan["interpretation_context"]
            ))

    async def test_the_pulled_in_exchange_is_bounded_not_a_whole_earlier_task(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = await self._client(root)
            session = await client.application.kernel.create_session("bounded")

            await self._say(client, session.session_id, "很久以前的旧目标", 1)
            await self._say(client, session.session_id, "再之前的一轮", 2)
            replying = await self._submit(
                client, session.session_id, "原因 直接写文档吧", 3,
            )

            context = await self._planning_context(client, replying)
            interpretation = context["session"]["interpretation_context"]

            self.assertLessEqual(len(interpretation), 2)
            self.assertNotIn(
                "很久以前的旧目标",
                "\n".join(item["text"] for item in interpretation),
                "the boundary stays a single exchange, never an older Task",
            )
            # INV-8: the lineage is not widened, only an explicit reference added.
            self.assertEqual(context["authority"]["scoped_messages"],
                             "AUTHORITATIVE")
            self.assertEqual(
                context["authority"]["interpretation_context"],
                "NON_AUTHORITATIVE",
            )

    async def test_a_self_contained_first_request_has_no_interpretation_context(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = await self._client(root)
            session = await client.application.kernel.create_session("first")

            first = await self._submit(
                client, session.session_id, "整理一份教学文档", 1,
            )

            session_plan = (
                await self._planning_context(client, first)
            )["session"]
            self.assertEqual(session_plan["interpretation_context"], [])
            self.assertEqual(
                [item["text"] for item in session_plan["scoped_messages"]],
                ["整理一份教学文档"],
            )


if __name__ == "__main__":
    unittest.main()
