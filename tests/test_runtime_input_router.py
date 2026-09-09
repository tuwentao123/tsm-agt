from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    RuntimeInputContext, RuntimeInputIntent, RuntimeInputRouter, TaskState,
)
from tsm_agt.ports import AdapterDescriptor, HealthState, HealthStatus


class FixtureClassifier:
    descriptor = AdapterDescriptor(
        "fixture.runtime-input-classifier", "1.0",
        "RuntimeInputClassifierPort", "1.0",
    )

    async def start(self, context):
        pass

    async def health(self):
        return HealthStatus(HealthState.HEALTHY)

    async def stop(self, deadline):
        pass

    def __init__(self, intent="REPLACE", confidence=0.93):
        self.intent = intent
        self.confidence = confidence
        self.contexts = []

    async def classify_runtime_input(self, text, context):
        self.contexts.append(dict(context))
        return {"intent": self.intent, "confidence": self.confidence}


async def executing_task(application, root: Path):
    task = await application.kernel.create_task("original goal", root)
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await application.kernel.transition_task(
            task.task_id, state, state.value
        )
    return task


class RuntimeInputRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.router = RuntimeInputRouter()
        self.context = RuntimeInputContext("EXECUTING")

    def test_ordinary_language_is_never_special_cased_in_core(self):
        for text in (
            "另外不要修改公共 API",
            "别修这个了，改成只补测试",
            "现在做到哪了？",
            "完成当前任务后再整理文档",
            "return to the earlier investigation",
        ):
            with self.subTest(text=text):
                route = self.router.route(text, self.context)
                self.assertEqual(route.intent, RuntimeInputIntent.AMBIGUOUS)
                self.assertTrue(route.requires_confirmation)

    def test_explicit_ui_choice_is_deterministic(self):
        route = self.router.route(
            "arbitrary text", self.context,
            explicit_intent=RuntimeInputIntent.REPLACE,
        )
        self.assertEqual(route.intent, RuntimeInputIntent.REPLACE)
        self.assertFalse(route.requires_confirmation)

    def test_pending_protocols_take_precedence(self):
        answer = self.router.route(
            "选第二个", RuntimeInputContext(
                "AWAITING_USER", awaiting_clarification=True
            )
        )
        self.assertEqual(answer.intent, RuntimeInputIntent.CLARIFICATION_ANSWER)
        approval = self.router.route(
            "可以", RuntimeInputContext(
                "AWAITING_APPROVAL", awaiting_approval=True
            )
        )
        self.assertEqual(approval.intent, RuntimeInputIntent.AMBIGUOUS)
        self.assertTrue(approval.requires_confirmation)


class RuntimeInputKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_optional_classifier_can_replace_ambiguous_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                runtime_input_classifier_adapter=FixtureClassifier(),
            )
            await app.registry.start_all()
            try:
                task = await executing_task(app, Path(directory))
                route = await app.kernel.route_runtime_input(
                    task.task_id, "这个方向不太对", "classified-input"
                )
                self.assertEqual(route.intent, RuntimeInputIntent.REPLACE)
                self.assertTrue(route.applied)
                self.assertTrue(route.router_version.startswith("classifier:"))
            finally:
                await app.registry.stop_all()

    async def test_route_event_is_redacted_and_steering_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                runtime_input_classifier_adapter=FixtureClassifier("STEER"),
            )
            await app.registry.start_all()
            try:
                task = await executing_task(app, root)
                secret_text = "另外不要修改公共 API secret-marker"
                route = await app.kernel.route_runtime_input(
                    task.task_id, secret_text, "input-1"
                )
                self.assertEqual(route.intent, RuntimeInputIntent.STEER)
                self.assertTrue(route.applied)
                events = await app.kernel.dependencies.store.read_events(
                    task.task_id
                )
                routed = next(
                    event for event in events
                    if event.event_type == "runtime_input.routed"
                )
                self.assertNotIn("text", routed.payload)
                self.assertNotIn("secret-marker", str(routed.payload))
                steering = await app.kernel.get_steering(task.task_id)
                self.assertEqual(steering.pending[0].text, secret_text)
                replay = await app.kernel.route_runtime_input(
                    task.task_id, secret_text, "input-1"
                )
                self.assertEqual(replay, route)
                self.assertEqual(len((await app.kernel.get_steering(
                    task.task_id
                )).pending), 1)
            finally:
                await app.registry.stop_all()

    async def test_classifier_receives_current_goal_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            classifier = FixtureClassifier("STATUS_QUERY")
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                runtime_input_classifier_adapter=classifier,
            )
            await app.registry.start_all()
            try:
                task = await executing_task(app, Path(directory))
                route = await app.kernel.route_runtime_input(
                    task.task_id, "how is it going", "status-input"
                )
                self.assertEqual(route.intent, RuntimeInputIntent.STATUS_QUERY)
                self.assertEqual(classifier.contexts[0]["current_goal"], "original goal")
                self.assertEqual(classifier.contexts[0]["task_state"], "EXECUTING")
            finally:
                await app.registry.stop_all()

    async def test_ambiguous_input_is_recorded_but_not_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=()
            )
            await app.registry.start_all()
            try:
                task = await executing_task(app, Path(directory))
                route = await app.kernel.route_runtime_input(
                    task.task_id, "这个方向不太对", "input-ambiguous"
                )
                self.assertTrue(route.requires_confirmation)
                self.assertFalse(route.applied)
                self.assertFalse((await app.kernel.get_steering(task.task_id)).pending)
            finally:
                await app.registry.stop_all()
