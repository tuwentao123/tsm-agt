from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    SessionResumeCandidate, SessionResumeSafety,
    SessionRouteDisposition, SessionTaskRelation, TaskState,
)
from tsm_agt.ports import AdapterDescriptor, HealthState, HealthStatus
from tsm_agt.sdk import EngineeringAgentClient


class FixtureResolver:
    descriptor = AdapterDescriptor(
        "fixture.session-text", "1.0", "SessionInputResolverPort", "1.0"
    )

    def __init__(self, factory):
        self.factory = factory

    async def start(self, _context):
        pass

    async def stop(self, _deadline):
        pass

    async def health(self):
        return HealthStatus(HealthState.HEALTHY)

    async def resolve_session_input(self, _text, context):
        return self.factory(context)


def route(disposition, relation, *, source_task_id=None, goal=None, clarification=None):
    return {
        "disposition": disposition,
        "relation": relation,
        "source_task_id": source_task_id,
        "resolved_goal": goal,
        "input_grounding": "SELF_CONTAINED",
        "confidence": 0.96,
        "reason_code": "fixture",
        "clarification": clarification,
        "candidate_task_ids": [],
    }


class SessionTextFacadeTest(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, root: Path, resolver=None):
        app = compose_fixture_application(
            model_adapter=EchoModelProvider(), tool_adapters=(),
            session_input_resolver_adapter=resolver,
        )
        client = EngineeringAgentClient(root, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        return client

    async def test_different_message_request_ids_create_distinct_tasks_in_one_session(self):
        with tempfile.TemporaryDirectory() as directory:
            client = await self.make_client(Path(directory))
            session = await client.application.kernel.create_session("messages")

            first = await client.submit_session_text(
                session.session_id, "first task", command_id="request-1"
            )
            second = await client.submit_session_text(
                session.session_id, "second task", command_id="request-2"
            )
            replay = await client.submit_session_text(
                session.session_id, "first task", command_id="request-1"
            )

            self.assertEqual(first.result["kind"], "task")
            self.assertTrue(replay.replayed)
            self.assertEqual(second.result["kind"], "task")
            self.assertNotEqual(
                first.result["task"]["task_id"], second.result["task"]["task_id"]
            )
            tasks = await client.application.kernel.list_session_tasks(session.session_id)
            self.assertEqual(len(tasks), 2)

    async def test_follow_up_preserves_source_and_relation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def follow_up(context):
                source = context["task_catalog"][0]["task_id"]
                return route(
                    "CREATE_TASK", "FOLLOW_UP", source_task_id=source,
                    goal="write regression tests",
                )

            client = await self.make_client(root, FixtureResolver(follow_up))
            session = await client.application.kernel.create_session("follow up")
            source = await client.application.kernel.create_task(
                "implement endpoint", root, session_id=session.session_id
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING, TaskState.VERIFYING,
                TaskState.FINALIZING, TaskState.SUCCEEDED,
            ):
                await client.application.kernel.transition_task(
                    source.task_id, state, state.value
                )

            result = await client.submit_session_text(
                session.session_id, "also cover the edge case", command_id="request-follow-up"
            )
            task_id = result.result["task"]["task_id"]
            events = await client.application.kernel.dependencies.store.read_events(task_id)
            created = next(event for event in events if event.event_type == "task.created")
            self.assertEqual(created.payload["source_task_id"], source.task_id)
            self.assertEqual(
                created.payload["task_relation"], SessionTaskRelation.FOLLOW_UP.value
            )

    async def test_ordinary_resume_proposal_derives_a_fresh_follow_up_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_id = {"value": ""}

            def resume(_context):
                return route(
                    "RESUME_TASK", "CONTINUE",
                    source_task_id=source_id["value"],
                )

            client = await self.make_client(root, FixtureResolver(resume))
            kernel = client.application.kernel
            session = await kernel.create_session("derived resume")
            source = await kernel.create_task(
                "push the verified commit", root,
                session_id=session.session_id,
            )
            source_id["value"] = source.task_id
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING, TaskState.VERIFYING,
                TaskState.FINALIZING, TaskState.SUCCEEDED,
            ):
                source = await kernel.transition_task(
                    source.task_id, state, state.value
                )
            candidate = SessionResumeCandidate(
                source.task_id, source.goal, "AWAITING_USER", str(root),
                SessionResumeSafety.EXACT_RESUME, "fixture_resume",
            )
            with patch.object(
                kernel, "list_session_resume_candidates",
                AsyncMock(return_value=(candidate,)),
            ):
                result = await client.submit_session_text(
                    session.session_id, "continue and push now",
                    command_id="request-derived-resume",
                )

            new_task_id = result.result["task"]["task_id"]
            self.assertNotEqual(new_task_id, source.task_id)
            self.assertEqual(
                result.result["decision"]["disposition"], "CREATE_TASK"
            )
            self.assertEqual(
                result.result["decision"]["relation"], "FOLLOW_UP"
            )
            created_events = await kernel.dependencies.store.read_events(
                new_task_id
            )
            created = next(
                event for event in created_events
                if event.event_type == "task.created"
            )
            self.assertEqual(created.payload["source_task_id"], source.task_id)
            self.assertEqual(created.payload["task_relation"], "FOLLOW_UP")
            self.assertIn("continue and push now", created.payload["goal"])
            self.assertIn(source.task_id, created.payload["goal"])
            original = await kernel.get_task(source.task_id)
            self.assertEqual(original.state, TaskState.SUCCEEDED)
            self.assertIsNone(original.active_agent_checkpoint)
            session_events = await kernel.dependencies.store.read_session_events(
                session.session_id
            )
            derived = next(
                event for event in session_events
                if event.event_type == "session.task_derived"
                and event.payload["task_id"] == new_task_id
            )
            self.assertIn("authority_free_session_summary", derived.payload["inherited"])
            self.assertIn("checkpoint", derived.payload["not_inherited"])
            self.assertIn("approval", derived.payload["not_inherited"])

    async def test_answer_and_clarify_do_not_create_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mode = {"value": "answer"}

            def resolve(context):
                if mode["value"] == "answer":
                    return route("ANSWER", "INDEPENDENT")
                return {
                    **route(
                        "CLARIFY", "UNCERTAIN",
                        clarification="Which completed task do you mean?",
                    ),
                    "candidate_task_ids": [
                        context["task_catalog"][0]["task_id"],
                        context["task_catalog"][1]["task_id"],
                    ],
                }

            resolver = FixtureResolver(resolve)
            client = await self.make_client(root, resolver)
            session = await client.application.kernel.create_session("direct replies")
            history = await client.application.kernel.create_task(
                "completed history", root, session_id=session.session_id
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING, TaskState.VERIFYING,
                TaskState.FINALIZING, TaskState.SUCCEEDED,
            ):
                await client.application.kernel.transition_task(history.task_id, state, state.value)
            other_history = await client.application.kernel.create_task(
                "another completed history", root, session_id=session.session_id
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING, TaskState.VERIFYING,
                TaskState.FINALIZING, TaskState.SUCCEEDED,
            ):
                await client.application.kernel.transition_task(
                    other_history.task_id, state, state.value
                )

            answer = await client.submit_session_text(
                session.session_id, "What is a session?", command_id="request-answer"
            )
            mode["value"] = "clarify"
            clarify = await client.submit_session_text(
                session.session_id, "and that one?", command_id="request-clarify"
            )

            self.assertEqual(answer.result["kind"], "answer")
            self.assertEqual(clarify.result["kind"], "clarify")
            self.assertEqual(clarify.result["clarification"], "Which completed task do you mean?")
            tasks = await client.application.kernel.list_session_tasks(session.session_id)
            self.assertEqual(
                [task.task_id for task in tasks],
                [history.task_id, other_history.task_id],
            )


if __name__ == "__main__":
    unittest.main()
