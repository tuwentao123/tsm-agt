"""Real file reads, durable history and provider wires exercise request-only reuse."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

import pytest

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.posix_path import PosixWorkspacePath
from tsm_agt.adapters.windows_path import WindowsWorkspacePath
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ContextWindowManager, PromptTemplate
from tsm_agt.core.read_result_reuse import project_read_results
from tsm_agt.ports import (
    AdapterContext, Message, MessageRole, ModelRequest, RuntimeStorePort,
    TextBlock, ToolCall, ToolCallBlock, ToolInvocationContext, ToolResultBlock,
)
from tsm_agt.ports.model import ReasoningBlock
from tests.test_conversation_loop import ScriptedModel, _executing, text_response, tool_response


BODY = "".join(f"value_{i:03d} = 'observed source text'\n" for i in range(100))


def results(messages):
    return [b.result for m in messages for b in m.content if isinstance(b, ToolResultBlock)]


def exchange(call, result):
    return (
        Message("call-" + call.call_id, MessageRole.ASSISTANT, (ToolCallBlock(call),)),
        Message("result-" + call.call_id, MessageRole.TOOL, (ToolResultBlock(result),)),
    )


@pytest.fixture
def reader(tmp_path):
    """Use the production provider, including path checks and real SHA-256."""
    provider = CoreReadOnlyToolProvider()
    paths = WindowsWorkspacePath() if os.name == "nt" else PosixWorkspacePath()
    adapter = AdapterContext(config={}, emit_event=lambda *_: None)
    asyncio.run(provider.start(adapter))
    asyncio.run(paths.start(adapter))
    (tmp_path / "source.py").write_text(BODY, encoding="utf-8")

    def read(call_id, path="source.py", *, task="task", root=None, **arguments):
        call = ToolCall(call_id, "core.read_file", {"path": path, **arguments})
        context = ToolInvocationContext(
            invocation_id=call_id, task_id=task, turn_id="turn",
            workspace=root or tmp_path, workspace_path=paths,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        return call, asyncio.run(provider.invoke(call, context))
    return read


def test_full_results_are_durable_but_duplicates_are_references(reader):
    first, second, third = (reader(i) for i in ("a", "b", "c"))
    canonical = (*exchange(*first), *exchange(*second), *exchange(*third))
    snapshot = json.dumps([m.to_data() for m in canonical])
    projected = project_read_results(canonical, "task")
    a, b, c = results(projected.messages)
    assert a.data["content"] == BODY
    for result in (b, c):
        assert result.ok and "content" not in result.data
        assert result.data["status"] == "unchanged"
        assert result.data["content_returned"] is False
        assert result.data["reused_from_call_id"] == "a"
        assert result.fact_descriptor == second[1].fact_descriptor
    assert [r.call_id for r in (a, b, c)] == ["a", "b", "c"]
    assert json.dumps([m.to_data() for m in canonical]) == snapshot
    assert projected.stats.references == (("b", "a"), ("c", "a"))
    assert projected.stats.source_result_bytes > projected.stats.model_result_bytes
    assert BODY not in json.dumps(projected.stats.event_data())
    # Appending another call keeps the already-sent prefix stable.
    assert project_read_results(canonical[:4], "task").messages == projected.messages[:4]
    assert project_read_results(projected.messages, "task").messages == projected.messages


def test_short_and_empty_files_keep_their_body(reader, tmp_path):
    for body in ("", "x\n"):
        (tmp_path / "source.py").write_text(body)
        canonical = (*exchange(*reader("a")), *exchange(*reader("b")))
        assert project_read_results(canonical, "task").messages == canonical


@pytest.mark.parametrize("change", ["same_length_and_mtime", "atomic_replace", "outside_window"])
def test_file_changes_are_actually_observed(reader, tmp_path, change):
    path = tmp_path / "source.py"
    first = reader("a", max_lines=30)
    stamp = path.stat()
    updated = BODY.replace("value_099" if change == "outside_window" else "value_000", "value_NEW")
    if change == "atomic_replace":
        replacement = tmp_path / "replacement.py"
        replacement.write_text(updated)
        replacement.replace(path)
    else:
        path.write_text(updated)
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    second = reader("b", max_lines=30)
    assert first[1].data["sha256"] != second[1].data["sha256"]
    canonical = (*exchange(*first), *exchange(*second))
    assert project_read_results(canonical, "task").messages == canonical


@pytest.mark.parametrize("difference", ["path", "root", "task", "range", "request_limit"])
def test_identical_text_is_not_enough_to_reuse(reader, tmp_path, difference):
    first = reader("a", max_lines=30)
    if difference == "path":
        (tmp_path / "other.py").write_text(BODY)
        second = reader("b", path="other.py", max_lines=30)
    elif difference == "root":
        other = tmp_path / "other"
        other.mkdir()
        (other / "source.py").write_text(BODY)
        second = reader("b", root=other, max_lines=30)
    elif difference == "task":
        second = reader("b", task="other-task", max_lines=30)
    elif difference == "range":
        second = reader("b", start_line=2, max_lines=30)
    else:
        first = reader("a", max_lines=200)
        second = reader("b", max_lines=300)  # same actual body, different request
    canonical = (*exchange(*first), *exchange(*second))
    assert project_read_results(canonical, "task").messages == canonical


@pytest.mark.parametrize("invalid", ["no_fact", "authority", "hash", "range", "cursor", "redacted", "failed", "visible_text"])
def test_unprovable_or_transformed_results_keep_content(reader, invalid):
    first, (call, result) = reader("a", max_lines=30), reader("b", max_lines=30)
    if invalid == "no_fact":
        result = replace(result, fact_descriptor=None)
    elif invalid == "authority":
        result = replace(result, fact_descriptor=replace(result.fact_descriptor, authority="external"))
    elif invalid == "hash":
        result = replace(result, data={**result.data, "sha256": "0" * 64})
    elif invalid == "range":
        result = replace(result, data={**result.data, "end_line": None})
    elif invalid == "cursor":
        result = replace(result, data={**result.data, "next_cursor": None})
    elif invalid == "redacted":
        result = replace(result, meta={**result.meta, "result_governance": {"redacted": True}})
    elif invalid == "failed":
        result = replace(result, ok=False, error_code="PERMISSION_DENIED")
    else:
        result = replace(result, data={**result.data, "content": "different visible body" * 100})
    canonical = (*exchange(*first), *exchange(call, result))
    assert project_read_results(canonical, "task").messages == canonical


def test_pagination_preserves_unread_ranges_and_split_line_cursor(reader, tmp_path):
    (tmp_path / "source.py").write_text("长" * 17000 + "\nlast\n")
    first = reader("a")
    second = reader("b")
    canonical = (*exchange(*first), *exchange(*second))
    reference = results(project_read_results(canonical, "task").messages)[1]
    assert reference.truncated
    assert reference.data["next_cursor"] == {"start_line": 1, "start_column": 8001}
    assert reference.data["next_offset"] == 1
    following = reader("c", **reference.data["next_cursor"])
    canonical += exchange(*following)
    assert results(project_read_results(canonical, "task").messages)[2].data["content"] == "长" * 8000


def test_removed_anchor_promotes_surviving_full_result_and_reread(reader):
    canonical = tuple(m for i in ("a", "b", "c") for m in exchange(*reader(i)))
    fresh = project_read_results(canonical[2:], "task")
    assert results(fresh.messages)[0].data["content"] == BODY
    assert results(fresh.messages)[1].data["reused_from_call_id"] == "b"
    # Legacy projected-only input cannot claim its missing source is visible.
    old = project_read_results(canonical, "task").messages
    missing = project_read_results(old[2:], "task")
    assert all(r.data["status"] == "content_unavailable" for r in results(missing.messages))
    assert all("reused_from_call_id" not in r.data for r in results(missing.messages))
    recovered = project_read_results((*missing.messages, *exchange(*reader("d"))), "task")
    assert results(recovered.messages)[-1].data["content"] == BODY
    orphan = replace(results(old)[1], fact_descriptor=None)
    unprovable = project_read_results(exchange(reader("b")[0], orphan), "task")
    assert results(unprovable.messages)[0].data["status"] == "content_unavailable"


@pytest.mark.parametrize("change", ["deleted", "symlink_escape", "binary", "not_utf8"])
def test_previous_read_does_not_bypass_new_access_or_file_checks(reader, tmp_path, change):
    first = reader("a")
    path = tmp_path / "source.py"
    if change == "deleted":
        path.unlink()
    elif change == "symlink_escape":
        path.unlink()
        path.symlink_to(tmp_path.parent / "outside.py")
    else:
        path.write_bytes(b"binary\x00" if change == "binary" else b"\xff")
    second = reader("b")
    assert not second[1].ok
    canonical = (*exchange(*first), *exchange(*second))
    assert project_read_results(canonical, "task").messages == canonical


def test_context_budget_uses_projection_and_compaction_reselects_anchor(reader):
    template = PromptTemplate.default()
    user = Message("user", MessageRole.USER, (TextBlock("Inspect this source"),))
    canonical = (user, *exchange(*reader("a")),
                 Message("bridge", MessageRole.ASSISTANT, (TextBlock("old notes " * 1000),)),
                 *exchange(*reader("b")), *exchange(*reader("c")))
    manager = ContextWindowManager(recent_message_floor=4, history_soft_tokens=0)
    plain = manager.prepare(conversation=canonical, tools=(), prompt_template=template,
                            context_window=0, max_output_tokens=128)
    projected = manager.prepare(conversation=canonical, tools=(), prompt_template=template,
                                context_window=0, max_output_tokens=128, read_reuse_task_id="task")
    assert plain.messages == canonical
    assert projected.canonical_messages == canonical
    assert projected.budget.estimated_input_tokens < plain.budget.estimated_input_tokens
    assert projected.budget.estimated_input_tokens == manager.estimate_input_tokens(projected.messages, (), template)
    window = projected.budget.estimated_input_tokens  # trigger safe compaction
    compacted = manager.prepare(conversation=canonical, tools=(), prompt_template=template,
                                context_window=window, max_output_tokens=128, read_reuse_task_id="task")
    assert compacted.compaction is not None
    assert [r.call_id for r in results(compacted.messages)] == ["b", "c"]
    assert results(compacted.messages)[0].data["content"] == BODY
    assert results(compacted.messages)[1].data["reused_from_call_id"] == "b"
    assert all(r.data["content"] == BODY for r in results(compacted.canonical_messages))
    assert compacted.budget.estimated_input_tokens == manager.estimate_input_tokens(compacted.messages, (), template)
    assert compacted.budget.estimated_input_tokens <= compacted.budget.hard_input_tokens


@pytest.mark.parametrize("wire", ["chat", "responses", "anthropic"])
def test_final_provider_payload_retains_source_call_ids_and_reasoning(reader, wire):
    from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider
    from tsm_agt.adapters.openai_compatible.responses import OpenAIResponsesModelProvider
    from tsm_agt.adapters.anthropic_messages import AnthropicMessagesModelProvider

    pairs = [reader(i) for i in ("a", "b")]
    reasoning = {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "opaque"}
    blocks = ((ReasoningBlock("openai_responses", "reasoning", data=json.dumps(reasoning)),)
              if wire == "responses" else ())
    batch = Message("batch", MessageRole.ASSISTANT, blocks + tuple(ToolCallBlock(c) for c, _ in pairs))
    canonical = (batch, *(exchange(c, r)[1] for c, r in pairs))
    projection = project_read_results(canonical, "task")
    request = ModelRequest("turn", PromptTemplate.default().assemble(projection.messages, ()).messages)
    if wire == "responses":
        payload, _ = OpenAIResponsesModelProvider("https://example.test", "model", "key")._responses_payload(request)
        assert reasoning in payload["input"]
        rows = [row for row in payload["input"] if row.get("type") == "function_call_output"]
        ids, bodies = [r["call_id"] for r in rows], [json.loads(r["output"]) for r in rows]
    elif wire == "chat":
        payload, _ = OpenAICompatibleModelProvider("https://example.test", "model", "key")._build_payload(request)
        rows = [row for row in payload["messages"] if row["role"] == "tool"]
        ids, bodies = [r["tool_call_id"] for r in rows], [json.loads(r["content"]) for r in rows]
    else:
        payload, _ = AnthropicMessagesModelProvider("https://example.test", "model", "key")._build_payload(request)
        rows = [block for row in payload["messages"] for block in row["content"] if block["type"] == "tool_result"]
        ids, bodies = [r["tool_use_id"] for r in rows], [json.loads(r["content"]) for r in rows]
    assert ids == ["a", "b"]
    assert bodies[0]["data"]["content"] == BODY
    assert "content" not in bodies[1]["data"]
    assert bodies[1]["data"]["reused_from_call_id"] == "a"


class ReadReuseLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_reads_ledger_checkpoint_and_request_use_separate_views(self):
        await self._run_read_loop(compact=False)

    async def test_compacted_checkpoint_reopens_with_full_surviving_results(self):
        await self._run_read_loop(compact=True)

    async def _run_read_loop(self, *, compact):
        from tsm_agt.core.agent_loop import AgentTurnCheckpoint
        from tsm_agt.adapters.sqlite import SQLiteRuntimeStore

        class CapturingModel(ScriptedModel):
            application = None

            async def complete(self, request):
                task = await self.application.kernel.get_task("task-conv")
                raw = json.loads(json.dumps(task.active_agent_checkpoint))
                self.checkpoints.append(AgentTurnCheckpoint.from_data(raw))
                return await super().complete(request)

        class CountingReads(CoreReadOnlyToolProvider):
            count = 0

            async def invoke(self, call, context):
                if call.name == "core.read_file":
                    self.count += 1
                return await super().invoke(call, context)

        provider = CountingReads()
        model = CapturingModel([
            tool_response("m1", "a", "core.read_file", {"path": "source.py"}),
            tool_response("m2", "b", "core.read_file", {"path": "source.py"}),
            *([tool_response("m3", "c", "core.read_file", {"path": "source.py"})] if compact else []),
            text_response("final", "Inspected"),
        ])
        model.checkpoints = []
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = Path(temporary.name) / "runtime.db"
        application = compose_fixture_application(
            model_adapter=model, tool_adapters=(provider,),
            store_adapter=SQLiteRuntimeStore(database),
            context_manager=(ContextWindowManager(latency_soft_input_tokens=1, recent_message_floor=8)
                             if compact else ContextWindowManager()),
        )
        model.application = application
        await application.registry.start_all()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "source.py").write_text(BODY)
                task = await _executing(application, root)
                await application.kernel.run_agent_turn(task.task_id, "Inspect source.py twice")
                assert provider.count == (3 if compact else 2)
                visible = results(model.requests[-1].messages)
                assert visible, str([(m.message_id, m.role.value) for m in model.requests[-1].messages
                                     if not m.message_id.startswith("prompt-")])
                assert visible[0].data["content"] == BODY
                assert visible[1].data["reused_from_call_id"] == ("b" if compact else "a")
                # A real checkpoint serialization/restore retains both full bodies.
                saved = model.checkpoints[-1]
                assert [r.data["content"] for r in results(saved.messages)] == [BODY, BODY]
                assert project_read_results(saved.messages, task.task_id).stats.references == (
                    (("c", "b"),) if compact else (("b", "a"),))
                events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
                completed = [e.payload["result"] for e in events if e.event_type == "tool.completed"]
                assert [r["data"]["content"] for r in completed] == [BODY] * provider.count
                stats = next(e.payload for e in events if e.event_type == "context.read_results_reused")
                assert stats["reference_count"] == 1 and stats["saved_result_bytes"] > 0
                assert BODY not in json.dumps(stats)
                assert not any(e.event_type.startswith("completion_review.") for e in events)
                assert any(e.event_type == "context.compacted" for e in events) == compact
        finally:
            await application.registry.stop_all()
        # Reopen the actual database, rather than relying only on in-memory rows.
        reopened = SQLiteRuntimeStore(database)
        await reopened.start(AdapterContext(config={}, emit_event=lambda *_: None))
        try:
            persisted = await reopened.read_events("task-conv")
            assert sum(e.event_type == "tool.completed" for e in persisted) == provider.count
            assert all(e.payload["result"]["data"]["content"] == BODY
                       for e in persisted if e.event_type == "tool.completed")
        finally:
            await reopened.stop(datetime.now(timezone.utc) + timedelta(seconds=10))
