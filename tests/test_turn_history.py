"""History attribution is factual bookkeeping, not task interpretation."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace

from tsm_agt.core.session import SessionSnapshot
from tsm_agt.core.session_context import SessionContextProjector
from tsm_agt.core.context import ContextWindowExceeded, ContextWindowManager
from tsm_agt.core.prompt import PromptTemplate, PromptTemplateSegment
from tsm_agt.core.session_history import compact_history_body, normalize_history_body
from tsm_agt.ports import Message, MessageRole, ModelRequest, SessionEvent, TextBlock, ToolResult, ToolResultBlock
from tests import test_session_context as fixtures


class HistoryConfigurationTest(unittest.TestCase):
    def test_default_native_and_explicit_json_override_without_policy_changes(self):
        from pathlib import Path
        from tsm_agt.bootstrap.context_configuration import load_context_configuration, session_context_projector

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            native = load_context_configuration(path, {})
            baseline = load_context_configuration(path, {"TSM_AGT_SESSION_HISTORY_FORMAT": "json"})
            self.assertEqual(session_context_projector(native).conversation_history_format, "native")
            self.assertEqual(session_context_projector(baseline).conversation_history_format, "json")
            self.assertEqual(native.compaction_ratio, baseline.compaction_ratio)
            self.assertEqual(native.history_soft_tokens, 16384)
            tuned = load_context_configuration(path, {"TSM_AGT_CONTEXT_HISTORY_SOFT_TOKENS": "2048"})
            self.assertEqual(tuned.history_soft_tokens, 2048)
            with self.assertRaisesRegex(ValueError, "HISTORY_SOFT_TOKENS"):
                load_context_configuration(path, {"TSM_AGT_CONTEXT_HISTORY_SOFT_TOKENS": "-1"})
            with self.assertRaisesRegex(ValueError, "SESSION_HISTORY_FORMAT"):
                load_context_configuration(path, {"TSM_AGT_SESSION_HISTORY_FORMAT": "invalid"})


def result_event(sequence: int, turn: str, user: str, assistant: str) -> SessionEvent:
    original = fixtures.SessionContextProjectorTest._event(sequence, user, assistant)
    payload = {
        **original.payload, "task_id": "task-1", "turn_id": turn,
        "user_message": Message(f"user-{turn}", MessageRole.USER, (TextBlock(user),)).to_data(),
        "assistant_message": Message(
            f"assistant-{turn}", MessageRole.ASSISTANT, (TextBlock(assistant),),
        ).to_data(),
        "task_summary": {
            **original.payload["task_summary"], "task_id": "task-1", "turn_id": turn,
        },
    }
    return replace(original, payload=payload)


class TurnHistoryAttributionTest(unittest.TestCase):
    def project(self, *events):
        snapshot = SessionSnapshot.create("session-1", "uid:1", "conversation")
        for _ in events:
            snapshot = snapshot.bump_context()
        return SessionContextProjector().project(snapshot, events)

    def test_same_task_retains_each_turn_but_task_index_is_an_explicit_latest_view(self):
        projection = self.project(
            result_event(1, "turn-1", "Inspect", "I can implement A and B."),
            result_event(2, "turn-2", "Implement both", "Implemented A."),
        )
        self.assertEqual([s.turn_id for s in projection.task_summaries], ["turn-1", "turn-2"])
        body = SessionContextProjector().for_prompt(projection, conversation_mode=True).prompt_data
        self.assertEqual([s["turn_id"] for s in body["recent_task_summaries"]], ["turn-1", "turn-2"])
        self.assertEqual(len(body["task_index"]), 1)
        self.assertEqual(body["task_index"][0]["source_turn_id"], "turn-2")

    def test_current_reads_cannot_be_attached_to_previous_turn_summary(self):
        projector = SessionContextProjector()
        projection = self.project(result_event(1, "turn-1", "Inspect", "Proposed A and B"))
        failed = fixtures.SessionContextProjectorTest._execution(
            call_id="patch", tool="core.apply_patch_v4a", arguments={"patch": "private"}, data={},
        )
        failed = replace(failed, result=ToolResult(
            "patch", False, error_code="CONFLICT", message="context did not match",
        ))
        reads = tuple(replace(failed, execution_id=f"turn-2:read-{n}", turn_id="turn-2") for n in range(8))
        facts = projector.project_recent_executions(
            {"task-1": (failed, *reads)}, exclude_task_id="task-1", exclude_turn_id="turn-2",
            conversation_mode=True,
        )
        body = projector.for_prompt(projection, recent_executions=facts, conversation_mode=True).prompt_data
        events = body["recent_task_summaries"][0]["execution_events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["turn_id"], "turn-1")
        self.assertEqual(events[0]["result"]["error_code"], "CONFLICT")
        self.assertEqual(events[0]["call"]["call_id"], "patch")

    def test_unknown_attribution_is_not_guessed(self):
        projector = SessionContextProjector()
        projection = self.project(result_event(1, "turn-1", "Inspect", "Proposal"))
        record = fixtures.SessionContextProjectorTest._execution(
            call_id="read", tool="core.read_file", arguments={}, data={},
        )
        facts = projector.project_recent_executions({"task-1": (replace(record, turn_id=""),)})
        body = projector.for_prompt(projection, recent_executions=facts).prompt_data
        self.assertNotIn("execution_events", body["recent_task_summaries"][0])
        self.assertEqual(body["unattributed_execution_events"][0]["turn_attribution"], "unknown")

    def test_command_transport_success_does_not_erase_nonzero_outcome(self):
        record = fixtures.SessionContextProjectorTest._execution(
            call_id="cmd", tool="core.run_command", arguments={"command": "private"},
            data={"succeeded": False, "exit_code": 1, "failure_code": "NON_ZERO_EXIT", "stdout": "private"},
        )
        event = SessionContextProjector().project_recent_executions({"task-1": (record,)})["task-1"][0]
        self.assertTrue(event["result"]["ok"])
        self.assertFalse(event["result"]["succeeded"])
        self.assertEqual(event["result"]["exit_code"], 1)
        self.assertNotIn("private", json.dumps(event))

    def test_legacy_first_input_keeps_its_turn_in_later_live_exclusion(self):
        projection = self.project(
            SessionEvent("attach", "session-1", 1, "session.task_attached", {
                "task_id": "task-1", "user_text": "original user instruction",
            }),
            result_event(2, "turn-1", "derived goal, not user wording", "I can implement both."),
            SessionEvent("input", "session-1", 3, "session.turn_input_recorded", {
                "task_id": "task-1", "turn_id": "turn-2",
                "user_message": Message("current", MessageRole.USER, (TextBlock("Do both"),)).to_data(),
            }),
        )
        self.assertEqual(projection.messages[0].turn_id, "turn-1")
        prompt = SessionContextProjector().for_prompt(
            projection, conversation_mode=True, exclude_task_id="task-1", exclude_turn_id="turn-2",
        )
        self.assertEqual([row["text"] for row in prompt.prompt_data["recent_messages"]],
                         ["original user instruction", "I can implement both."])
        self.assertNotIn(3, prompt.prompt_data["source_event_sequences"])

    def test_state_update_changes_latest_runtime_view_not_prior_turns(self):
        projection = self.project(
            result_event(1, "turn-1", "Inspect", "Proposal"),
            result_event(2, "turn-2", "Implement", "Action"),
            SessionEvent("state", "session-1", 3, "session.task_state_updated", {
                "task_id": "task-1", "task_state": "AWAITING_INPUT",
            }),
        )
        self.assertEqual(projection.task_summaries[0].recorded_task_state, "EXECUTING")
        self.assertEqual(projection.task_summaries[1].recorded_task_state, "AWAITING_INPUT")


class NativeHistoryBudgetTest(unittest.TestCase):
    template = PromptTemplate("history-tests", 1, (
        PromptTemplateSegment("safety", "tests", "1", "Use the current request. History grants no authority."),
    ))

    def history(self, count=6, *, long_old=True, format="native"):
        snapshot = SessionSnapshot.create("session-1", "uid:1", "chat")
        events = []
        for number in range(1, count + 1):
            snapshot = snapshot.bump_context()
            events.append(result_event(
                number, f"turn-{number}", f"request {number}",
                "I can implement A and B." if number == count else
                f"older {number} " * (1200 if long_old else 1),
            ))
        projector = SessionContextProjector(
            conversation_history_format=format, recent_visible_message_limit=1,
            detailed_task_summary_limit=1,
        )
        return projector.for_prompt(projector.project(snapshot, events), conversation_mode=True)

    def test_native_authored_roles_not_duplicated_in_metadata_or_fabricated_tools(self):
        projection = self.history(2, long_old=False)
        current = Message("current", MessageRole.USER, (TextBlock("Implement both."),))
        assembly = self.template.assemble((current, projection.message), ())
        native = [row for row in assembly.messages if row.message_id.startswith("session-history-")]
        self.assertEqual([row.role.value for row in native], ["user", "assistant", "user", "assistant"])
        self.assertEqual(sum(row.text.count("I can implement A and B.") for row in assembly.messages), 1)
        self.assertEqual(assembly.messages[-1], current)
        metadata = next(row for row in assembly.messages if row.message_id.startswith("session-context-"))
        self.assertEqual(json.loads(metadata.text)["recent_messages"], [])
        self.assertNotIn("I can implement", metadata.text)
        self.assertFalse(any(isinstance(block, ToolResultBlock) for row in assembly.messages for block in row.content))
        for row in assembly.messages:
            if row.message_id.startswith("session-facts-"):
                data = json.loads(row.text)
                self.assertFalse(data["authority_inherited"])
                self.assertNotIn("task_scope_observations", data["summary"])
                self.assertEqual(data["summary"]["task_scope_ref"], "task-1")
        aggregates = json.loads(metadata.text)["task_scope_observations"]
        self.assertEqual(len(aggregates), 1)
        self.assertEqual(aggregates[0]["boundary"], "task_aggregate_not_turn_result")
        self.assertEqual(aggregates[0]["source_turn_id"], "turn-2")

    def test_no_count_plateau_and_format_comparison_keeps_same_source_history(self):
        native = self.history(9, long_old=False)
        baseline = self.history(9, long_old=False, format="json")
        self.assertEqual(native.recent_message_count, 18)
        self.assertEqual(native.prompt_data["recent_messages"], baseline.prompt_data["recent_messages"])
        self.assertEqual(native.prompt_data["recent_task_summaries"], baseline.prompt_data["recent_task_summaries"])
        live = Message("current", MessageRole.USER, (TextBlock("Do it."),))
        a = self.template.assemble((native.message, live), ())
        b = self.template.assemble((baseline.message, live), ())
        self.assertEqual(a.receipt.stable_prefix_hash, b.receipt.stable_prefix_hash)
        self.assertEqual(a.receipt.ordered_toolset_hash, b.receipt.ordered_toolset_hash)

    def test_budget_drops_complete_turns_protects_proposal_and_accounts_for_every_message(self):
        history = self.history()
        live = Message("current", MessageRole.USER, (TextBlock("Implement both."),))
        prepared = ContextWindowManager(trigger_ratio=.8, recent_message_floor=1).prepare(
            conversation=(live, history.message), tools=(), prompt_template=self.template,
            context_window=3300, max_output_tokens=128,
        )
        self.assertIsNotNone(prepared.compaction)
        session = next(row for row in prepared.messages if row.message_id.startswith("session-context-"))
        body = json.loads(session.text)
        kept = body["recent_messages"]
        self.assertEqual([row["text"] for row in kept[-2:]], ["request 6", "I can implement A and B."])
        self.assertEqual(len(kept) % 2, 0)
        self.assertEqual(len(kept) + body["earlier_summary"]["message_count"], 12)
        self.assertTrue(body["earlier_summary"]["source_event_ranges"])
        self.assertIn(1, prepared.compaction.forgotten_event_sequences)
        self.assertIn(live, prepared.messages)
        self.assertLessEqual(prepared.budget.estimated_input_tokens, prepared.budget.hard_input_tokens)

    def test_latest_oversized_proposal_is_not_silently_truncated(self):
        projection = self.history(1)
        body = dict(projection.prompt_data)
        body["recent_messages"] = [
            {**row, "text": row["text"] * 1000} for row in body["recent_messages"]
        ]
        oversized = Message(projection.message.message_id, MessageRole.USER, (TextBlock(json.dumps(body)),))
        with self.assertRaises(ContextWindowExceeded):
            ContextWindowManager().prepare(
                conversation=(Message("live", MessageRole.USER, (TextBlock("Do it"),)), oversized),
                tools=(), prompt_template=self.template, context_window=3000, max_output_tokens=128,
            )

    def test_compaction_does_not_mutate_original_or_promote_assistant_excerpts(self):
        original = self.history(4).prompt_data
        before = json.dumps(original, sort_keys=True)
        reduced = compact_history_body(original, 1)
        self.assertEqual(json.dumps(original, sort_keys=True), before)
        self.assertEqual(reduced["earlier_summary"]["message_count"], 6)
        self.assertTrue(reduced["earlier_summary"]["turn_previews"][0]["messages"][1]["truncated"])
        self.assertIn("not verified", reduced["earlier_summary"]["warning"])

    def test_public_provider_wire_payloads_accept_native_history_without_tool_orphans(self):
        from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider
        from tsm_agt.adapters.anthropic_messages import AnthropicMessagesModelProvider

        history = self.history(2, long_old=False)
        current = Message("live", MessageRole.USER, (TextBlock("Implement both now."),))
        request = ModelRequest("turn-current", self.template.assemble((current, history.message), ()).messages)
        for provider in (
            OpenAICompatibleModelProvider("https://example.invalid/v1", "test", "not-a-key"),
            AnthropicMessagesModelProvider("https://example.invalid", "test", "not-a-key"),
        ):
            payload, _names = provider._build_payload(request)
            encoded = json.dumps(payload, ensure_ascii=False)
            self.assertEqual(encoded.count("I can implement A and B."), 1)
            self.assertEqual(encoded.count("Implement both now."), 1)
            self.assertNotIn('"role": "tool"', encoded)
            self.assertNotIn('"type": "tool_result"', encoded)

    def test_old_excerpts_keep_their_matching_failure_facts(self):
        history = self.history(2, long_old=False)
        body = dict(history.prompt_data)
        summaries = [dict(item) for item in body["recent_task_summaries"]]
        summaries[0]["execution_events"] = [{
            "task_id": "task-1", "turn_id": "turn-1",
            "call": {"call_id": "failed", "tool": "core.read_file"},
            "result": {"execution_id": "turn-1:failed", "ok": False, "error_code": "NOT_FOUND"},
        }]
        body["recent_task_summaries"] = summaries
        reduced = compact_history_body(body, 1)
        preview = reduced["earlier_summary"]["turn_previews"][0]
        self.assertEqual(preview["execution_facts"][0]["turn_id"], "turn-1")
        self.assertEqual(preview["execution_facts"][0]["result"]["error_code"], "NOT_FOUND")

    def test_inflight_user_input_remains_protected_even_when_not_first_or_recent(self):
        steering = Message("steering-1", MessageRole.USER, (TextBlock("Do not change tests."),), scaffolding="steering")
        self.assertTrue(ContextWindowManager._is_protected_group((steering,)))

    def test_large_provider_window_does_not_invite_filling_it_with_old_replies(self):
        history = self.history(21)
        live = Message("live", MessageRole.USER, (TextBlock("Implement both now."),))
        manager = ContextWindowManager(history_soft_tokens=5000)
        prepared = manager.prepare(conversation=(history.message, live), tools=(), prompt_template=self.template,
                                   context_window=128000, max_output_tokens=8192)
        self.assertIsNotNone(prepared.compaction)
        self.assertLessEqual(prepared.budget.event_data()["history_estimated_tokens"], 5000)
        self.assertFalse(prepared.budget.event_data()["history_soft_limit_exceeded"])
        native = self.template.assemble(prepared.messages, ()).messages
        self.assertEqual(sum(item.text == "I can implement A and B." for item in native), 1)
        self.assertEqual(native[-1], live)
        # Preparing an already reduced checkpoint cannot inflate the history.
        again = manager.prepare(conversation=prepared.messages, tools=(), prompt_template=self.template,
                                context_window=128000, max_output_tokens=8192)
        self.assertEqual(again.messages, prepared.messages)

    def test_latest_proposal_soft_overflow_is_explicit_not_a_truncated_authorization(self):
        history = self.history(1)
        body = dict(history.prompt_data)
        text = "Implement A then B and preserve user changes. " * 250
        body["recent_messages"][-1]["text"] = text
        message = Message(history.message.message_id, MessageRole.USER, (TextBlock(json.dumps(body)),))
        prepared = ContextWindowManager(history_soft_tokens=1200).prepare(
            conversation=(message, Message("live", MessageRole.USER, (TextBlock("Do both."),))),
            tools=(), prompt_template=self.template, context_window=128000, max_output_tokens=8192,
        )
        self.assertTrue(prepared.budget.event_data()["history_soft_limit_exceeded"])
        self.assertIn(text, [item.text for item in self.template.assemble(prepared.messages, ()).messages])

    def test_read_payload_deduplication_keeps_calls_and_failures_then_sheds_only_bodies(self):
        body = dict(self.history(2, long_old=False).prompt_data)
        text = "unique-read-body-" * 50
        events = [{
            "task_id": "task-1", "turn_id": f"turn-{n}",
            "call": {"tool": "core.read_file", "call_id": f"read-{n}", "arguments": {"path": "a.py"}},
            "result": {"execution_id": f"read-{n}", "ok": True,
                       "observation": {"path": "a.py", "content": text, "content_included": True}},
        } for n in (1, 2)]
        failure = {"task_id": "task-1", "turn_id": "turn-2", "call": {"call_id": "patch"},
                   "result": {"ok": False, "error_code": "CONFLICT", "message": "Context did not match",
                              "execution_state": "not_started", "historical_approval": {"decision": "deny"}}}
        for summary, event in zip(body["recent_task_summaries"], events):
            summary["execution_events"] = [event]
        body["recent_task_summaries"][-1]["execution_events"].append(failure)
        normalized = normalize_history_body(body)
        self.assertEqual(json.dumps(normalized).count(text), 1)
        self.assertEqual(len(normalized["read_payloads"]), 1)
        self.assertEqual(normalize_history_body(normalized), normalized)
        stripped = compact_history_body(normalized, 2, include_previews=False, omit_read_bodies=True)
        self.assertEqual(stripped["read_payloads"], {})
        self.assertEqual(stripped["recent_task_summaries"][-1]["execution_events"][-1], failure)
        self.assertEqual([item["execution_events"][0]["call"]["call_id"] for item in stripped["recent_task_summaries"]],
                         ["read-1", "read-2"])
        self.assertNotIn(text, json.dumps(stripped))
        self.assertIn("content", events[0]["result"]["observation"])  # original not modified

    def test_old_checkpoint_cumulative_snapshots_are_replaced_by_latest_once(self):
        body = dict(self.history(3, long_old=False).prompt_data)
        body.pop("task_scope_observations")
        for number, summary in enumerate(body["recent_task_summaries"], 1):
            summary["task_scope_observations"] = {
                "boundary": "task_aggregate_not_turn_result", "mutations": [{"path": f"latest-{number}"}],
            }
        normalized = normalize_history_body(body)
        self.assertEqual(normalized["task_scope_observations"], [{
            "boundary": "task_aggregate_not_turn_result", "task_id": "task-1", "source_turn_id": "turn-3",
            "mutations": [{"path": "latest-3"}],
        }])
        self.assertEqual(json.dumps(normalized).count('"mutations"'), 1)

    def test_unknown_provider_window_still_bounds_history_but_not_live_protocol(self):
        history = self.history(8)
        live = Message("live", MessageRole.USER, (TextBlock("Do both."),))
        result = ContextWindowManager(history_soft_tokens=4000).prepare(
            conversation=(history.message, live), tools=(), prompt_template=self.template,
            context_window=0, max_output_tokens=1024,
        )
        self.assertLessEqual(result.budget.event_data()["history_estimated_tokens"], 4000)
        self.assertEqual(result.budget.hard_input_tokens, 0)
        self.assertIn(live, result.messages)

    def test_conversation_soft_allocation_does_not_trigger_legacy_scoped_compaction(self):
        history = self.history(4)
        body = dict(history.prompt_data)
        body.pop("history_budget")
        legacy = Message(history.message.message_id, MessageRole.USER, (TextBlock(json.dumps(body)),))
        live = Message("live", MessageRole.USER, (TextBlock("Continue"),))
        result = ContextWindowManager(history_soft_tokens=1000).prepare(
            conversation=(legacy, live), tools=(), prompt_template=self.template,
            context_window=128000, max_output_tokens=8192,
        )
        self.assertIsNone(result.compaction)
        self.assertEqual(result.messages, (legacy, live))

    def test_read_bodies_are_shed_before_turns_without_touching_live_tool_protocol(self):
        from tsm_agt.ports import ToolCall, ToolCallBlock

        history = self.history(3, long_old=False)
        body = dict(history.prompt_data)
        events = [{
            "task_id": "task-1", "turn_id": "turn-3",
            "call": {"tool": "core.read_file", "call_id": f"read-{number}"},
            "result": {"execution_id": f"read-{number}", "ok": True,
                       "observation": {"path": f"{number}.py", "content": str(number) + "source " * 140}},
        } for number in range(12)]
        body["recent_task_summaries"][-1]["execution_events"] = events
        history_message = Message(history.message.message_id, MessageRole.USER, (TextBlock(json.dumps(body)),))
        live = (
            Message("live", MessageRole.USER, (TextBlock("Implement both."),)),
            Message("live-call", MessageRole.ASSISTANT, (
                ToolCallBlock(ToolCall("c-live", "core.read_file", {"path": "actual.py"})),
            )),
            Message("live-result", MessageRole.TOOL, (
                ToolResultBlock(ToolResult("c-live", True, data={"content": "complete live result"})),
            )),
        )
        result = ContextWindowManager(history_soft_tokens=5000, recent_message_floor=1).prepare(
            conversation=(history_message, *live), tools=(), prompt_template=self.template,
            context_window=128000, max_output_tokens=8192,
        )
        reduced = json.loads(result.messages[0].text)
        self.assertEqual(len(reduced["recent_messages"]), 6)
        self.assertEqual(len(reduced["recent_task_summaries"][-1]["execution_events"]), 12)
        self.assertEqual(reduced["read_payloads"], {})
        self.assertEqual(result.messages[1:], live)
