"""Multi-turn context: what the model actually sees on the second and third turn.

**Corrected.** An earlier revision of this file asserted that a later turn could
not see an earlier turn's user text, and marked that assertion
``expectedFailure``. The assertion was wrong, and so was the diagnosis behind
it: the tests drove the session with ``submit_session_text`` alone, which
*creates* a Task but never runs it, so no model request was ever issued and the
assertion failed for that reason rather than for missing context.

Driving the turn properly -- ``submit_session_text`` then ``run_task`` -- shows
that prior turns **are** carried: each turn's model request includes a
``session-context-*`` message whose ``recent_messages`` holds the earlier user
*and* assistant rows in order. Multi-turn text continuity works.

What is genuinely absent, and is characterized below, is everything that is not
user/assistant text: tool calls and tool results never enter that projection.
That is a deliberate safety decision recorded at ``Kernel._session_task_summary`` in ``core/kernel_session.py``
("Never copy command argv/environment, patch bodies or arbitrary third-party
Tool arguments into Session context"), not an oversight.

The lesson kept in this docstring: a characterization test that fails for the
wrong reason looks exactly like one that fails for the right reason. Drive the
real path before trusting either.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ContextWindowManager, SessionContextProjector
from tsm_agt.ports import (
    AdapterDescriptor, HealthState, HealthStatus, MessageRole, ModelRequest,
    ModelResponse, ProviderCapabilities,
)
from tsm_agt.sdk import EngineeringAgentClient


class RecordingModelProvider(EchoModelProvider):
    """Echo model that keeps every advice request, so a test can inspect exactly
    what the model was shown -- which is the only observable that matters for a
    context-continuity claim.

    ``context_window`` is settable because the Session-history bound has two
    independent caps and only the second one is window-driven: a small window
    keeps the token-budget compactor in play, a large one lets the projector's own
    count bound be the only limit. Tests need to choose.
    """

    descriptor = AdapterDescriptor(
        "fixture.recording-model", "1.0", "ModelProviderPort", "1.0",
        frozenset({"text"}),
    )

    def __init__(self, context_window: int = 4096) -> None:
        super().__init__()
        self.capabilities = ProviderCapabilities(context_window=context_window)
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
    async def make_client(
        self, root: Path, *, projector=None, context_manager=None,
        context_window: int = 4096,
    ):
        model = RecordingModelProvider(context_window)
        app = compose_fixture_application(
            model_adapter=model, tool_adapters=(),
            session_input_resolver_adapter=ContextualResolver(),
            session_projector=projector,
            context_manager=context_manager,
        )
        client = EngineeringAgentClient(root, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        return client, model

    async def _say(self, client, session_id: str, text: str, index: int) -> None:
        """Submit one user message and actually run the Task it creates.

        ``submit_session_text`` alone only creates the Task; without ``run_task``
        no model request is issued and any assertion about model context is
        vacuous.
        """

        result = await client.submit_session_text(
            session_id, text, command_id=f"request-{index}",
        )
        await client.run_task(result.result["task"]["task_id"], text)

    @staticmethod
    def _session_context(requests, since: int) -> dict:
        """The ``session-context-*`` payload from the newest request."""

        for request in reversed(requests[since:]):
            for message in request.messages:
                if message.message_id.startswith("session-context-"):
                    return json.loads(message.text)
        raise AssertionError("no session-context message was projected")

    async def test_second_turn_sees_the_first_turn_user_text(self):
        with tempfile.TemporaryDirectory() as directory:
            client, model = await self.make_client(Path(directory))
            session = await client.application.kernel.create_session("continuity")

            await self._say(client, session.session_id, "the first instruction", 1)
            mark = len(model.requests)
            await self._say(client, session.session_id, "the second instruction", 2)

            self.assertIn(
                "the first instruction", model.text_since(mark),
                "the second turn must carry the first turn's user text",
            )

    async def test_third_turn_sees_both_earlier_turns_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            client, model = await self.make_client(Path(directory))
            session = await client.application.kernel.create_session("continuity")

            for index in (1, 2):
                await self._say(
                    client, session.session_id, f"instruction number {index}", index,
                )
            mark = len(model.requests)
            await self._say(client, session.session_id, "instruction number 3", 3)

            user_bodies = [
                message.get("text")
                for message in self._session_context(model.requests, mark)[
                    "recent_messages"
                ]
                if message.get("role") == "user"
            ]
            self.assertEqual(
                user_bodies,
                ["instruction number 1", "instruction number 2",
                 "instruction number 3"],
                "prior user turns must be replayed in order, ahead of the current one",
            )

    async def test_prior_assistant_turns_are_carried_too(self):
        """Continuity is not user-only: the answering model's own earlier replies
        are part of what a later turn must see."""

        with tempfile.TemporaryDirectory() as directory:
            client, model = await self.make_client(Path(directory))
            session = await client.application.kernel.create_session("continuity")

            await self._say(client, session.session_id, "the first instruction", 1)
            mark = len(model.requests)
            await self._say(client, session.session_id, "the second instruction", 2)

            roles = [
                message.get("role")
                for message in self._session_context(model.requests, mark)[
                    "recent_messages"
                ]
            ]
            self.assertIn("assistant", roles)

    async def test_tool_rows_are_not_carried_across_turns(self):
        """The real gap. Tool calls and results never enter the projection, by
        the deliberate decision at ``Kernel._session_task_summary`` in ``core/kernel_session.py``: only read-only
        navigation metadata is allowed to reach a later Turn's Session context.

        This test pins today's behaviour so that changing it is a visible,
        deliberate act rather than a side effect -- reversing it is only safe
        once the tool boundary can bound and redact results (see
        ``tsm-agt-工具边界能力对比.md``).
        """

        with tempfile.TemporaryDirectory() as directory:
            client, model = await self.make_client(Path(directory))
            session = await client.application.kernel.create_session("continuity")

            await self._say(client, session.session_id, "the first instruction", 1)
            mark = len(model.requests)
            await self._say(client, session.session_id, "the second instruction", 2)

            payload = self._session_context(model.requests, mark)
            for message in payload["recent_messages"]:
                self.assertIn(
                    message.get("role"), {"user", "assistant"},
                    "only user and assistant text may cross a turn boundary today",
                )

    async def test_the_default_bound_plateaus_and_drops_early_turns(self):
        """Pins the measured plateau: with the default 8-message bound the
        projection stops growing after four round trips, and turn 1's words leave
        the prompt entirely -- they survive only as event references in
        ``earlier_summary``.

        This is a *bound*, not a break: the same turns were verified reachable
        above. It is pinned here so that changing it is deliberate.
        """

        with tempfile.TemporaryDirectory() as directory:
            client, model = await self.make_client(Path(directory))
            session = await client.application.kernel.create_session("plateau")

            for index in range(1, 9):
                await self._say(
                    client, session.session_id, f"instruction number {index}", index,
                )

            payload = self._session_context(model.requests, 0)
            bodies = [
                message.get("text") for message in payload["recent_messages"]
            ]
            self.assertEqual(len(bodies), 8, "the default bound is eight messages")
            self.assertNotIn("instruction number 1", bodies)
            self.assertIn("instruction number 8", bodies)
            # Fifteen rows, not sixteen: the newest Turn's own reply does not
            # exist yet when its request is built.
            self.assertEqual(
                payload["earlier_summary"]["message_count"], 7,
                "seven of the fifteen existing rows left the prompt",
            )
            self.assertEqual(
                len(bodies) + payload["earlier_summary"]["message_count"], 15,
            )

    async def test_a_compacted_prompt_admits_the_messages_it_dropped(self):
        """Two layers drop history and only one of them used to say so.

        ``SessionContextProjector`` counts what it omits into
        ``earlier_summary.message_count``. ``ContextWindowManager`` then trims the
        already-projected list again -- and did not fold those drops into the same
        count, so on a small provider window the payload could report
        ``message_count: 0`` beside a prompt that had just lost eight messages.

        Raising only the projector's bound puts the two layers in disagreement on
        purpose: the projector omits nothing, the compactor drops eight.
        """

        with tempfile.TemporaryDirectory() as directory:
            client, model = await self.make_client(
                Path(directory),
                projector=SessionContextProjector(
                    recent_visible_message_limit=64,
                ),
            )
            session = await client.application.kernel.create_session("two-layers")

            for index in range(1, 9):
                await self._say(
                    client, session.session_id, f"instruction number {index}", index,
                )

            payload = self._session_context(model.requests, 0)
            self.assertEqual(len(payload["recent_messages"]), 8)
            self.assertIn("compaction", payload)
            self.assertEqual(
                payload["earlier_summary"]["message_count"], 7,
                "the payload must report the messages compaction dropped, not "
                "only the ones the projector omitted",
            )

    async def test_raising_both_bounds_keeps_early_turns_in_the_prompt(self):
        """The payoff of the newly-configurable bounds: turning them up actually
        keeps an early turn's words in a later turn's prompt, so "the bound is too
        small" is fixable by configuration rather than by re-architecting.

        Both knobs are needed, and the provider window is a third constraint.
        ``ContextWindowManager`` applies ``recent_message_floor`` on top of the
        projector's bound, so raising only the projector changes nothing on a
        small window -- the compactor trims straight back down. And keeping the
        history at all needs a window big enough to hold it. The two runs above
        are the controls.
        """

        with tempfile.TemporaryDirectory() as directory:
            client, model = await self.make_client(
                Path(directory),
                projector=SessionContextProjector(
                    recent_visible_message_limit=64,
                ),
                context_manager=ContextWindowManager(
                    recent_message_floor=64, pinned_message_ceiling=64,
                ),
                context_window=32768,
            )
            session = await client.application.kernel.create_session("raised")

            for index in range(1, 9):
                await self._say(
                    client, session.session_id, f"instruction number {index}", index,
                )

            payload = self._session_context(model.requests, 0)
            bodies = [
                message.get("text") for message in payload["recent_messages"]
            ]
            self.assertIn("instruction number 1", bodies)
            self.assertIn("instruction number 8", bodies)
            self.assertNotIn("compaction", payload)
            self.assertEqual(
                payload["earlier_summary"]["message_count"], 0,
                "nothing is omitted once every turn fits under the raised bound",
            )


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
