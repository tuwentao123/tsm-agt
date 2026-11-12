import base64
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.export_langfuse_evaluation import LangfuseExporter, LangfuseExportError


class RecordingLangfuse(LangfuseExporter):
    def __init__(self):
        super().__init__("http://localhost:3000", "public-key", "private-key")
        self.requests = []
        self.rejected_spans = 0

    def request(self, path, payload=None):
        self.requests.append((path, payload))
        if path == "/api/public/projects":
            return {"data": [{"id": "test-project"}]}
        if self.rejected_spans and path == "/api/public/otel/v1/traces":
            return {"partialSuccess": {"rejectedSpans": str(self.rejected_spans)}}
        return {}


def sample_report():
    return {
        "case": "implementation", "label": "baseline", "trial": 1,
        "fixture_hash": "hash", "case_passed": False, "all_turns_run": True,
        "started_at_unix_nano": 100, "finished_at_unix_nano": 900,
        "model": {"model": "test-model"},
        "turns": [{
            "turn": 1, "task_id": "task-1", "state": "AWAITING_INPUT", "turn_completed": True,
            "started_at_unix_nano": 200, "finished_at_unix_nano": 800,
            "final_answer": "private answer",
            "request_diagnostics": ["private request"],
            "model_exchanges": [{
                "started_at_unix_nano": 250, "finished_at_unix_nano": 400,
                "usage": {"input_tokens": 10, "output_tokens": 3},
            }],
            "tool_names": ["core.run_command"],
            "model_tool_proposals": [
                {"tool": "core.run_command", "call_id": "test", "model_call": 1},
                {"tool": "unrequested tool", "call_id": "ignored", "model_call": 1},
            ],
            "tool_results": [{
                "call_id": "test", "ok": False, "exit_code": 1, "data": "private tool output",
                "started_at_unix_nano": 450, "finished_at_unix_nano": 600,
            }],
            "checks": {"agent_verified_after_last_edit": False, "independent_tests": True},
            "independent_tests": {"passed": True, "exit_code": 0},
        }],
    }


def span_attributes(span):
    return {attr["key"]: attr["value"]["stringValue"] for attr in span["attributes"]}


class LangfuseEvaluationTest(unittest.TestCase):
    def test_reads_default_yaml_without_environment_variables(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".tsm-agt" / "langfuse.yaml"
            config.parent.mkdir()
            config.write_text(
                'base_url: https://us.cloud.langfuse.com\npublic_key: "public"\nsecret_key: "secret"\n',
                encoding="utf-8",
            )
            with patch.object(Path, "home", return_value=home), patch.dict("os.environ", clear=True):
                exporter = LangfuseExporter.from_config()
            self.assertEqual(exporter.base_url, "https://us.cloud.langfuse.com")
            token = exporter.headers["Authorization"].removeprefix("Basic ")
            self.assertEqual(base64.b64decode(token), b"public:secret")

    def test_invalid_explicit_yaml_does_not_reveal_keys_or_fall_back_to_environment(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / "settings.yaml"
            config.write_text('secret_key: ["PRIVATE_KEY_MARKER"\n', encoding="utf-8")
            with patch.dict("os.environ", {"LANGFUSE_PUBLIC_KEY": "public", "LANGFUSE_SECRET_KEY": "secret"}):
                with self.assertRaisesRegex(LangfuseExportError, "Could not read Langfuse YAML") as error:
                    LangfuseExporter.from_config(config)
            self.assertNotIn("PRIVATE_KEY_MARKER", str(error.exception))

    def test_uses_environment_when_default_yaml_is_absent(self):
        with TemporaryDirectory() as directory, patch.object(Path, "home", return_value=Path(directory)):
            with patch.dict("os.environ", {
                "LANGFUSE_BASE_URL": "http://localhost:3000",
                "LANGFUSE_PUBLIC_KEY": "public",
                "LANGFUSE_SECRET_KEY": "secret",
            }, clear=True):
                self.assertEqual(LangfuseExporter.from_config().base_url, "http://localhost:3000")

    def test_exports_experiment_trace_and_boolean_scores_without_transcript(self):
        exporter = RecordingLangfuse()
        links = exporter.export([sample_report()])
        self.assertEqual(exporter.requests[0], ("/api/public/projects", None))
        route, payload = exporter.requests[1]
        self.assertEqual(route, "/api/public/otel/v1/traces")
        root, turn, model, tool = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(turn["parentSpanId"], root["spanId"])
        self.assertEqual(model["parentSpanId"], turn["spanId"])
        self.assertEqual(tool["parentSpanId"], turn["spanId"])
        self.assertEqual((tool["startTimeUnixNano"], tool["endTimeUnixNano"]), ("450", "600"))
        self.assertEqual(json.loads(span_attributes(tool)["langfuse.observation.output"])["exit_code"], 1)
        for span in (root, turn, model, tool):
            self.assertEqual(
                span_attributes(span)["langfuse.experiment.item.root_observation_id"], root["spanId"],
            )
        scores = [body for route, body in exporter.requests if route == "/api/public/scores"]
        self.assertEqual({entry["name"]: entry["value"] for entry in scores}, {
            "case_passed": 0, "turn_1.agent_verified_after_last_edit": 0, "turn_1.independent_tests": 1,
        })
        self.assertTrue(all(entry["dataType"] == "BOOLEAN" and entry["observationId"] == root["spanId"]
                            for entry in scores))
        self.assertEqual(links, [f"http://localhost:3000/project/test-project/traces/{root['traceId']}"])
        self.assertNotIn("private", json.dumps(exporter.requests))

    def test_repeated_export_preserves_ids_and_other_runs_share_dataset(self):
        exporter = RecordingLangfuse()
        baseline = sample_report()
        candidate = deepcopy(baseline)
        candidate["label"] = "candidate"
        first_links = exporter.export([baseline, candidate])
        first_requests = exporter.requests[:]
        exporter.requests.clear()
        self.assertEqual(exporter.export([baseline, candidate]), first_links)
        self.assertEqual(exporter.requests, first_requests)
        attrs = [
            span_attributes(body["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
            for route, body in exporter.requests if route == "/api/public/otel/v1/traces"
        ]
        self.assertEqual(attrs[0]["langfuse.experiment.dataset.id"], attrs[1]["langfuse.experiment.dataset.id"])
        self.assertEqual(attrs[0]["langfuse.experiment.item.id"], attrs[1]["langfuse.experiment.item.id"])
        self.assertNotEqual(attrs[0]["langfuse.experiment.id"], attrs[1]["langfuse.experiment.id"])

    def test_reports_partial_trace_rejection_before_sending_scores(self):
        exporter = RecordingLangfuse()
        exporter.rejected_spans = 1
        with self.assertRaisesRegex(LangfuseExportError, "rejected 1 spans"):
            exporter.export([sample_report()])
        self.assertEqual(len(exporter.requests), 2)
