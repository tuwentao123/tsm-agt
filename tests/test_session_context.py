from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    SessionContextProjector, SessionSnapshot, SessionWorkingState, TaskState,
)
from tsm_agt.ports import (
    AdapterContext, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ModelUsage, SessionEvent, TextBlock, ToolCall, ToolCallBlock,
    RuntimeStorePort,
)


class RecordingModel(EchoModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        current = request.messages[-1].text
        return ModelResponse(
            Message(
                f"assistant-{request.turn_id}", MessageRole.ASSISTANT,
                (TextBlock(f"answered: {current}"),),
            ),
            FinishReason.STOP, ModelUsage(3, 2),
        )


class SessionContextProjectorTest(unittest.TestCase):
    @staticmethod
    def _event(sequence: int, user: str, assistant: str) -> SessionEvent:
        return SessionEvent(
            f"event-{sequence}", "session-1", sequence,
            "session.task_result_recorded",
            {
                "task_id": f"task-{sequence}",
                "turn_id": f"turn-{sequence}",
                "user_message": Message(
                    f"user-{sequence}", MessageRole.USER,
                    (TextBlock(user),),
                ).to_data(),
                "assistant_message": Message(
                    f"assistant-{sequence}", MessageRole.ASSISTANT,
                    (TextBlock(assistant),),
                ).to_data(),
            },
        )

    def test_projection_is_deterministic_and_summarizes_older_messages(self) -> None:
        snapshot = SessionSnapshot.create(
            "session-1", "uid:1", "projection",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        snapshot = snapshot.bump_context().bump_context().bump_context()
        events = tuple(
            self._event(index, f"question {index}", f"answer {index}")
            for index in range(1, 4)
        )
        projector = SessionContextProjector(recent_message_limit=2)

        first = projector.project(snapshot, events)
        second = projector.project(snapshot, tuple(reversed(events)))
        self.assertEqual(first, second)
        self.assertEqual(first.working_state.goal, "question 3")
        self.assertEqual(first.source_event_sequences, (1, 2, 3))

        prompt = projector.for_prompt(first)
        assert prompt.message is not None
        body = json.loads(prompt.message.text)
        self.assertEqual(prompt.recent_message_count, 2)
        self.assertEqual(prompt.summarized_message_count, 4)
        self.assertEqual(
            [item["text"] for item in body["recent_messages"]],
            ["question 3", "answer 3"],
        )
        self.assertEqual(body["earlier_summary"]["message_count"], 4)
        self.assertEqual(body["earlier_summary"]["revision"], first.revision)
        self.assertEqual(body["earlier_summary"]["source_event_sequences"], [1, 2])
        self.assertEqual(body["earlier_summary"]["source_event_ranges"], [[1, 2]])
        self.assertEqual(prompt.summary_source_event_sequences, (1, 2))
        self.assertEqual(prompt.summary_source_event_ranges, ((1, 2),))
        self.assertEqual(prompt.summary_hash, body["earlier_summary"]["content_hash"])
        self.assertEqual(body["content_hash"], first.content_hash)

    def test_projection_keeps_text_but_drops_tool_protocol_and_authority(self) -> None:
        snapshot = SessionSnapshot.create(
            "session-1", "uid:1", "safe projection"
        ).bump_context()
        assistant = Message(
            "assistant-tool", MessageRole.ASSISTANT,
            (
                TextBlock("visible conclusion"),
                ToolCallBlock(ToolCall("call-1", "dangerous.tool", {"token": "x"})),
            ),
        )
        event = SessionEvent(
            "event-1", "session-1", 1, "session.task_result_recorded",
            {
                "task_id": "task-1", "turn_id": "turn-1",
                "user_message": Message(
                    "user-1", MessageRole.USER, (TextBlock("request"),)
                ).to_data(),
                "assistant_message": assistant.to_data(),
                "approval": {"decision": "APPROVED"},
                "process_id": "process-1",
            },
        )
        projection = SessionContextProjector().project(snapshot, (event,))
        serialized = json.dumps(projection.to_data(), ensure_ascii=False)
        self.assertIn("visible conclusion", serialized)
        self.assertNotIn("dangerous.tool", serialized)
        self.assertNotIn("APPROVED", serialized)
        self.assertNotIn("process-1", serialized)
        self.assertNotIn("token", serialized)


class SessionContextRuntimeTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _executing_task(application, root: Path, goal: str, task_id: str, session_id: str):
        task = await application.kernel.create_task(
            goal, root, task_id=task_id, session_id=session_id
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(task_id, state, state.value)
        return task

    async def test_next_task_receives_history_and_sqlite_restart_rebuilds_equally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first_model = RecordingModel()
            first_app = compose_fixture_application(
                model_adapter=first_model,
                store_adapter=SQLiteRuntimeStore(database), tool_adapters=(),
            )
            await first_app.registry.start_all()
            try:
                session = await first_app.kernel.create_session(
                    "restart conversation", "session-restart"
                )
                first = await self._executing_task(
                    first_app, root, "first task", "task-first", session.session_id
                )
                await first_app.kernel.run_text_turn(
                    first.task_id, "Use Kotlin and never Java"
                )
                before = await first_app.kernel.get_session_conversation(session.session_id)
                before_prompt = (
                    first_app.kernel.dependencies.session_context_projector.for_prompt(
                        before
                    )
                )
                self.assertEqual(
                    [message.text for message in before.messages],
                    ["Use Kotlin and never Java", "answered: Use Kotlin and never Java"],
                )
            finally:
                await first_app.registry.stop_all()

            second_model = RecordingModel()
            second_app = compose_fixture_application(
                model_adapter=second_model,
                store_adapter=SQLiteRuntimeStore(database), tool_adapters=(),
            )
            await second_app.registry.start_all()
            try:
                after = await second_app.kernel.get_session_conversation(
                    "session-restart"
                )
                self.assertEqual(after, before)
                after_prompt = (
                    second_app.kernel.dependencies.session_context_projector.for_prompt(
                        after
                    )
                )
                self.assertEqual(after_prompt, before_prompt)
                second = await self._executing_task(
                    second_app, root, "follow-up", "task-second", "session-restart"
                )
                await second_app.kernel.run_text_turn(
                    second.task_id, "Which language did I require?"
                )
                request = second_model.requests[-1]
                context_messages = [
                    message for message in request.messages
                    if message.message_id.startswith("session-context-")
                ]
                self.assertEqual(len(context_messages), 1)
                context = json.loads(context_messages[0].text)
                self.assertEqual(
                    [item["text"] for item in context["recent_messages"]],
                    ["Use Kotlin and never Java", "answered: Use Kotlin and never Java"],
                )
                self.assertEqual(request.messages[-1].text, "Which language did I require?")
            finally:
                await second_app.registry.stop_all()

    async def test_sessions_do_not_share_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = RecordingModel()
            application = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            await application.registry.start_all()
            try:
                one = await application.kernel.create_session("one", "session-one")
                two = await application.kernel.create_session("two", "session-two")
                task_one = await self._executing_task(
                    application, root, "private", "task-one", one.session_id
                )
                await application.kernel.run_text_turn(task_one.task_id, "only session one")
                task_two = await self._executing_task(
                    application, root, "other", "task-two", two.session_id
                )
                await application.kernel.run_text_turn(task_two.task_id, "session two request")
                serialized = json.dumps(
                    [message.to_data() for message in model.requests[-1].messages],
                    ensure_ascii=False,
                )
                self.assertNotIn("only session one", serialized)
            finally:
                await application.registry.stop_all()

    async def test_explicit_working_state_is_versioned_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            application = compose_fixture_application(
                model_adapter=RecordingModel(),
                store_adapter=SQLiteRuntimeStore(database), tool_adapters=(),
            )
            await application.registry.start_all()
            try:
                session = await application.kernel.create_session(
                    "state", "session-state"
                )
                stored = await application.registry.require(
                    RuntimeStorePort
                ).load_session(session.session_id)
                assert stored is not None
                state = SessionWorkingState(
                    goal="Ship the CLI",
                    constraints=("Keep macOS working", "Do not require Git"),
                    decisions=("MCP is optional",),
                    open_questions=("Which tokenizer?",),
                    completed_work=("Session persistence",),
                    remaining_work=("Working memory",),
                )
                projection = await application.kernel.update_session_working_state(
                    session.session_id, state, expected_version=stored.version
                )
                self.assertEqual(projection.working_state, state)
                self.assertEqual(projection.source_event_sequences, (2,))
                with self.assertRaisesRegex(ValueError, "version conflict"):
                    await application.kernel.update_session_working_state(
                        session.session_id, state, expected_version=stored.version
                    )
            finally:
                await application.registry.stop_all()

            restarted = compose_fixture_application(
                model_adapter=RecordingModel(),
                store_adapter=SQLiteRuntimeStore(database), tool_adapters=(),
            )
            await restarted.registry.start_all()
            try:
                rebuilt = await restarted.kernel.get_session_conversation(
                    "session-state"
                )
                self.assertEqual(rebuilt, projection)
                prompt = restarted.kernel.dependencies.session_context_projector.for_prompt(
                    rebuilt
                )
                assert prompt.message is not None
                body = json.loads(prompt.message.text)
                self.assertEqual(body["working_state"]["goal"], "Ship the CLI")
                self.assertEqual(
                    body["working_state"]["remaining_work"], ["Working memory"]
                )
            finally:
                await restarted.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
