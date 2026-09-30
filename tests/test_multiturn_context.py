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

A second restriction compounds this: the conversation model admitted user and
assistant roles only, so tool calls and tool results could not survive a turn
boundary at all. That one is now fixed -- ``SessionConversationMessage`` accepts
``tool`` rows with their ``tool_call_id`` / ``tool_name`` pairing, and
``ConversationMessageContractTest`` pins the new contract.

Fixing the message model alone does **not** make the XFAIL tests below pass, and
that is expected: the projector only ever reads the ``user_message`` and
``assistant_message`` keys of a ``session.task_result_recorded`` payload
(``core/session_context.py:614-615``), so there is no tool data in the event
stream to project in the first place. Making these pass needs the transcript
itself, not a wider role set.

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
    AdapterDescriptor, HealthState, HealthStatus, MessageRole, ModelRequest,
    ModelResponse,
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

class ConversationMessageContractTest(unittest.TestCase):
    """Tool rows must be representable, and only when they are pairable.

    Before this contract, ``SessionConversationMessage`` admitted user and
    assistant only, so a tool call and its result could not cross a turn
    boundary at all. Opening the role without the pairing rule would be worse
    than closing it: an unpaired tool row breaks strict role alternation on the
    wire, which is the failure mode the durable message row's
    ``tool_call_id`` / ``tool_name`` columns exist to prevent.
    """

    def _message(self, **overrides):
        from tsm_agt.core.session_context import SessionConversationMessage

        fields = {
            "message_id": "row-1", "role": MessageRole.USER, "text": "hello",
            "task_id": "task-1", "turn_id": "turn-1", "source_event_sequence": 1,
        }
        fields.update(overrides)
        return SessionConversationMessage(**fields)

    def test_tool_rows_are_representable_and_pair_to_their_call(self) -> None:
        message = self._message(
            role=MessageRole.TOOL, text="{}",
            tool_call_id="call-1", tool_name="core.read_file",
        )

        data = message.source_data()
        self.assertEqual(data["role"], "tool")
        self.assertEqual(data["tool_call_id"], "call-1")
        self.assertEqual(data["tool_name"], "core.read_file")

    def test_tool_row_may_carry_empty_text(self) -> None:
        """A command that printed nothing is a legitimate result."""

        self.assertEqual(
            self._message(
                role=MessageRole.TOOL, text="", tool_call_id="call-1",
            ).text,
            "",
        )

    def test_tool_row_without_a_call_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._message(role=MessageRole.TOOL, text="{}")

    def test_non_tool_row_carrying_pairing_fields_is_rejected(self) -> None:
        for role in (MessageRole.USER, MessageRole.ASSISTANT):
            with self.subTest(role=role):
                with self.assertRaises(ValueError):
                    self._message(role=role, tool_call_id="call-1")

    def test_system_role_stays_out_of_the_conversation(self) -> None:
        """The system prompt is built once and replayed verbatim, never
        reconstructed from history (Hermes ``agent/AGENTS.md:56-62``)."""

        with self.assertRaises(ValueError):
            self._message(role=MessageRole.SYSTEM, text="you are")

    def test_absent_pairing_fields_are_omitted_from_the_hash_source(self) -> None:
        """``source_data`` feeds ``SessionConversationProjection.hash_source``;
        emitting null pairing fields would change every existing content hash."""

        self.assertEqual(
            set(self._message().source_data()),
            {"message_id", "role", "text", "task_id", "turn_id",
             "source_event_sequence"},
        )

    def test_existing_positional_construction_still_works(self) -> None:
        message = self._message()
        self.assertIsNone(message.tool_call_id)
        self.assertIsNone(message.tool_name)


class ScaffoldingMarkerTest(unittest.TestCase):
    """Runtime-authored rows must be markable as never-durable.

    Without this, persisting a turn's messages into conversation history would
    write a Runtime-authored "user" turn that later outranks the real
    instruction -- the failure Hermes' ``_EPHEMERAL_SCAFFOLDING_FLAGS``
    (``agent/session_persistence.py:36-54``) exists to prevent.
    """

    def _message(self, **overrides):
        from tsm_agt.ports import Message, TextBlock

        fields = {
            "message_id": "msg-1", "role": MessageRole.USER,
            "content": (TextBlock("correction"),),
        }
        fields.update(overrides)
        return Message(**fields)

    def test_every_kind_used_by_the_runtime_is_declared(self) -> None:
        from tsm_agt.ports import SCAFFOLDING_KINDS

        self.assertEqual(
            SCAFFOLDING_KINDS,
            {"completion-readiness", "continuation-input", "steering"},
        )

    def test_ordinary_messages_are_not_scaffolding(self) -> None:
        self.assertFalse(self._message().is_scaffolding)

    def test_a_marked_message_reports_its_kind(self) -> None:
        message = self._message(scaffolding="completion-readiness")
        self.assertTrue(message.is_scaffolding)
        self.assertEqual(message.scaffolding, "completion-readiness")

    def test_unknown_kind_fails_closed(self) -> None:
        """A typo must not silently mark nothing and let the row persist."""

        with self.assertRaises(ValueError):
            self._message(scaffolding="completion_readiness")

    def test_only_user_rows_may_be_scaffolding(self) -> None:
        for role in (MessageRole.ASSISTANT, MessageRole.TOOL, MessageRole.SYSTEM):
            with self.subTest(role=role):
                with self.assertRaises(ValueError):
                    self._message(role=role, scaffolding="steering")

    def test_absent_marker_is_omitted_from_the_payload(self) -> None:
        """``to_data`` is the checkpoint wire; emitting the key unconditionally
        would change every existing payload's bytes."""

        self.assertNotIn("scaffolding", self._message().to_data())

    def test_marker_survives_a_checkpoint_round_trip(self) -> None:
        from tsm_agt.ports import Message

        original = self._message(scaffolding="steering")
        self.assertEqual(Message.from_data(original.to_data()), original)

    def test_round_trip_of_an_unmarked_message_stays_unmarked(self) -> None:
        from tsm_agt.ports import Message

        original = self._message()
        restored = Message.from_data(original.to_data())
        self.assertIsNone(restored.scaffolding)
        self.assertEqual(restored, original)


if __name__ == "__main__":
    unittest.main()
