from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.model_session_input import ModelSessionInputResolver
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import SessionInputAction, TaskState
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, HealthState, HealthStatus, Message,
    MessageRole, ModelResponse, ModelUsage, TextBlock,
)


class FixtureResolver:
    descriptor = AdapterDescriptor(
        "fixture.generic-session-resolver", "1.0",
        "SessionInputResolverPort", "1.0",
    )

    def __init__(self, response):
        self.response = response
        self.inputs = []

    async def start(self, context): pass
    async def health(self): return HealthStatus(HealthState.HEALTHY)
    async def stop(self, deadline): pass

    async def resolve_session_input(self, text, context):
        self.inputs.append((text, context))
        return dict(self.response)


class JsonModel(EchoModelProvider):
    def __init__(self, data):
        super().__init__()
        self.data = data
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(
            Message(
                "resolver-result", MessageRole.ASSISTANT,
                (TextBlock(json.dumps(self.data)),),
            ), FinishReason.STOP, ModelUsage(1, 1),
        )


async def interrupted_task(app, root: Path):
    session = await app.kernel.create_session("generic resolution")
    task = await app.kernel.create_task(
        "trace the payment ownership", root, session_id=session.session_id
    )
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await app.kernel.transition_task(task.task_id, state, state.value)
    # A compact valid checkpoint is easiest to obtain from a failed model call
    # in the existing CLI tests; here the candidate directory contract itself
    # is exercised by assigning the resolver after a fixture checkpoint test.
    return session, task


class SessionInputResolverContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_model_adapter_returns_only_structured_decision(self):
        model = JsonModel({
            "action": "RESUME_TASK", "task_id": "task-a",
            "confidence": 0.94, "reason_code": "historical_reference",
            "clarification": None,
        })
        resolver = ModelSessionInputResolver(model)
        from tsm_agt.ports import AdapterContext
        await model.start(AdapterContext({}, lambda *_: None))
        await resolver.start(AdapterContext({}, lambda *_: None))
        result = await resolver.resolve_session_input(
            "go back to the investigation before the last one",
            {"unfinished_tasks": [{"task_id": "task-a"}]},
        )
        self.assertEqual(result["action"], "RESUME_TASK")
        self.assertFalse(model.requests[-1].allow_tool_calls)
        self.assertEqual(model.requests[-1].tools, ())

    async def test_no_unfinished_task_bypasses_semantic_resolver(self):
        resolver = FixtureResolver({})
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                session_input_resolver_adapter=resolver,
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("empty")
                decision = await app.kernel.resolve_session_input(
                    session.session_id, "any ordinary input", Path(directory)
                )
                self.assertEqual(decision.action, SessionInputAction.NEW_TASK)
                self.assertEqual(resolver.inputs, [])
                events = await app.kernel.dependencies.store.read_session_events(
                    session.session_id
                )
                resolved = events[-1]
                self.assertEqual(resolved.event_type, "session.input_resolved")
                self.assertNotIn("any ordinary input", str(resolved.payload))
                self.assertIn("text_hash", resolved.payload)
            finally:
                await app.registry.stop_all()

    async def test_invalid_or_low_confidence_selection_requires_clarification(self):
        # Kernel validates Resolver output; the Adapter cannot invent a Task ID
        # or turn a weak guess into execution authority.
        from tsm_agt.core import (
            AgentTurnCheckpoint, SessionResumeCandidate, SessionResumeSafety,
        )
        resolver = FixtureResolver({
            "action": "RESUME_TASK", "task_id": "task-invented",
            "confidence": 0.99, "reason_code": "guess",
            "clarification": None,
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                session_input_resolver_adapter=resolver,
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("invalid selection")
                task = await app.kernel.create_task(
                    "unfinished goal", root, session_id=session.session_id
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                checkpoint = AgentTurnCheckpoint(
                    task.task_id, "turn-fixture", 1, (), (), (),
                    0, 0, 0, 0, 2, 2, 128, 1.0,
                    task.project_fingerprint,
                    task.effective_configurations[-1].effective_config_hash,
                    "unused", task.effective_configurations[-1].prompt_manifest_hash or "",
                    session.session_id, (await app.kernel.get_session(session.session_id)).context_hash,
                    (await app.kernel.get_working_memory(task.task_id)).content_hash,
                )
                stored = await app.kernel.dependencies.store.load_task(task.task_id)
                assert stored is not None
                # Use the Kernel checkpoint writer so the candidate remains a
                # normal authoritative Task snapshot.
                await app.kernel._save_agent_checkpoint(checkpoint, "fixture")
                decision = await app.kernel.resolve_session_input(
                    session.session_id, "refer to some earlier work", root
                )
                self.assertEqual(decision.action, SessionInputAction.CLARIFY)
                self.assertEqual(decision.reason_code, "semantic_resolution_failed")
            finally:
                await app.registry.stop_all()
