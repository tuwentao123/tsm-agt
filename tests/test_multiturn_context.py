"""Multi-turn context: what the model actually sees on the second and third turn.

These are **characterization tests for a defect**, not for desired behaviour.

The claim under test: when the session router explicitly classifies a message as
``CONTEXTUAL`` -- documented as "a safe, isolated Task that receives ordinary
Session context" (``core/runtime_input.py:114-116``) -- the new Task's model
request still does not contain the previous turn's user text.

Why that is structural rather than incidental:

* ``SessionSnapshot`` holds no transcript at all; it is a container of Task ids
  (``core/session.py:81-95``).
* Conversation content is re-projected every model call
  (``core/session_context.py:582``, ``kernel.py:13437-13450``).
* The projection is filtered by Task lineage membership, and the selector states
  outright that "recency is never an eligibility criterion"
  (``core/session_context.py:212-227``). A fresh Task's lineage is itself, so the
  previous Task's rows are ineligible.

A third restriction compounds this: ``SessionConversationMessage`` refuses any
role other than user/assistant (``core/session_context.py:154-155``), so tool
calls and tool results cannot survive a turn boundary at all.

Each test below is marked ``expectedFailure`` on purpose. It asserts the
**desired** behaviour, so it fails today; the moment the transcript work lands it
becomes an unexpected success, and unittest reports that as a failure telling us
to remove the marker. That is the intended signal -- do not delete these tests,
and do not weaken them into asserting the defect.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.ports import (
    AdapterDescriptor, HealthState, HealthStatus, ModelRequest, ModelResponse,
)
from tsm_agt.sdk import EngineeringAgentClient


class RecordingModelProvider(EchoModelProvider):
    """Echo model that keeps every advice request, so a test can inspect exactly
    what the model was shown -- which is the only observable that matters for a
    context-continuity claim."""

    descriptor = AdapterDescriptor(
        "fixture.recording-model", "1.0", "ModelProviderPort", "1.0",
        frozenset({"text"}),
    )

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)

    def text_since(self, mark: int) -> str:
        """Every message body the model saw in requests issued after ``mark``."""

        return "\n".join(
            message.text
            for request in self.requests[mark:]
            for message in request.messages
        )


class ContextualResolver:
    """Routes every message as CONTEXTUAL: 'receives ordinary Session context'."""

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
            "input_grounding": "SELF_CONTAINED",
            "confidence": 0.96,
            "reason_code": "fixture",
            "clarification": None,
            "candidate_task_ids": [],
        }


class MultiTurnContextTest(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, root: Path):
        model = RecordingModelProvider()
        app = compose_fixture_application(
            model_adapter=model, tool_adapters=(),
            session_input_resolver_adapter=ContextualResolver(),
        )
        client = EngineeringAgentClient(root, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        return client, model

    @unittest.expectedFailure
    async def test_second_turn_sees_the_first_turn_user_text(self):
        with tempfile.TemporaryDirectory() as directory:
            client, model = await self.make_client(Path(directory))
            session = await client.application.kernel.create_session("turn continuity")

            await client.submit_session_text(
                session.session_id, "the first instruction",
                command_id="request-1",
            )
            mark = len(model.requests)
            await client.submit_session_text(
                session.session_id, "the second instruction",
                command_id="request-2",
            )

            self.assertIn(
                "the first instruction", model.text_since(mark),
                "the second turn's model request must carry the first turn's user text",
            )

    @unittest.expectedFailure
    async def test_third_turn_sees_both_earlier_user_texts(self):
        with tempfile.TemporaryDirectory() as directory:
            client, model = await self.make_client(Path(directory))
            session = await client.application.kernel.create_session("turn continuity")

            for index in (1, 2):
                await client.submit_session_text(
                    session.session_id, f"instruction number {index}",
                    command_id=f"request-{index}",
                )
            mark = len(model.requests)
            await client.submit_session_text(
                session.session_id, "instruction number 3", command_id="request-3",
            )

            seen = model.text_since(mark)
            self.assertIn("instruction number 1", seen)
            self.assertIn("instruction number 2", seen)

    def test_conversation_messages_reject_tool_roles(self):
        """Tool rows cannot cross a turn boundary today: the conversation model
        admits user and assistant only (``core/session_context.py:154-155``)."""

        from tsm_agt.core.session_context import SessionConversationMessage
        from tsm_agt.ports import MessageRole

        with self.assertRaises(ValueError):
            SessionConversationMessage(
                message_id="tool-row", role=MessageRole.TOOL, text="{}",
                task_id="task-1", turn_id="turn-1", source_event_sequence=1,
            )


if __name__ == "__main__":
    unittest.main()
