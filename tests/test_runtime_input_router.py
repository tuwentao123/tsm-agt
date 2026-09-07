from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    RuntimeInputContext, RuntimeInputIntent, RuntimeInputRouter, TaskState,
    is_retry_last_interrupted_input, parse_retry_last_interrupted_input,
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

    async def classify_runtime_input(self, text, context):
        return {"intent": "REPLACE", "confidence": 0.93}


class RetryLastInterruptedInputTest(unittest.TestCase):
    def test_short_retry_phrases_are_recognized(self):
        for text in (
            "继续", "继续吧！", "重试呢", "再试一下", "retry",
            "重新试试", "刚才断了，再来一下", "恢复上次执行",
            "重试登录流程",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_retry_last_interrupted_input(text))

    def test_input_with_a_new_requirement_is_not_retry(self):
        for text in ("继续修改另一个页面", "重新分析另一个项目"):
            with self.subTest(text=text):
                self.assertFalse(is_retry_last_interrupted_input(text))

    def test_retry_can_carry_additional_steering(self):
        parsed = parse_retry_last_interrupted_input(
            "继续，但不要改公共组件"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.steering_text, "但不要改公共组件")


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

    def test_routes_high_confidence_phrases_and_defers_ambiguous_input(self):
        cases = {
            "另外不要修改公共 API": RuntimeInputIntent.STEER,
            "别修这个了，改成只补测试": RuntimeInputIntent.REPLACE,
            "现在做到哪了？": RuntimeInputIntent.STATUS_QUERY,
            "完成当前任务后再整理文档": (
                RuntimeInputIntent.NEW_TASK_AFTER_CURRENT
            ),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                route = self.router.route(text, self.context)
                self.assertEqual(route.intent, expected)
                self.assertFalse(route.requires_confirmation)
        ambiguous = self.router.route("测试呢？", self.context)
        self.assertEqual(ambiguous.intent, RuntimeInputIntent.AMBIGUOUS)
        self.assertTrue(ambiguous.requires_confirmation)

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
                model_adapter=EchoModelProvider(), tool_adapters=()
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
