"""Real instruction reads, ledger provenance and provider protocol at compaction."""
from __future__ import annotations

import json
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tsm_agt.adapters.builtin.skill_tools import CoreSkillToolProvider
from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.adapters.skill_catalog import SkillCatalog
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.adapters.anthropic_messages import AnthropicMessagesModelProvider
from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider
from tsm_agt.core import ContextWindowManager, PromptTemplate
from tsm_agt.core.agent_loop import AgentTurnCheckpoint
from tsm_agt.core.context_window import establish_window
from tsm_agt.core.conversation_loop.active_skills import active_skill_groups
from tsm_agt.core.conversation_loop.compaction import complete_window_compaction
from tsm_agt.core.conversation_loop.workflow_handoff import workflow_handoff, MAX_HANDOFF_BYTES
from tsm_agt.core.execution import ToolExecutionRecord, ToolCommitState
from tsm_agt.core.kernel import Kernel
from tsm_agt.core.skills import SkillRegistry
from tsm_agt.ports import (Message, MessageRole, ModelRequest, ModelUsage, TextBlock,
                           ToolCall, ToolCallBlock, ToolResultBlock, ToolResult,
                           ToolIdempotency, ToolRisk)
from tsm_agt.ports.context_summary import ContextSummary
from tests.test_context_compaction import tool_pair
from tests.test_model_streaming import InterruptOnceModel, executing_task


def committed(call, result, turn="turn"):
    return ToolExecutionRecord.start(
        task_id="task", turn_id=turn, invocation_id=call.call_id, call=call,
        payload_hash="fixture", policy_decision_id="fixture", effective_risk=ToolRisk.R0,
        approval_request_id=None, idempotency=ToolIdempotency.IDEMPOTENT,
    ).finish(ToolCommitState.COMMITTED, result)


class ContextHandoffTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = UserSkillsStore(Path(temp.name))
        self.store.install_upload({"SKILL.md": b"---\nname: fixture-check\ndescription: Check a fixture\n---\nRead the check output. Do not infer permission.\n"})
        self.registry = SkillRegistry()
        self.catalog = SkillCatalog(self.store)
        self.provider = CoreSkillToolProvider(self.registry, self.store, catalog=self.catalog)
        await self.provider.start(None)
        self.call = ToolCall("load", "skill.load", {"name": "fixture-check"})
        self.result = await self.provider.invoke(self.call, None)
        self.row = committed(self.call, self.result)
        self.group = (Message("load-call", MessageRole.ASSISTANT, (ToolCallBlock(self.call),)),
                      Message("load-result", MessageRole.TOOL, (ToolResultBlock(self.result),)))

    def select(self, *, rows=None, group=None, limit=16000, turn="turn"):
        group = group or self.group
        return active_skill_groups(group, rows or (self.row,), turn_id=turn,
                                   catalog=self.catalog, registry=self.registry,
                                   groups=(group,), budget_bytes=limit)

    async def test_current_unchanged_complete_skill_survives_as_original_tool_group(self):
        selected, receipt = self.select()
        self.assertEqual(selected, (self.group,))
        self.assertEqual(receipt["retained_call_ids"], ["load"])
        for provider in (OpenAICompatibleModelProvider("https://example.invalid/v1", "test", "test"),
                         AnthropicMessagesModelProvider("https://example.invalid", "test", "test")):
            payload = provider._build_payload(ModelRequest("turn", self.group))[0]
            self.assertIn("Do not infer permission", json.dumps(payload))
        # A new request does not silently reactivate a previous Turn's Skill.
        self.assertEqual(self.select(turn="next-turn")[0], ())
        steering = Message("steer", MessageRole.USER, (TextBlock("Switch to analysis; do not execute"),), scaffolding="steering")
        self.assertEqual(self.select(group=(*self.group, steering))[0], ())

    async def test_changed_deleted_truncated_and_failed_reload_are_not_active(self):
        truncated = replace(self.result, truncated=True)
        group = (self.group[0], replace(self.group[1], content=(ToolResultBlock(truncated),)))
        self.assertEqual(self.select(rows=(committed(self.call, truncated),), group=group)[0], ())
        failed_call = replace(self.call, call_id="failed-load")
        self.assertEqual(self.select(rows=(self.row, committed(failed_call, ToolResult("failed-load", False, error_code="SKILL_RESOURCE_PERMISSION_DENIED"))))[0], ())
        path = Path(self.result.data["path"])
        path.write_text(path.read_text().replace("Read the check", "Changed: read the check"))
        self.assertEqual(self.select()[0], ())
        path.unlink()
        self.assertEqual(self.select()[0], ())

    async def test_no_provenance_and_oversized_parallel_group_are_not_protected(self):
        self.assertEqual(self.select(rows=(replace(self.row, turn_id="old"),))[0], ())
        sibling = ToolCall("sibling", "web.search", {"query": "fixture"})
        group = (replace(self.group[0], content=(ToolCallBlock(self.call), ToolCallBlock(sibling))),
                 self.group[1], Message("sibling-result", MessageRole.TOOL,
                                       (ToolResultBlock(ToolResult("sibling", True, {"body": "x" * 20000})),)))
        # The unrelated large sibling is never cut off to retain only the Skill.
        self.assertEqual(self.select(group=group)[0], ())
        self.assertEqual(self.select(limit=1)[0], ())

    async def test_compaction_preserves_skill_outside_summary_and_rebuilds_handoff(self):
        template = PromptTemplate.default()
        baseline, window = establish_window(template, (), (), "identity")
        user = Message("user", MessageRole.USER, (TextBlock("Continue the fixture repair"),))
        source = (*baseline, user, *self.group, *tool_pair("noise", "old output " * 700),
                  *tool_pair("recent", "recent output"))
        manager = ContextWindowManager(latency_soft_input_tokens=1, recent_message_floor=2)
        prepared = manager.prepare(conversation=source, tools=(), prompt_template=template,
                                   context_window=32000, max_output_tokens=128)
        self.assertFalse(any(message.message_id == "load-result" for message in prepared.canonical_messages))
        summary_inputs, events = [], []
        class Summarizer:
            async def summarize(self, messages, **kwargs):
                summary_inputs.extend(messages)
                return ContextSummary("Next: run the documented fixture check.", ModelUsage(2, 1), 1)
        task = SimpleNamespace(task_id="task", tool_executions={"load": self.row}, mutation_journal=())
        async def get_task(task_id):
            return task
        async def append(task_id, rows):
            events.extend(rows)
        kernel = SimpleNamespace(_dependencies=SimpleNamespace(context_manager=manager,
            context_summarizer=Summarizer(), prompt_template=template, skill_catalog=self.catalog,
            skill_registry=self.registry), get_task=get_task, _append_events=append,
            _source_user_message=Kernel._source_user_message)
        checkpoint = AgentTurnCheckpoint("task", "turn", 1, source, (), (), 0, 0, 0, 0, 10, 10, 128, 10,
                                         context_window_state=window)
        state = SimpleNamespace(messages=list(source), task_id="task", turn_id="turn",
                                visible_tool_by_name={}, checkpoint=checkpoint)
        memory = Message("working-memory-context-record", MessageRole.USER, (TextBlock(json.dumps({
            "task_id": "task", "remaining_work": ["run the documented fixture check"],
        })),))
        result = await complete_window_compaction(kernel, state, prepared, (), (memory,), "identity")
        self.assertIn(self.group[0], result.canonical_messages)
        self.assertIn(self.group[1], result.canonical_messages)
        self.assertNotIn(self.group[1], summary_inputs)
        handoff = next(json.loads(m.text) for m in result.messages if m.message_id.startswith("workflow-handoff-context-"))
        self.assertEqual(handoff["latest_request"]["text_excerpt"], user.text)
        self.assertEqual(handoff["coverage_assessment"], "not_assessed")
        self.assertEqual(next(data for kind, data in events if kind == "context.active_skills_retained")["retained_call_ids"], ["load"])
        Kernel._normalize_and_validate_model_boundary(result.messages)
        self.assertNotIn("load-result", result.compaction.forgotten_message_ids)
        self.assertIn("latency_target", result.compaction.trigger_reasons)
        # Remove the optional view to compute a tight but valid baseline. The
        # retention optimization must release whole exchanges rather than
        # overflow or strip just one result from a parallel tool group.
        minimal = tuple(message for message in result.canonical_messages
                        if message not in self.group and not message.message_id.startswith("workflow-handoff-context-"))
        tight_limit = manager.estimate_input_tokens(minimal, (), template) + 20
        tight = replace(prepared, budget=replace(prepared.budget, hard_input_tokens=tight_limit))
        released = await complete_window_compaction(kernel, state, tight, (), (memory,), "identity")
        self.assertLessEqual(released.budget.estimated_input_tokens, tight_limit)
        Kernel._normalize_and_validate_model_boundary(released.messages)
        self.assertTrue(next(data for kind, data in reversed(events) if kind == "context.active_skills_retained")["released_for_capacity"])

    async def test_handoff_separates_author_claim_and_exit_zero_from_verification(self):
        now = datetime.now(timezone.utc)
        call = ToolCall("command", "core.run_command", {"argv": ["unrelated", "command"]})
        row = committed(call, ToolResult("command", True, {"exit_code": 0, "output_observed": True}))
        task = SimpleNamespace(task_id="task", tool_executions={"command": row}, mutation_journal=(
            SimpleNamespace(mutation_id="mutation-new", path="source.py", after_hash="new-version", created_at=now),))
        memory = Message("working-memory-context-test", MessageRole.USER, (TextBlock(json.dumps({
            "task_id": "task", "completed_work": ["model claims checks passed"],
            "remaining_work": ["run documented check"], "evidence": [{"reference": "x" * 20000}],
        })),))
        user = Message("current-user", MessageRole.USER, (TextBlock("Only analyze; do not execute"),))
        message = workflow_handoff(task, user, (memory,))
        body = json.loads(message.text)
        self.assertLessEqual(len(message.text.encode()), MAX_HANDOFF_BYTES)
        self.assertTrue(body["details_omitted"])
        self.assertEqual(body["model_authored_progress"]["completed_work"], ["model claims checks passed"])
        self.assertEqual(body["coverage_assessment"], "not_assessed")
        self.assertEqual(body["recent_process_facts"][0]["exit_code"], 0)
        self.assertEqual(body["recent_edits"][0]["after_hash"], "new-version")
        self.assertEqual(workflow_handoff(task, user, (), budget_bytes=1), None)
        steering = Message("steer", MessageRole.USER, (TextBlock("The latest requested scope"),), scaffolding="steering")
        steered = json.loads(workflow_handoff(task, user, (), history=(user, steering)).text)
        self.assertEqual(steered["latest_request"]["text_excerpt"], steering.text)

    async def test_nonempty_handoff_is_not_duplicated_or_echoed_after_stream_cancel_resume(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(model_adapter=InterruptOnceModel(), tool_adapters=(),
                store_adapter=SQLiteRuntimeStore(root / "runtime.db"), enable_working_memory=True)
            await app.start()
            try:
                task = await executing_task(app, root)
                memory = await app.kernel.get_working_memory(task.task_id)
                await app.kernel.update_working_memory(task.task_id, memory.revision,
                    {**memory.to_data(), "remaining_work": ["Use the known check entry"]}, "fixture-handoff", "fixture")
                with self.assertRaises(asyncio.CancelledError):
                    await app.kernel.run_agent_turn(task.task_id, "Continue the actual user request")
                await app.kernel.interrupt_agent_turn(task.task_id)
                before = (await app.kernel.get_task(task.task_id)).active_agent_checkpoint
                initial_notes = [row for row in before["messages"] if row["message_id"].startswith("workflow-handoff-context-")]
                self.assertEqual(len(initial_notes), 1)
                result = await app.kernel.resume_checkpointed_agent_turn(task.task_id)
                self.assertEqual(result.assistant_message.text, "Continue the actual user request")
                session = await app.kernel.get_session(task.session_id)
                final_notes = [row for row in session.model_context_window["messages"]
                               if row["message_id"].startswith("workflow-handoff-context-")]
                self.assertEqual(len(final_notes), 1)
            finally:
                await app.registry.stop_all()
