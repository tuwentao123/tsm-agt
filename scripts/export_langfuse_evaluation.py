"""Publish saved evaluation reports to Langfuse as experiments and traces."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from tsm_agt.adapters.langfuse import (
    LangfuseClient, LangfuseExportError, attributes, trace_payload,
)


def digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def score(trace_id, observation_id, name, passed):
    return {
        "id": str(UUID(digest(trace_id, observation_id, name)[:32])),
        "traceId": trace_id,
        "observationId": observation_id,
        "name": name,
        "value": int(passed),
        "dataType": "BOOLEAN",
    }


def trial_payload(report, experiment_id, dataset_id, fallback_time):
    case, trial = report["case"], report["trial"]
    item_id = str(UUID(digest(dataset_id, case, trial)[:32]))
    trace_id = digest(experiment_id, item_id)[:32]
    root_id = digest(trace_id, "root")[:16]
    timing = "recorded" if "started_at_unix_nano" in report else "report_snapshot"
    context = {
        "langfuse.experiment.id": experiment_id,
        "langfuse.experiment.name": f"{report['label']}-{experiment_id[:8]}",
        "langfuse.experiment.dataset.id": dataset_id,
        "langfuse.experiment.item.id": item_id,
        "langfuse.experiment.item.root_observation_id": root_id,
        "langfuse.environment": "evaluation",
    }
    spans = []

    def add_span(span_id, parent, name, kind, source, fields, start_fallback, end_fallback=None):
        start = source.get("started_at_unix_nano", start_fallback)
        end = source.get("finished_at_unix_nano", end_fallback if end_fallback is not None else start)
        span = {
            "traceId": trace_id, "spanId": span_id, "name": name, "kind": 1,
            "startTimeUnixNano": str(start), "endTimeUnixNano": str(max(start, end)),
            "attributes": attributes({
                **context, "langfuse.observation.type": kind, **fields,
            }),
        }
        if parent is not None:
            span["parentSpanId"] = parent
        spans.append(span)
        return start, max(start, end)

    started, finished = add_span(root_id, None, case, "agent", report, {
        "langfuse.observation.input": json.dumps({"case": case, "trial": trial}),
        "langfuse.observation.output": json.dumps({
            "passed": report["case_passed"], "all_turns_run": report["all_turns_run"],
        }),
        "langfuse.experiment.item.metadata.fixture_hash": report["fixture_hash"],
        "langfuse.observation.metadata.timing_source": timing,
        "langfuse.observation.metadata.snapshot_commit": report.get("snapshot_commit"),
        "langfuse.observation.metadata.model": report.get("model", {}).get("model"),
        "langfuse.observation.level": "ERROR" if not report["case_passed"] else None,
    }, fallback_time)
    scores = [score(trace_id, root_id, "case_passed", report["case_passed"])]

    for turn in report["turns"]:
        number = turn["turn"]
        turn_id = digest(trace_id, "turn", number)[:16]
        independent = turn.get("independent_tests") or {}
        _, turn_end = add_span(turn_id, root_id, f"Turn {number}", "chain", turn, {
            "langfuse.observation.output": json.dumps({
                "state": turn["state"], "completed": turn["turn_completed"],
                "independent_tests": {key: independent[key] for key in ("passed", "exit_code") if key in independent},
                "waiting_for_user": turn.get("blocked_by_explicit_user_action", False),
                "runtime_failures": [
                    {key: failure[key] for key in ("event_type", "error_type", "error_code", "failure_code")
                     if key in failure}
                    for failure in turn.get("runtime_failures", ())
                ],
            }),
            "langfuse.observation.level": "ERROR" if turn.get("runtime_failures") else None,
        }, started, finished)

        for index, exchange in enumerate(turn["model_exchanges"], start=1):
            add_span(digest(trace_id, "model", number, index)[:16], turn_id,
                     f"Model response {index}", "generation", exchange, {
                "langfuse.observation.model.name": report.get("model", {}).get("model"),
                "langfuse.observation.usage_details": json.dumps({
                    "input": exchange.get("usage", {}).get("input_tokens", 0),
                    "output": exchange.get("usage", {}).get("output_tokens", 0),
                }),
                "langfuse.observation.output": json.dumps({
                    "finish_reason": exchange.get("finish_reason"),
                }),
            }, turn_end)

        requested = set(turn["tool_names"])
        proposals = {call["call_id"]: call for call in turn["model_tool_proposals"]}
        results = {result["call_id"]: result for result in turn["tool_results"]}
        for index, call_id in enumerate(dict.fromkeys([*proposals, *results]), start=1):
            result = results.get(call_id, {})
            call = proposals.get(call_id, {})
            name = result.get("tool") or call.get("tool")
            if name not in requested:
                continue
            outcome = {
                key: result[key]
                for key in ("ok", "error_code", "exit_code", "execution_state", "commit_state")
                if key in result
            }
            add_span(digest(trace_id, "tool", number, index)[:16], turn_id,
                     f"{index:02d} {name}", "tool", result, {
                "langfuse.observation.metadata.model_response": call.get("model_call"),
                "langfuse.observation.level": "ERROR" if result.get("ok") is False else None,
                "langfuse.observation.output": json.dumps({"result_recorded": bool(result), **outcome}),
            }, turn_end)
        for name, passed in turn["checks"].items():
            scores.append(score(trace_id, root_id, f"turn_{number}.{name}", passed))

    return trace_id, trace_payload("tsm-agt-eval", spans), scores


class LangfuseExporter(LangfuseClient):
    def export(self, reports, *, fallback_time=None):
        if not reports:
            raise LangfuseExportError("The evaluation report is empty")
        project = self.request("/api/public/projects")
        projects = project.get("data", ())
        if len(projects) != 1 or not projects[0].get("id"):
            raise LangfuseExportError("Langfuse project keys must identify one project")
        project_id = quote(projects[0]["id"], safe="")
        fallback_time = time.time_ns() if fallback_time is None else fallback_time
        links = []
        for label in dict.fromkeys(report["label"] for report in reports):
            group = sorted((report for report in reports if report["label"] == label),
                           key=lambda report: (report["case"], report["trial"]))
            if len({(report["case"], report["trial"]) for report in group}) != len(group):
                raise LangfuseExportError("Use a separate label for each experiment run")
            dataset_id = str(UUID(digest(sorted({(r["case"], r["fixture_hash"]) for r in group}))[:32]))
            # Task IDs and recorded timestamps are stable when re-uploading the same report.
            experiment_id = str(UUID(digest(dataset_id, label, [
                (r["case"], r["trial"], r.get("started_at_unix_nano"), [t["task_id"] for t in r["turns"]])
                for r in group
            ])[:32]))
            for report in group:
                trace_id, trace, scores = trial_payload(report, experiment_id, dataset_id, fallback_time)
                self.send_trace(trace, description=report["case"])
                for entry in scores:
                    self.request("/api/public/scores", entry)
                links.append(f"{self.base_url}/project/{project_id}/traces/{trace_id}")
        return links


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="JSONL report from evaluate_multiturn_behavior")
    parser.add_argument("--langfuse-config", type=Path, help="Langfuse YAML file (default: ~/.tsm-agt/langfuse.yaml)")
    args = parser.parse_args()
    try:
        exporter = LangfuseExporter.from_config(args.langfuse_config)
        reports = [json.loads(line) for line in args.report.read_text(encoding="utf-8").splitlines() if line.strip()]
        for link in exporter.export(reports, fallback_time=args.report.stat().st_mtime_ns):
            print(link)
    except (LangfuseExportError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"Langfuse export failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
