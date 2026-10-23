from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.evaluate_multiturn_behavior import (
    check_files, file_state, load_fixture, model_tool_proposals, observe_model_exchanges,
    run_trial, tool_result_facts, workspace_path,
)
from tsm_agt.bootstrap import compose_fixture_application
from tests.test_conversation_loop import ScriptedModel, text_response


class MultiTurnEvaluationTest(unittest.TestCase):
    def test_fixture_load_and_path_boundary(self):
        fixture = load_fixture(Path("scripts/fixtures/multiturn-implementation.json"))
        self.assertEqual(len(fixture["turns"]), 3)
        for name in ("../outside", "/tmp/outside", ".agent/runtime.db", ".env", ".git/config", ""):
            with self.subTest(name=name), self.assertRaises((ValueError, IndexError)):
                workspace_path(Path("/disposable-evaluation"), name)

    def test_file_assertions_not_mutation_counts_determine_observed_correctness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file.txt").write_text("wrong", encoding="utf-8")
            before = file_state(root)
            (root / "file.txt").write_text("still wrong", encoding="utf-8")
            checks = check_files(root, {"files_equal": {"file.txt": "correct"}, "unchanged": True}, before)
            self.assertFalse(checks["files_equal:file.txt"])
            self.assertFalse(checks["workspace_unchanged"])
            (root / ".hidden").write_text("a change", encoding="utf-8")
            self.assertIn(".hidden", file_state(root))

    def test_reports_failures_and_model_proposals_not_just_successful_calls(self):
        from types import SimpleNamespace
        events = [
            SimpleNamespace(event_type="llm.completed", payload={"model_call": 1, "message": {
                "content": [{"type": "tool_call", "call_id": "c1", "name": "core.apply_patch",
                             "arguments": {"patch": "PRIVATE BODY"}}],
            }}),
            SimpleNamespace(event_type="tool.failed", payload={"commit_state": "FAILED", "result": {
                "call_id": "c1", "ok": False, "error_code": "CONFLICT", "data": {"execution_state": "not_started"},
            }}),
        ]
        self.assertEqual(model_tool_proposals(events)[0]["tool"], "core.apply_patch")
        self.assertEqual(tool_result_facts(events)[0]["error_code"], "CONFLICT")
        self.assertNotIn("PRIVATE BODY", json.dumps(model_tool_proposals(events)))


class MultiTurnEvaluationRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_probe_observes_streaming_tools_and_restores_provider_without_bodies(self):
        from tsm_agt.ports import (FinishReason, Message, MessageRole, ModelRequest,
                                   ModelResponse, ModelStreamCompleted, ToolCall, ToolCallBlock)

        response = ModelResponse(Message("model-response", MessageRole.ASSISTANT, (
            ToolCallBlock(ToolCall("call", "core.apply_patch", {"patch": "PRIVATE PATCH"})),
        )), FinishReason.TOOL_CALL)

        class Provider:
            async def complete(self, request):
                return response

            async def stream_complete(self, request):
                yield ModelStreamCompleted(await self.complete(request))

        provider = Provider()
        complete = provider.complete
        stream = provider.stream_complete
        observations = []
        restore = observe_model_exchanges(provider, observations)
        try:
            request = ModelRequest("turn", ())
            received = [item async for item in provider.stream_complete(request)]
            self.assertEqual(received, [ModelStreamCompleted(response)])
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0]["proposed_tools"], [{"call_id": "call", "tool": "core.apply_patch"}])
            self.assertNotIn("PRIVATE PATCH", json.dumps(observations))
        finally:
            restore()
        self.assertEqual(provider.complete, complete)
        self.assertEqual(provider.stream_complete, stream)

    async def test_real_sdk_multiturn_harness_does_not_treat_noop_model_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_file = Path(directory) / "fixture.json"
            fixture = {
                "files": {"module.py": "before"},
                "turns": [{"input": "Analyze only", "unchanged": True},
                          {"input": "Implement both", "files_equal": {"module.py": "after"}}],
            }
            fixture_file.write_text(json.dumps(fixture), encoding="utf-8")
            model = ScriptedModel([text_response("proposal", "I can implement A and B."),
                                   text_response("no-op", "Both are complete.")])
            app = compose_fixture_application(model_adapter=model, tool_adapters=())
            args = argparse.Namespace(env_file=Path(directory) / ".env", history_format="native",
                                      capture_requests=False, timeout=5, run_tests=False,
                                      label="transport-only", fixture=fixture_file)
            with patch("scripts.evaluate_multiturn_behavior.compose_openai_compatible_engineering_application_from_env", return_value=app):
                report = await run_trial(args, fixture, 1)
            first, second = report["turns"]
            self.assertEqual(first["task_id"], second["task_id"])
            self.assertTrue(first["automated_checks_passed"])
            self.assertFalse(second["automated_checks_passed"])
            self.assertEqual(second["final_answer_accuracy"], "requires_human_review")
            self.assertEqual(second["tool_names"], [])
            self.assertTrue(any(row.text == "I can implement A and B." for row in model.requests[-1].messages))
