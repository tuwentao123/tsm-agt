from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.structured_evidence import StructuredEvidenceDeltaEvaluator
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import AgentTurnResult, TaskState
from tsm_agt.ports import (
    AdapterContext, EvidenceQuestion, EvidenceInventory, FinishReason, Message,
    MessageRole, ModelRequest, ModelResponse, ProviderCapabilities,
    RuntimeStorePort, TextBlock, ToolCall, ToolCallBlock, ToolResult,
    ToolResultBlock,
)


class StructuredEvidenceDeltaEvaluatorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.evaluator = StructuredEvidenceDeltaEvaluator()
        await self.evaluator.start(
            AdapterContext(config={}, emit_event=lambda *_: None)
        )

    async def test_search_results_add_paths_and_relations_then_deduplicate(self):
        call = ToolCall(
            "search-1", "core.search_text",
            {"query": "Kernel", "path": "src"},
            EvidenceQuestion("E1", "Where is Kernel referenced?"),
        )
        result = ToolResult(call.call_id, True, data={
            "path": "src",
            "matches": [
                {"path": "src/a.py", "line": 3, "text": "Kernel()"},
                {"path": "src/b.py", "line": 8, "text": "Kernel"},
            ],
            "scanned_files": 2,
        })

        first = await self.evaluator.evaluate(call, result, EvidenceInventory())
        second = await self.evaluator.evaluate(call, result, first.inventory)

        self.assertEqual(first.delta.counts["new_paths"], 3)
        self.assertEqual(first.delta.counts["new_relations"], 2)
        self.assertTrue(first.delta.has_progress)
        self.assertEqual(second.delta.total_new, 0)
        self.assertEqual(second.delta.consecutive_zero_delta, 1)

    async def test_empty_result_is_one_exclusion_then_zero_delta(self):
        call = ToolCall(
            "search-empty", "core.search_text",
            {"query": "MissingThing", "path": "src"},
            EvidenceQuestion("E2", "Does MissingThing occur in src?"),
        )
        result = ToolResult(call.call_id, True, data={
            "path": "src", "matches": [], "scanned_files": 4,
        })

        first = await self.evaluator.evaluate(call, result, EvidenceInventory())
        second = await self.evaluator.evaluate(call, result, first.inventory)

        self.assertEqual(first.delta.counts["new_exclusions"], 1)
        self.assertEqual(second.delta.total_new, 0)

    async def test_file_content_and_process_output_are_not_persisted(self):
        call = ToolCall(
            "read-1", "core.read_file", {"path": "secret.txt"},
            EvidenceQuestion("E3", "What does the artifact establish?"),
        )
        result = ToolResult(call.call_id, True, data={
            "path": "secret.txt", "sha256": "a" * 64,
            "start_line": 1, "end_line": 2,
            "content": "do-not-persist-this-body",
        })

        evaluation = await self.evaluator.evaluate(
            call, result, EvidenceInventory()
        )
        rendered = str(evaluation.delta.to_data()) + str(
            evaluation.inventory.to_data()
        )
        self.assertNotIn("do-not-persist-this-body", rendered)
        self.assertIn("secret.txt", rendered)

    async def test_recoverable_path_error_preserves_zero_delta_streak(self):
        call = ToolCall(
            "recover-path", "core.read_file",
            {"path": "src/external.py"},
            EvidenceQuestion("E4", "Read the discovered external file"),
        )
        result = ToolResult(
            call.call_id, False, data={"candidates": []},
            error_code="PATH_CONTEXT_REQUIRED", retryable=True,
            meta={"recoverable_input": True},
        )
        evaluation = await self.evaluator.evaluate(
            call, result, EvidenceInventory(consecutive_zero_delta=2)
        )
        self.assertEqual(evaluation.delta.consecutive_zero_delta, 2)
        self.assertEqual(
            evaluation.delta.result_status,
            "recoverable:PATH_CONTEXT_REQUIRED",
        )

    async def test_recoverable_scope_mismatch_is_not_evidence_or_zero_delta(self):
        call = ToolCall(
            "wrong-scope", "core.search_text",
            {"path": ".", "query": "Service"},
            EvidenceQuestion(
                "E5", "Find Service in the intended repository",
                expected_scope="/intended/repository",
            ),
        )
        result = ToolResult(
            call.call_id, False,
            data={
                "expected_scope": "/intended/repository",
                "resolved_root": "/actual/repository",
            },
            error_code="TOOL_SCOPE_MISMATCH", retryable=True,
            meta={"recoverable_input": True},
        )
        evaluation = await self.evaluator.evaluate(
            call, result, EvidenceInventory(consecutive_zero_delta=2)
        )
        self.assertEqual(evaluation.delta.total_new, 0)
        self.assertEqual(evaluation.delta.consecutive_zero_delta, 2)
        self.assertEqual(
            evaluation.delta.result_status, "recoverable:TOOL_SCOPE_MISMATCH"
        )


class RepeatedEvidenceModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        result_count = sum(
            isinstance(block, ToolResultBlock)
            for message in request.messages for block in message.content
        )
        if result_count >= 2:
            return ModelResponse(Message(
                "final", MessageRole.ASSISTANT, (TextBlock("done"),)
            ))
        self.calls += 1
        return ModelResponse(
            Message(
                f"call-{self.calls}", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    f"echo-{self.calls}", "fixture.echo",
                    {"text": "same observation"},
                    EvidenceQuestion("E1", "What does the fixture return?"),
                )),),
            ), FinishReason.TOOL_CALL,
        )


class EvidenceDeltaKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_kernel_persists_new_then_zero_delta_and_checkpoint_state(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=RepeatedEvidenceModel(),
                require_evidence_questions=True,
                evidence_delta_evaluator_adapter=StructuredEvidenceDeltaEvaluator(),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "inspect twice", Path(directory), "task-evidence-delta"
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                progress = []
                result = await app.kernel.run_agent_turn(
                    task.task_id, "inspect", on_progress=progress.append
                )
                self.assertIsInstance(result, AgentTurnResult)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                deltas = [
                    event.payload for event in events
                    if event.event_type == "evidence.delta_evaluated"
                ]
                self.assertEqual([item["total_new"] for item in deltas], [1, 0])
                self.assertEqual(deltas[1]["consecutive_zero_delta"], 1)
                self.assertNotIn("same observation", str(deltas))
                completed = [item for item in progress if item.ok is not None]
                self.assertEqual(
                    [item.evidence_delta for item in completed], [1, 0]
                )
                tool_results = [
                    block.result for message in result.messages
                    for block in message.content
                    if isinstance(block, ToolResultBlock)
                ]
                self.assertEqual(
                    [
                        item.meta["evidence_delta"]["total_new"]
                        for item in tool_results
                    ],
                    [1, 0],
                )
                stored = await app.kernel.get_task(task.task_id)
                checkpoint = stored.active_agent_checkpoint
                self.assertIsNone(checkpoint)
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
