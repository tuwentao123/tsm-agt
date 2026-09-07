from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.rule_based_artifact_read import RuleBasedArtifactReadPolicy
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskState
from tsm_agt.ports import (
    ArtifactReadAction, ArtifactReadPolicyPort, ArtifactReadProbe,
    ArtifactReadState, EvidenceQuestion, FinishReason, Message, MessageRole,
    ModelRequest, ModelResponse, ProviderCapabilities, RuntimeStorePort,
    TextBlock, ToolCall, ToolCallBlock, ToolResult, ToolResultBlock,
)


def read_call(
    call_id: str, *, start_line: int = 1, max_lines: int = 2,
    question_id: str = "Q-read",
) -> ToolCall:
    return ToolCall(
        call_id, "core.read_file",
        {"path": "src/target.py", "start_line": start_line,
         "max_lines": max_lines},
        EvidenceQuestion(question_id, "What does this artifact contain?"),
    )


class RuleBasedArtifactReadPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.policy = RuleBasedArtifactReadPolicy()
        await self.policy.start(None)

    async def asyncTearDown(self) -> None:
        await self.policy.stop(datetime.now())

    async def _record(self):
        call = read_call("read-1")
        probe = ArtifactReadProbe("path-hash", "", 1, 2)
        result = ToolResult(call.call_id, True, {
            "path": "src/target.py", "sha256": "content-v1",
            "start_line": 1, "end_line": 2, "total_lines": 6,
            "content": "one\ntwo\n",
        })
        return (await self.policy.after_result(
            call, result, probe, ArtifactReadState()
        )).state

    async def test_same_content_range_and_question_requires_reuse(self):
        state = await self._record()
        decision = await self.policy.before_call(
            read_call("read-2"),
            ArtifactReadProbe("path-hash", "content-v1", 1, 2), state,
        )
        self.assertEqual(decision.action, ArtifactReadAction.REQUIRE_REUSE)
        self.assertEqual(decision.reason, "range_already_read")

    async def test_changed_content_new_range_or_new_question_are_allowed(self):
        state = await self._record()
        cases = (
            (read_call("changed"),
             ArtifactReadProbe("path-hash", "content-v2", 1, 2),
             "new_or_changed_artifact"),
            (read_call("range", start_line=3),
             ArtifactReadProbe("path-hash", "content-v1", 3, 2),
             "new_range"),
            (read_call("question", question_id="Q-other"),
             ArtifactReadProbe("path-hash", "content-v1", 1, 2),
             "new_evidence_question"),
        )
        for call, probe, reason in cases:
            with self.subTest(reason=reason):
                decision = await self.policy.before_call(call, probe, state)
                self.assertEqual(decision.action, ArtifactReadAction.ALLOW)
                self.assertEqual(decision.reason, reason)


class RepeatReadModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result for message in request.messages for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        if not results:
            call = read_call("read-first")
        elif len(results) == 1:
            call = read_call("read-repeat")
        elif results[-1].error_code == "ARTIFACT_ALREADY_READ":
            call = read_call("read-new-range", start_line=3)
        else:
            return ModelResponse(Message(
                "final", MessageRole.ASSISTANT, (TextBlock("used cached read"),)
            ))
        return ModelResponse(Message(
            call.call_id, MessageRole.ASSISTANT, (ToolCallBlock(call),)
        ), FinishReason.TOOL_CALL)


class ChangeBetweenReadsModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.changed = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result for message in request.messages for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        if not results:
            call = read_call("changed-first")
        elif not self.changed:
            self.path.write_text(
                "changed\ntwo\nthree\nfour\nfive\n", encoding="utf-8"
            )
            self.changed = True
            call = read_call("changed-second")
        else:
            return ModelResponse(Message(
                "changed-final", MessageRole.ASSISTANT,
                (TextBlock("read changed artifact"),),
            ))
        return ModelResponse(Message(
            call.call_id, MessageRole.ASSISTANT, (ToolCallBlock(call),)
        ), FinishReason.TOOL_CALL)


class CountingReadTools(CoreReadOnlyToolProvider):
    def __init__(self) -> None:
        super().__init__()
        self.read_calls = 0

    async def invoke(self, call, context):
        if call.name == "core.read_file":
            self.read_calls += 1
        return await super().invoke(call, context)


class ArtifactReadKernelTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, root: Path, database: Path | None = None):
        tool = CountingReadTools()
        app = compose_fixture_application(
            model_adapter=RepeatReadModel(), tool_adapters=(tool,),
            store_adapter=SQLiteRuntimeStore(database) if database else None,
            require_evidence_questions=True,
            artifact_read_policy_adapter=RuleBasedArtifactReadPolicy(),
        )
        await app.registry.start_all()
        task = await app.kernel.create_task(
            "inspect target", root, "task-artifact-read"
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await app.kernel.transition_task(task.task_id, state, state.value)
        result = await app.kernel.run_agent_turn(task.task_id, "inspect target")
        return app, tool, task, result

    async def test_kernel_blocks_repeat_but_allows_new_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src/target.py").write_text(
                "one\ntwo\nthree\nfour\nfive\n", encoding="utf-8"
            )
            app, tool, task, result = await self._run(root)
            try:
                self.assertEqual(result.assistant_message.text, "used cached read")
                self.assertEqual(tool.read_calls, 2)
                self.assertEqual(result.tool_calls, 2)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                self.assertEqual(sum(
                    event.event_type == "artifact_read.reuse_required"
                    for event in events
                ), 1)
                payload = next(
                    event.payload for event in events
                    if event.event_type == "artifact_read.reuse_required"
                )
                self.assertNotIn("target.py", str(payload))
                self.assertNotIn("Q-read", str(payload))
            finally:
                await app.registry.stop_all()

    async def test_sqlite_restart_preserves_redacted_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            (root / "src").mkdir()
            (root / "src/target.py").write_text(
                "one\ntwo\nthree\nfour\nfive\n", encoding="utf-8"
            )
            app, _, task, _ = await self._run(root, database)
            await app.registry.stop_all()
            store = SQLiteRuntimeStore(database)
            await store.start(None)
            try:
                stored = await store.load_task(task.task_id)
                self.assertIsNotNone(stored)
                events = await store.read_events(task.task_id)
                payloads = [
                    event.payload for event in events
                    if event.event_type.startswith("artifact_read.")
                ]
                self.assertTrue(payloads)
                self.assertNotIn("target.py", str(payloads))
                self.assertNotIn("one\ntwo", str(payloads))
            finally:
                await store.stop(datetime.now())

    async def test_kernel_allows_same_range_after_file_content_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            target = root / "src/target.py"
            target.write_text(
                "one\ntwo\nthree\nfour\nfive\n", encoding="utf-8"
            )
            tool = CountingReadTools()
            app = compose_fixture_application(
                model_adapter=ChangeBetweenReadsModel(target),
                tool_adapters=(tool,), require_evidence_questions=True,
                artifact_read_policy_adapter=RuleBasedArtifactReadPolicy(),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "observe changed target", root, "task-artifact-changed"
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                result = await app.kernel.run_agent_turn(
                    task.task_id, "observe changed target"
                )
                self.assertEqual(result.assistant_message.text, "read changed artifact")
                self.assertEqual(tool.read_calls, 2)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                self.assertFalse(any(
                    event.event_type == "artifact_read.reuse_required"
                    for event in events
                ))
                reasons = [
                    event.payload["reason"] for event in events
                    if event.event_type == "artifact_read.action_evaluated"
                ]
                self.assertIn("new_or_changed_artifact", reasons)
            finally:
                await app.registry.stop_all()


class ArtifactReadCompositionTest(unittest.TestCase):
    def test_fixture_policy_is_optional_and_replaceable(self):
        without = compose_fixture_application()
        self.assertEqual(without.registry.all(ArtifactReadPolicyPort), ())
        policy = RuleBasedArtifactReadPolicy()
        with_policy = compose_fixture_application(artifact_read_policy_adapter=policy)
        self.assertIs(with_policy.registry.require(ArtifactReadPolicyPort), policy)


if __name__ == "__main__":
    unittest.main()
