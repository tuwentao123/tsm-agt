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
    ToolCommitState, ToolExecutionRecord, WorkingMemorySnapshot,
)
from tsm_agt.ports import (
    AdapterContext, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ModelUsage, SessionEvent, TextBlock, ToolCall, ToolCallBlock,
    RuntimeStorePort, ToolIdempotency, ToolResult, ToolRisk,
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
                "task_summary": {
                    "task_id": f"task-{sequence}",
                    "turn_id": f"turn-{sequence}",
                    "goal": user,
                    "recorded_task_state": "EXECUTING",
                    "tool_counts": {"core.search_text": sequence},
                    "important_actions": [],
                    "confirmed": [],
                    "completed_work": [f"completed {sequence}"],
                    "remaining_work": [f"remaining {sequence}"],
                    "workspace_roots": [f"/workspace-{sequence}"],
                    "mutations": [],
                    "verification_status": None,
                },
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
        self.assertEqual(
            body["earlier_summary"]["algorithm"],
            "deterministic-task-handoff-v1",
        )
        self.assertEqual(body["earlier_summary"]["revision"], first.revision)
        self.assertEqual(body["earlier_summary"]["source_event_sequences"], [1, 2])
        self.assertEqual(body["earlier_summary"]["source_event_ranges"], [[1, 2]])
        self.assertEqual(prompt.summary_source_event_sequences, (1, 2))
        self.assertEqual(prompt.summary_source_event_ranges, ((1, 2),))
        self.assertEqual(prompt.summary_hash, body["earlier_summary"]["content_hash"])
        self.assertEqual(body["content_hash"], first.content_hash)
        self.assertEqual(
            [item["task_id"] for item in body["earlier_summary"]["tasks"]],
            ["task-1", "task-2"],
        )
        self.assertEqual(
            body["earlier_summary"]["tasks"][0]["completed_work"],
            ["completed 1"],
        )
        self.assertEqual(
            [item["task_id"] for item in body["recent_task_summaries"]],
            ["task-3"],
        )
        self.assertEqual(
            body["recent_task_summaries"][0]["tool_counts"],
            {"core.search_text": 3},
        )

    def test_recent_task_summaries_follow_recent_messages_and_are_bounded(self) -> None:
        snapshot = SessionSnapshot.create(
            "session-1", "uid:1", "recent summaries",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        for _ in range(7):
            snapshot = snapshot.bump_context()
        events = tuple(
            self._event(index, f"question {index}", f"answer {index}")
            for index in range(1, 8)
        )
        projector = SessionContextProjector(
            recent_message_limit=12, recent_task_summary_limit=5
        )
        body = json.loads(projector.for_prompt(
            projector.project(snapshot, events)
        ).message.text)

        # Twelve messages cover Tasks 2..7, but the explicit Task-summary cap
        # keeps only the five most recent distinct Tasks.
        self.assertEqual(
            [item["task_id"] for item in body["recent_task_summaries"]],
            ["task-3", "task-4", "task-5", "task-6", "task-7"],
        )
        self.assertNotIn(
            "task-2",
            json.dumps(body["recent_task_summaries"], ensure_ascii=False),
        )

    def test_earlier_summary_groups_one_task_and_does_not_repeat_recent_task(self) -> None:
        snapshot = SessionSnapshot.create(
            "session-1", "uid:1", "grouped history",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        ).bump_context().bump_context().bump_context()
        first = self._event(1, "inspect service", "found entry")
        # A later visible Turn in the same Task replaces its durable summary but
        # both message pairs still belong to one historical handoff.
        second = SessionEvent(
            "event-2", "session-1", 2, "session.task_result_recorded", {
                **first.payload,
                "task_id": "task-1", "turn_id": "turn-2",
                "user_message": Message(
                    "user-2", MessageRole.USER, (TextBlock("continue service"),)
                ).to_data(),
                "assistant_message": Message(
                    "assistant-2", MessageRole.ASSISTANT,
                    (TextBlock("found caller"),),
                ).to_data(),
                "task_summary": {
                    **first.payload["task_summary"],
                    "task_id": "task-1", "turn_id": "turn-2",
                    "completed_work": ["found entry", "found caller"],
                    "remaining_work": ["verify route"],
                    "mutations": [{
                        "mutation_id": "m1", "path": "src/service.py",
                        "operation": "modify", "after_hash": "hash",
                    }],
                    "verification_status": "passed",
                },
            },
        )
        recent = self._event(3, "new topic", "new answer")
        projector = SessionContextProjector(recent_message_limit=2)
        body = json.loads(projector.for_prompt(
            projector.project(snapshot, (first, second, recent))
        ).message.text)
        tasks = body["earlier_summary"]["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_id"], "task-1")
        self.assertEqual(tasks[0]["visible_result"], "found caller")
        self.assertEqual(tasks[0]["completed_work"], ["found entry", "found caller"])
        self.assertEqual(tasks[0]["remaining_work"], ["verify route"])
        self.assertEqual(tasks[0]["verification_status"], "passed")
        self.assertEqual(tasks[0]["mutations"][0]["path"], "src/service.py")
        self.assertEqual(
            [item["task_id"] for item in body["recent_task_summaries"]],
            ["task-3"],
        )

    def test_earlier_summary_is_bounded_by_task_count_and_characters(self) -> None:
        snapshot = SessionSnapshot.create(
            "session-1", "uid:1", "bounded history",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        for _ in range(5):
            snapshot = snapshot.bump_context()
        events = tuple(
            self._event(index, "q" * 300, "a" * 300)
            for index in range(1, 6)
        )
        projector = SessionContextProjector(
            recent_message_limit=2, earlier_task_summary_limit=2,
            max_summary_characters=1200, summary_item_characters=100,
        )
        body = json.loads(projector.for_prompt(
            projector.project(snapshot, events)
        ).message.text)
        earlier = body["earlier_summary"]
        self.assertLessEqual(len(earlier["tasks"]), 2)
        self.assertGreaterEqual(earlier["omitted_task_count"], 2)
        self.assertNotIn("items", earlier)

    @staticmethod
    def _execution(
        *, tool: str, arguments: dict[str, object], data: object,
        call_id: str = "call-1",
    ) -> ToolExecutionRecord:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = ToolExecutionRecord.start(
            task_id="task-1", turn_id="turn-1",
            invocation_id="invocation-1",
            call=ToolCall(call_id, tool, arguments), payload_hash="payload",
            policy_decision_id="policy", effective_risk=ToolRisk.R0,
            approval_request_id=None, idempotency=ToolIdempotency.IDEMPOTENT,
        )
        # Preserve a fixed ordering timestamp so the projection is deterministic.
        object.__setattr__(record, "started_at", now)
        object.__setattr__(record, "updated_at", now)
        record = record.mark_running().finish(
            ToolCommitState.COMMITTED, ToolResult(call_id, True, data=data)
        )
        object.__setattr__(record, "started_at", now)
        object.__setattr__(record, "updated_at", now)
        return record

    def test_recent_execution_keeps_atomic_small_read_observation(self) -> None:
        projector = SessionContextProjector()
        execution = self._execution(
            tool="core.read_file", arguments={
                "path": "src/app.py", "start_line": 1, "max_lines": 20,
            }, data={
                "requested_path": "src/app.py", "path": "src/app.py",
                "resolved_path": "/workspace/src/app.py",
                "resolved_root": "/workspace",
                "root_kind": "PRIMARY_WORKSPACE", "sha256": "abc",
                "start_line": 1, "end_line": 2, "total_lines": 2,
                "content": "class App:\n    pass\n",
            },
        )
        projected = projector.project_recent_executions({
            "task-1": (execution,)
        })
        event = projected["task-1"][0]
        self.assertEqual(event["call"]["tool"], "core.read_file")
        self.assertEqual(event["result"]["state"], "COMMITTED")
        observation = event["result"]["observation"]
        self.assertTrue(observation["content_included"])
        self.assertEqual(observation["content"], "class App:\n    pass\n")

    def test_recent_execution_summarizes_large_read_and_sensitive_tool(self) -> None:
        projector = SessionContextProjector(small_read_content_characters=20)
        large = self._execution(
            tool="core.read_file", arguments={"path": "large.py"},
            data={
                "path": "large.py", "resolved_path": "/w/large.py",
                "resolved_root": "/w", "root_kind": "PRIMARY_WORKSPACE",
                "sha256": "large-hash", "content": "secret-source" * 10,
            }, call_id="large",
        )
        command = self._execution(
            tool="core.run_command", arguments={
                "argv": ["tool", "--token", "secret-value"],
                "environment": {"API_KEY": "secret-value"},
            }, data={"stdout": {"text": "secret-output"}},
            call_id="command",
        )
        projected = projector.project_recent_executions({
            "task-1": (large, command)
        })["task-1"]
        encoded = json.dumps(projected, ensure_ascii=False)
        large_event = next(
            item for item in projected if item["call"]["tool"] == "core.read_file"
        )
        large_observation = large_event["result"]["observation"]
        self.assertFalse(large_observation["content_included"])
        self.assertNotIn("content", large_observation)
        self.assertIn("core.run_command", encoded)
        self.assertNotIn("secret-value", encoded)
        self.assertNotIn("secret-output", encoded)
        self.assertNotIn("--token", encoded)

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

    def test_legacy_event_builds_minimal_recent_task_summary(self) -> None:
        snapshot = SessionSnapshot.create(
            "session-1", "uid:1", "legacy"
        ).bump_context()
        event = SessionEvent(
            "event-1", "session-1", 1, "session.task_result_recorded", {
                "task_id": "task-old", "turn_id": "turn-old",
                "task_state": "EXECUTING",
                "user_message": Message(
                    "user-old", MessageRole.USER, (TextBlock("continue old work"),)
                ).to_data(),
                "assistant_message": Message(
                    "assistant-old", MessageRole.ASSISTANT,
                    (TextBlock("old result"),),
                ).to_data(),
                "working_state": {
                    "goal": "inspect legacy project",
                    "remaining_work": ["read the caller"],
                },
            },
        )
        body = json.loads(SessionContextProjector().for_prompt(
            SessionContextProjector().project(snapshot, (event,))
        ).message.text)
        self.assertEqual(len(body["recent_task_summaries"]), 1)
        summary = body["recent_task_summaries"][0]
        self.assertEqual(summary["task_id"], "task-old")
        self.assertEqual(summary["remaining_work"], ["read the caller"])
        self.assertEqual(summary["tool_counts"], {})

    def test_terminal_state_event_updates_existing_task_summary(self) -> None:
        snapshot = SessionSnapshot.create(
            "session-1", "uid:1", "terminal state"
        ).bump_context().bump_context()
        result = self._event(1, "inspect", "done")
        terminal = SessionEvent(
            "event-2", "session-1", 2, "session.task_state_updated",
            {"task_id": "task-1", "task_state": "SUCCEEDED"},
        )
        projection = SessionContextProjector().project(
            snapshot, (result, terminal)
        )
        self.assertEqual(
            projection.task_summaries[0].recorded_task_state, "SUCCEEDED"
        )
        body = json.loads(SessionContextProjector().for_prompt(projection).message.text)
        self.assertEqual(
            body["recent_task_summaries"][0]["recorded_task_state"],
            "SUCCEEDED",
        )

    def test_reference_catalogs_are_bounded_and_same_question_ids_do_not_collide(
        self,
    ) -> None:
        snapshot = SessionSnapshot.create(
            "session-1", "uid:1", "bounded catalogs"
        ).bump_context().bump_context()

        def event(sequence: int, task_id: str, offset: int) -> SessionEvent:
            resources = [{
                "catalog_ref": f"catalog-{offset + index}",
                "canonical_path": f"/project-{task_id}/file-{index}",
                "resolved_root": f"/project-{task_id}",
                "root_kind": "PRIMARY_WORKSPACE",
                "resource_kind": "ARTIFACT",
                "source_task_id": task_id,
                "source_turn_id": f"turn-{sequence}",
                "source_tool": "core.read_file",
                "question_ref": f"question-{task_id}-{index}",
                "question_status": "RESOLVED",
                "evidence_references": [],
                "authority_inherited": False,
            } for index in range(105)]
            questions = [{
                # Both Tasks may call their local question Q1; the persisted
                # Session reference includes Task identity and remains unique.
                "question_ref": f"question-{task_id}-{index}",
                "question": "Q1" if index == 54 else f"Q{index}",
                "status": "RESOLVED",
                "source_task_id": task_id,
                "source_turn_id": f"turn-{sequence}",
                "catalog_refs": [f"catalog-{offset + index}"],
                "evidence_references": [],
                "blocking_reason": None,
                "authority_inherited": False,
            } for index in range(55)]
            return SessionEvent(
                f"event-{sequence}", "session-1", sequence,
                "session.task_result_recorded",
                {
                    "task_id": task_id, "turn_id": f"turn-{sequence}",
                    "resource_catalog": resources,
                    "question_catalog": questions,
                },
            )

        projection = SessionContextProjector().project(
            snapshot, (event(1, "task-a", 0), event(2, "task-b", 105))
        )
        self.assertEqual(len(projection.resource_catalog), 200)
        self.assertEqual(len(projection.question_catalog), 100)
        q1_records = [
            item for item in projection.question_catalog if item.question == "Q1"
        ]
        self.assertEqual(
            {item.source_task_id for item in q1_records}, {"task-a", "task-b"}
        )


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

    async def test_task_summary_does_not_copy_command_or_patch_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = compose_fixture_application(
                model_adapter=RecordingModel(), tool_adapters=()
            )
            await application.registry.start_all()
            try:
                session = await application.kernel.create_session(
                    "safe summary", "session-safe-summary"
                )
                task = await self._executing_task(
                    application, root, "safe summary", "task-safe-summary",
                    session.session_id,
                )
                # The summary helper consumes the same persisted record shape.
                # A command-like Tool is intentionally represented by a minimal
                # fake record because its arguments must never be copied.
                from types import SimpleNamespace
                from tsm_agt.core import WorkingMemorySnapshot

                unsafe_execution = SimpleNamespace(
                    turn_id="turn-safe", updated_at=datetime.now(timezone.utc),
                    execution_id="execution-safe",
                    call=ToolCall("call-safe", "core.run_command", {
                        "argv": ["tool", "--token", "secret-value"],
                        "cwd": str(root),
                    }),
                    state=SimpleNamespace(value="COMMITTED"),
                    result=SimpleNamespace(ok=True, error_code=None),
                )
                # Call the pure projection with a record-compatible Task view.
                task_view = SimpleNamespace(
                    task_id=task.task_id, goal=task.goal, state=task.state,
                    tool_executions={"execution-safe": unsafe_execution},
                    mutation_journal=(),
                )
                summary = application.kernel._session_task_summary(
                    task_view, "turn-safe",
                    WorkingMemorySnapshot.initial(task.task_id, task.goal), (),
                )
                encoded = json.dumps(summary, ensure_ascii=False)
                self.assertIn("core.run_command", encoded)
                self.assertNotIn("secret-value", encoded)
                self.assertNotIn("--token", encoded)
                self.assertNotIn(str(root), encoded)
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
                self.assertEqual(body["work_state"]["goal"], "Ship the CLI")
                self.assertEqual(
                    body["work_state"]["remaining_work"], ["Working memory"]
                )
            finally:
                await restarted.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
