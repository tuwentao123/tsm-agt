from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.evaluate_multiturn_behavior import (
    check_files, file_state, load_fixture, materialize_workspace, model_tool_proposals,
    observe_model_exchanges, run_independent_tests, run_trial, tool_result_facts,
    verified_after_last_edit, workspace_path,
)
from tsm_agt.adapters.builtin.core_tools import CoreReadOnlyToolProvider
from tsm_agt.bootstrap import compose_fixture_application
from tests.test_conversation_loop import ScriptedModel, text_response, tool_response


class MultiTurnEvaluationTest(unittest.TestCase):
    def test_fixture_load_and_path_boundary(self):
        fixture = load_fixture(Path("scripts/fixtures/multiturn-implementation.json"))
        self.assertEqual(len(fixture["turns"]), 3)
        for name in ("../outside", "/tmp/outside", ".agent/runtime.db", ".env", ".git/config", "nested/.env", ""):
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

    def test_replay_starts_from_commit_and_overlays_fixture_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            git = ["git", "-C", str(repo)]
            subprocess.run([*git, "init", "-q"], check=True)
            (repo / "module.py").write_text("committed", encoding="utf-8")
            subprocess.run([*git, "add", "module.py"], check=True)
            subprocess.run([*git, "-c", "user.name=Eval", "-c", "user.email=eval@example.invalid",
                            "-c", "commit.gpgSign=false", "commit", "-qm", "snapshot"], check=True)
            commit = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
            (repo / "module.py").write_text("uncommitted", encoding="utf-8")
            (repo / "local-only").touch()

            workspace = root / "replay"
            workspace.mkdir()
            fixture = {"snapshot_commit": commit, "files": {"case.txt": "fixture"}}
            materialize_workspace(workspace, fixture, repo, 5)
            self.assertEqual((workspace / "module.py").read_text(encoding="utf-8"), "committed")
            self.assertEqual((workspace / "case.txt").read_text(encoding="utf-8"), "fixture")
            self.assertFalse((workspace / "local-only").exists())
            self.assertFalse((workspace / ".git").exists())

    def test_verification_requires_at_least_one_test(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertNotEqual(run_independent_tests(root, 5), 0)
            (root / "test_fixture.py").write_text(
                "import unittest\nclass Test(unittest.TestCase):\n    def test_valid(self): pass\n",
                encoding="utf-8",
            )
            self.assertEqual(run_independent_tests(root, 5), 0)

    def test_agent_verification_requires_a_successful_check_started_after_the_last_edit(self):
        from types import SimpleNamespace

        def requested(sequence, call_id, name, arguments):
            return SimpleNamespace(sequence=sequence, event_type="tool.requested", payload={
                "call": {"call_id": call_id, "name": name, "arguments": arguments},
            })

        def completed(sequence, call_id, succeeded):
            return SimpleNamespace(sequence=sequence, event_type="tool.completed", payload={
                "result": {"call_id": call_id, "ok": True, "data": {"succeeded": succeeded}},
            })

        edit = requested(1, "edit", "core.apply_patch", {"path": "calculator.py"})
        edit_result = completed(3, "edit", True)
        test_command = {"argv": ["python3", "-m", "unittest"]}
        early_test = requested(2, "test", "core.run_command", test_command)
        later_test = requested(4, "test", "core.run_command", test_command)
        self.assertFalse(verified_after_last_edit([edit, early_test, edit_result, completed(5, "test", True)]))
        self.assertFalse(verified_after_last_edit([edit, edit_result, later_test, completed(5, "test", False)]))
        self.assertTrue(verified_after_last_edit([edit, edit_result, later_test, completed(5, "test", True)]))


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
            args = argparse.Namespace(config=Path(directory) / "config.yaml", history_format="native",
                                      capture_requests=False, timeout=5, run_tests=False,
                                      label="transport-only", fixture=fixture_file)
            with patch("scripts.evaluate_multiturn_behavior.compose_openai_compatible_engineering_application_from_config", return_value=app):
                report = await run_trial(args, fixture, 1)
            first, second = report["turns"]
            self.assertEqual(first["task_id"], second["task_id"])
            self.assertTrue(first["automated_checks_passed"])
            self.assertFalse(second["automated_checks_passed"])
            self.assertFalse(report["case_passed"])
            self.assertEqual(second["final_answer_accuracy"], "requires_human_review")
            self.assertEqual(second["tool_names"], [])
            self.assertTrue(any(row.text == "I can implement A and B." for row in model.requests[-1].messages))

    async def test_historical_podfile_replay_grades_current_answer_and_preserves_workspace(self):
        fixture_path = Path("scripts/fixtures/replay-podfile-local.json")
        fixture = load_fixture(fixture_path)
        args = argparse.Namespace(config=None, history_format="native", capture_requests=False,
                                  timeout=5, run_tests=False, label="transport-only", fixture=fixture_path)
        for answer, expected in (("ReplaySupport → Modules/LocalKit", True), ("没找到本地配置", False)):
            with self.subTest(answer=answer):
                model = ScriptedModel([
                    tool_response("lookup", "find", "core.search_files",
                                  {"pattern": "Podfile.local", "target": "files"}),
                    tool_response("read", "open", "core.read_file", {"path": "Podfile.local"}),
                    text_response("first", "Podfile.local 使用 iOS 15.4，target 是 ReplayLocal。"),
                    text_response("follow-up", answer),
                ])
                app = compose_fixture_application(model_adapter=model, tool_adapters=(CoreReadOnlyToolProvider(),))
                with patch("scripts.evaluate_multiturn_behavior.compose_openai_compatible_engineering_application_from_config", return_value=app):
                    report = await run_trial(args, fixture, 1)
                first, second = report["turns"]
                self.assertTrue(first["checks"]["workspace_unchanged"])
                self.assertTrue(second["checks"]["workspace_unchanged"])
                self.assertEqual([item["ok"] for item in first["tool_results"]], [True, True])
                self.assertEqual(report["case_passed"], expected)
