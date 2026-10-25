"""End-to-end tests for the model-led conversation loop.

The loop's contract is intentionally narrow, so these tests pin exactly that
contract: a text answer ends the turn, a tool result is written back as a
``tool`` role message, a rejected call comes back as a tool-role failure, and an
exhausted iteration budget is reported as a partial boundary rather than success.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AgentTurnSuspended,
    AgentTurnIncomplete,
    AgentTurnResult,
    ApprovalDecision,
    TaskState,
)
from tsm_agt.core.conversation_loop import (
    model_tool_exposure_profile,
    select_model_tools,
)
from tsm_agt.ports import (
    AdapterContext,
    AdapterDescriptor,
    CompletionReviewAction,
    CompletionReviewMode,
    CompletionReviewVerdict,
    FinishReason,
    HealthState,
    HealthStatus,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    RuntimeStorePort,
    TextBlock,
    ToolCall,
    ToolCallBlock,
    ToolIdempotency,
    ToolInvocationContext,
    ToolResult,
    ToolResultAuthority,
    ToolResultBlock,
    ToolRecoveryKind,
    ToolRisk,
    ToolSpec,
)


class ScriptedCompletionReviewer(EchoModelProvider):
    descriptor = AdapterDescriptor(
        "fixture.completion-reviewer", "1.0", "CompletionReviewerPort", "1.0"
    )

    def __init__(self, verdicts: list[CompletionReviewVerdict]) -> None:
        super().__init__()
        self.verdicts = verdicts
        self.candidates = []

    async def review(self, candidate):
        self.candidates.append(candidate)
        return self.verdicts.pop(0)


class FailingCompletionReviewer(EchoModelProvider):
    descriptor = AdapterDescriptor(
        "fixture.failing-completion-reviewer", "1.0",
        "CompletionReviewerPort", "1.0",
    )

    async def review(self, candidate):
        raise RuntimeError("review transport unavailable")


class ScriptedModel(EchoModelProvider):
    """Deterministic provider that plays a fixed list of responses."""

    capabilities = ProviderCapabilities(tools=True, context_window=32_000)
    descriptor = AdapterDescriptor(
        "fixture.scripted-model", "1.0", "ModelProviderPort", "1.0"
    )

    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__()
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("scripted model ran out of responses")
        return self._responses.pop(0)


def text_response(message_id: str, text: str) -> ModelResponse:
    return ModelResponse(
        Message(message_id, MessageRole.ASSISTANT, (TextBlock(text),)),
        FinishReason.STOP, ModelUsage(5, 4),
    )


def tool_response(
    message_id: str, call_id: str, name: str, arguments: dict,
) -> ModelResponse:
    call = ToolCall(call_id, name, arguments)
    return ModelResponse(
        Message(
            message_id, MessageRole.ASSISTANT,
            (ToolCallBlock(call),),
        ),
        FinishReason.TOOL_CALL, ModelUsage(6, 3),
    )


class ScriptedToolProvider:
    """Read-only fake tools named like the real retrieval tools."""

    descriptor = AdapterDescriptor(
        adapter_id="fixture.scripted-tools",
        adapter_version="1.0.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset({"web.search", "web.open", "fixture.fail"}),
    )

    _TOOLS = (
        ToolSpec(
            "web.search", "Return fake search results.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"], "additionalProperties": False,
            },
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
            result_authority=ToolResultAuthority.EXTERNAL_SERVICE,
        ),
        ToolSpec(
            "web.open", "Return a fake opened document.",
            {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"], "additionalProperties": False,
            },
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
            result_authority=ToolResultAuthority.EXTERNAL_SERVICE,
        ),
        ToolSpec(
            "fixture.fail", "Always fails, for recovery tests.",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"], "additionalProperties": False,
            },
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
        ),
        ToolSpec(
            "fixture.list_paths", "Return locations only, never content.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"], "additionalProperties": False,
            },
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
            result_authority=ToolResultAuthority.WORKSPACE_FACT,
        ),
    )

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "scripted tools",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return self._TOOLS

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext,
    ) -> ToolResult:
        self.calls.append(call.name)
        if call.name == "fixture.list_paths":
            return ToolResult(
                call.call_id, True,
                data={
                    "query": call.arguments.get("query", ""),
                    "files": [
                        {"path": "src/engine/run.py", "type": "blob"},
                        {"path": "src/engine/loop.py", "type": "blob"},
                    ],
                    "truncated": False,
                },
            )
        if call.name == "web.search":
            return ToolResult(
                call.call_id, True,
                data={
                    "results": [
                        {"title": "NBA roundup", "url": "https://news.example/nba",
                         "ref_id": "ref-nba"},
                    ],
                    "truncated": False,
                },
                meta={"untrusted_data": True},
            )
        if call.name == "web.open":
            return ToolResult(
                call.call_id, True,
                data={
                    "document_ref": "webdoc:nba",
                    "canonical_url": call.arguments.get("url", ""),
                    "content_hash": "sha256:abc",
                    "content": "The Lakers beat the Celtics 112-108.",
                    "total_chars": 36,
                    "preview_truncated": False,
                    "document_truncated": False,
                    "truncated": False,
                },
                meta={"untrusted_data": True},
            )
        return ToolResult(
            call.call_id, False,
            error_code="UPSTREAM_FAILED",
            message="the fake tool always fails",
            retryable=True,
            recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
            recovery_action={"action": "change_query"},
            meta={"untrusted_data": True},
        )


async def _executing(application, root: Path, task_id: str = "task-conv"):
    task = await application.kernel.create_task("chat", root, task_id)
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await application.kernel.transition_task(
            task.task_id, state, state.value
        )
    return task


class ConversationLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_never_summarizes_tool_outcomes_as_commentary(self):
        model = ScriptedModel([
            tool_response("first", "first-call", "fixture.fail", {"text": "test"}),
            tool_response("second", "second-call", "web.search", {"query": "example"}),
            text_response("final", "done"),
        ])
        application, _ = await self._application(model)
        progress = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                await application.kernel.run_agent_turn(task.task_id, "执行两个步骤", on_progress=progress.append)
                events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
                self.assertFalse(any(
                    event.event_type == "conversation.commentary" for event in events
                ))
                self.assertFalse(any(
                    item.kind.value == "commentary" for item in progress
                ))
        finally:
            await application.registry.stop_all()

    async def test_each_tool_proposal_can_publish_model_commentary(self):
        call = ToolCall("second-call", "web.open", {"url": "https://news.example/nba"})
        model = ScriptedModel([
            ModelResponse(Message("first", MessageRole.ASSISTANT, (
                TextBlock("我会先检索相关新闻，再阅读最相关的正文。"),
                ToolCallBlock(ToolCall(
                    "first-call", "web.search", {"query": "NBA"},
                )),
            )), FinishReason.TOOL_CALL, ModelUsage(6, 3)),
            ModelResponse(Message("second", MessageRole.ASSISTANT, (
                TextBlock("找到一篇摘要，接下来查看正文。"), ToolCallBlock(call),
            )), FinishReason.TOOL_CALL, ModelUsage(6, 3)),
            text_response("final", "done"),
        ])
        application, _ = await self._application(model)
        progress = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                await application.kernel.run_agent_turn(task.task_id, "执行两个步骤", on_progress=progress.append)
                events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
                notes = [p for p in progress if p.kind.value == "commentary"]
                self.assertEqual(
                    [(p.phase, p.activity) for p in notes],
                    [
                        ("model", "我会先检索相关新闻，再阅读最相关的正文。"),
                        ("model", "找到一篇摘要，接下来查看正文。"),
                    ],
                )
                commentary = [
                    event for event in events
                    if event.event_type == "conversation.commentary"
                ]
                requested = [
                    event for event in events
                    if event.event_type == "tool.requested"
                ]
                self.assertEqual(len(commentary), len(requested))
                self.assertTrue(all(
                    note.sequence < tool.sequence
                    for note, tool in zip(commentary, requested, strict=True)
                ))
        finally:
            await application.registry.stop_all()

    async def test_invalid_tool_response_does_not_publish_its_note(self):
        call = ToolCall("duplicate", "web.search", {"query": "example"})
        model = ScriptedModel([ModelResponse(
            Message("invalid", MessageRole.ASSISTANT, (
                TextBlock("这段说明不应显示。"), ToolCallBlock(call), ToolCallBlock(call),
            )), FinishReason.TOOL_CALL, ModelUsage(6, 3),
        )])
        application, _ = await self._application(model)
        progress = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                with self.assertRaises(Exception):
                    await application.kernel.run_agent_turn(task.task_id, "查资料", on_progress=progress.append)
                events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
                self.assertFalse(any(event.event_type == "conversation.commentary" for event in events))
                self.assertFalse(any(item.kind.value == "commentary" for item in progress))
        finally:
            await application.registry.stop_all()

    async def test_first_tool_call_commentary_is_progress_and_survives_as_event(self):
        from tsm_agt.web.app import _turn_progress_item

        call = ToolCall("call-note", "web.search", {"query": "example"})
        model = ScriptedModel([
            ModelResponse(
                Message("with-note", MessageRole.ASSISTANT, (
                    TextBlock("先查找相关资料。"), ToolCallBlock(call),
                )), FinishReason.TOOL_CALL, ModelUsage(6, 3),
            ),
            text_response("final", "这是最终答复。"),
        ])
        application, _ = await self._application(model)
        progress = []
        final_deltas = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                await application.kernel.run_agent_turn(
                    task.task_id, "查资料", on_progress=progress.append,
                    on_text_delta=final_deltas.append,
                )
                events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
                note = next(event for event in events if event.event_type == "conversation.commentary")
                requested = next(event for event in events if event.event_type == "tool.requested")
                self.assertLess(note.sequence, requested.sequence)
                self.assertEqual(_turn_progress_item(note)["activity"], "先查找相关资料。")
                notes = [item for item in progress if item.kind.value == "commentary"]
                self.assertEqual([item.activity for item in notes], ["先查找相关资料。"])
                self.assertNotIn("先查找相关资料。", "".join(final_deltas))
                self.assertEqual(sum(event.event_type == "conversation.commentary" for event in events), 1)
        finally:
            await application.registry.stop_all()

    def test_tool_exposure_never_uses_request_keyword_profiles(self):
        source_tools = (
            *ScriptedToolProvider._TOOLS,
            ToolSpec(
                "source.github.get_file", "Read source.", {"type": "object"},
                ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
            ),
            ToolSpec(
                "source.github.search_code", "Search source.", {"type": "object"},
                ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
            ),
        )
        request = "分析 Codex 源码是怎么实现多任务输入处理的"

        self.assertEqual(
            select_model_tools(source_tools, user_text=request, model_call=1),
            source_tools,
        )
        self.assertEqual(
            model_tool_exposure_profile(
                source_tools, user_text=request, model_call=1,
            ),
            "all-visible",
        )
        self.assertEqual(
            select_model_tools(source_tools, user_text=request, model_call=2),
            source_tools,
        )

    def test_normal_tool_set_is_independent_of_turn_number_and_wording(self):
        request = "解释一下什么是提示词工程"
        tools = ScriptedToolProvider._TOOLS

        self.assertEqual(
            select_model_tools(tools, user_text=request, model_call=1), tools,
        )
        self.assertEqual(
            model_tool_exposure_profile(tools, user_text=request, model_call=1),
            "all-visible",
        )

    def test_git_and_change_wording_do_not_hide_tools(self):
        def tool(name: str) -> ToolSpec:
            return ToolSpec(name, name, {"type": "object"}, ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT)
        tools = tuple(tool(name) for name in (
            "core.list_files", "core.search_files", "core.read_file",
            "core.apply_patch_v4a", "core.run_command", "code.diagnostics",
            "git.status", "git.diff", "git.log", "git.show", "git.blame",
            "git.branches", "web.search", "web.fetch_markdown", "web.open",
            "core.memory_list",
        ))
        git_selected = select_model_tools(tools, user_text="查看 git 提交历史和 diff", model_call=1)
        self.assertEqual(model_tool_exposure_profile(tools, user_text="查看 git 提交历史和 diff", model_call=1), "all-visible")
        self.assertEqual(git_selected, tools)
        change_selected = select_model_tools(tools, user_text="修复当前项目的报错", model_call=1)
        self.assertEqual(model_tool_exposure_profile(tools, user_text="修复当前项目的报错", model_call=1), "all-visible")
        self.assertEqual(change_selected, tools)
        self.assertEqual(select_model_tools(tools, user_text="修复当前项目的报错", model_call=2), tools)

    async def _application(
        self, model, *, with_evidence_delta: bool = False,
        completion_reviewer=None, completion_review_failure_policy=None,
        completion_review_mode=None,
    ):
        from tsm_agt.adapters.structured_evidence import (
            StructuredEvidenceDeltaEvaluator,
        )

        provider = ScriptedToolProvider()
        application = compose_fixture_application(
            model_adapter=model, tool_adapters=(provider,),
            evidence_delta_evaluator_adapter=(
                StructuredEvidenceDeltaEvaluator() if with_evidence_delta
                else None
            ),
            completion_reviewer_adapter=completion_reviewer,
            completion_review_mode=(
                completion_review_mode or CompletionReviewMode.ALWAYS
                if completion_reviewer is not None else CompletionReviewMode.OFF
            ),
            **({"completion_review_failure_policy": completion_review_failure_policy}
               if completion_review_failure_policy is not None else {}),
        )
        await application.registry.start_all()
        return application, provider

    async def test_turn_intent_is_never_advertised_to_the_model(self):
        model = ScriptedModel([text_response("direct", "Hello there.")])
        # No explicit tool adapters: turn intent is model reasoning, not a tool.
        application = compose_fixture_application(
            model_adapter=model,
        )
        await application.registry.start_all()
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                advertised = [
                    tool.name for tool in await application.kernel.list_tools()
                ]
                self.assertNotIn("core.turn_intent", advertised)
                result = await application.kernel.run_agent_turn(
                    task.task_id, "say hi"
                )
                self.assertIsInstance(result, AgentTurnResult)
                request_tools = [
                    tool.name for tool in model.requests[0].tools
                ]
                self.assertNotIn("core.turn_intent", request_tools)
                self.assertNotIn("core.task_outcome_complete", request_tools)
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                types = [event.event_type for event in events]
                self.assertNotIn("turn.intent_declared", types)
                self.assertNotIn("tool.intent_attempted", types)
                completed = next(event for event in events
                                 if event.event_type == "llm.completed")
                structure = completed.payload["prompt_message_structure"]
                self.assertEqual(completed.payload["prompt_message_structure_stage"],
                                 "provider_neutral_after_compaction")
                self.assertEqual([row["role"] for row in structure],
                                 [message.role.value for message in model.requests[0].messages])
                self.assertNotIn("say hi", str(structure))
        finally:
            await application.registry.stop_all()

    async def test_completion_reviewer_gets_tool_results_and_continues(self):
        model = ScriptedModel([
            tool_response(
                "list", "call-paths", "fixture.list_paths", {"query": "engine"},
            ),
            text_response("premature", "The engine runs a loop in run.py."),
            text_response("final", "I could not read the files; unverified."),
        ])
        reviewer = ScriptedCompletionReviewer([
            CompletionReviewVerdict(
                CompletionReviewAction.CONTINUE, "source_content_not_read",
                "Implementation content was not read.",
                ("Read content supporting the implementation claim.",),
            ),
            CompletionReviewVerdict(
                CompletionReviewAction.ACCEPT, "blocker_reported",
                "The answer accurately reports the limitation.",
            ),
        ])
        application, provider = await self._application(
            model, completion_reviewer=reviewer,
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                result = await application.kernel.run_agent_turn(
                    task.task_id, "how does the engine work"
                )
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(result.assistant_message.text, "I could not read the files; unverified.")
                # One tool call, then two samples: the asked-once re-sample.
                self.assertEqual(provider.calls, ["fixture.list_paths"])
                self.assertEqual(len(model.requests), 3)
                self.assertEqual(len(reviewer.candidates), 2)
                self.assertEqual(
                    reviewer.candidates[0].tool_executions[0].tool_name,
                    "fixture.list_paths",
                )
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                types = [event.event_type for event in events]
                self.assertEqual(types.count("turn.completion_review_completed"), 2)
                self.assertTrue(any(
                    message.scaffolding == "completion-review"
                    for message in result.messages
                ))
                session_events = await application.registry.require(
                    RuntimeStorePort
                ).read_session_events(task.session_id)
                result_event = next(
                    event for event in session_events
                    if event.event_type == "session.task_result_recorded"
                )
                self.assertIs(result_event.payload["assistant_replay_eligible"], True)
        finally:
            await application.registry.stop_all()

    async def test_repeated_rejected_candidate_stops_without_looping(self):
        answer = "I inspected both implementations."
        model = ScriptedModel([
            text_response("draft-1", answer), text_response("draft-2", answer),
        ])
        reviewer = ScriptedCompletionReviewer([
            CompletionReviewVerdict(
                CompletionReviewAction.CONTINUE, "sources_missing",
                "No source content was read.", ("Relevant source content",),
            ),
            CompletionReviewVerdict(
                CompletionReviewAction.CONTINUE, "sources_still_missing",
                "No source content was read.", ("Relevant source content",),
            ),
        ])
        application, _ = await self._application(
            model, completion_reviewer=reviewer,
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                result = await application.kernel.run_agent_turn(
                    task.task_id, "compare the source"
                )
                self.assertIsInstance(result, AgentTurnIncomplete)
                self.assertEqual(result.reason, "repeated_rejected_candidate")
                self.assertEqual(len(model.requests), 2)
                self.assertEqual(len(reviewer.candidates), 2)
        finally:
            await application.registry.stop_all()

    async def test_reviewer_failure_commits_an_unverified_answer_by_default(self):
        model = ScriptedModel([text_response("draft", "Unreviewed answer")])
        application, _ = await self._application(
            model, completion_reviewer=FailingCompletionReviewer(),
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                result = await application.kernel.run_agent_turn(
                    task.task_id, "inspect current state"
                )
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(result.assistant_message.text, "Unreviewed answer")
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                self.assertIn(
                    "turn.completion_review_bypassed",
                    [event.event_type for event in events],
                )
                session_events = await application.registry.require(
                    RuntimeStorePort
                ).read_session_events(task.session_id)
                result_event = next(
                    event for event in session_events
                    if event.event_type == "session.task_result_recorded"
                )
                self.assertIs(result_event.payload["assistant_replay_eligible"], True)
        finally:
            await application.registry.stop_all()

    async def test_required_review_skips_an_uncontracted_turn(self):
        model = ScriptedModel([text_response("direct", "Hello there.")])
        reviewer = ScriptedCompletionReviewer([])
        application, _ = await self._application(
            model, completion_reviewer=reviewer,
            completion_review_mode=CompletionReviewMode.REQUIRED,
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                result = await application.kernel.run_agent_turn(task.task_id, "say hi")
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(reviewer.candidates, [])
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                self.assertIn(
                    "turn.completion_review_skipped",
                    [event.event_type for event in events],
                )
        finally:
            await application.registry.stop_all()

    async def test_direct_answer_completes_without_a_completion_contract(self):
        model = ScriptedModel([text_response("direct", "Hello there.")])
        application, provider = await self._application(model)
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                result = await application.kernel.run_agent_turn(
                    task.task_id, "say hi"
                )
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(result.assistant_message.text, "Hello there.")
                self.assertEqual(provider.calls, [])
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                types = [event.event_type for event in events]
                self.assertIn("conversation.turn_started", types)
                self.assertIn("conversation.text_completed", types)
                self.assertNotIn("completion.claim_validated", types)
                session_events = await application.registry.require(
                    RuntimeStorePort
                ).read_session_events(task.session_id)
                result_event = next(
                    event for event in session_events
                    if event.event_type == "session.task_result_recorded"
                )
                self.assertIs(result_event.payload["assistant_replay_eligible"], True)
                self.assertNotIn("turn.incomplete_boundary", types)
                self.assertNotIn("tool.protocol_rejected", types)
        finally:
            await application.registry.stop_all()

    async def test_follow_up_sees_prior_visible_assistant_reply(self):
        """A normal conversational follow-up retains its antecedent.

        Completion review is disabled in this fixture.  That must not erase a
        reply the user has already seen from the next model request: a reply
        such as "yes, implement it" otherwise has no usable referent.
        """
        model = ScriptedModel([
            text_response("proposal", "I found two missing dashboard views; I can implement them."),
            text_response("implementation", "I implemented the dashboard views."),
        ])
        application, _ = await self._application(model)
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                await application.kernel.run_agent_turn(
                    task.task_id, "inspect the dashboard"
                )
                await application.kernel.run_agent_turn(
                    task.task_id, "yes, implement it"
                )
                session_message = next(
                    message for message in model.requests[1].messages
                    if message.message_id.startswith("session-context-")
                )
                from tests.history_helpers import session_history_view
                history = session_history_view(model.requests[1])["recent_messages"]
                self.assertIn(
                    "I found two missing dashboard views; I can implement them.",
                    [item["text"] for item in history
                     if item["role"] == "assistant"],
                )
        finally:
            await application.registry.stop_all()

    async def test_tool_result_is_fed_back_before_the_text_answer(self):
        model = ScriptedModel([
            tool_response("ask", "call-search", "web.search", {"query": "nba"}),
            text_response("answer", "Here is the summary."),
        ])
        application, provider = await self._application(model)
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                result = await application.kernel.run_agent_turn(task.task_id, "nba")
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(provider.calls, ["web.search"])
                # The second sample saw the tool-role result, not a completion gap.
                second = model.requests[1].messages
                tool_messages = [
                    message for message in second
                    if message.role is MessageRole.TOOL
                ]
                self.assertEqual(len(tool_messages), 1)
                results = [
                    block.result for block in tool_messages[0].content
                    if isinstance(block, ToolResultBlock)
                ]
                self.assertTrue(results[0].ok)
                self.assertNotIn(
                    "completion_continuation",
                    "\n".join(message.text for message in second),
                )
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                types = [event.event_type for event in events]
                self.assertIn("conversation.tool_result_recorded", types)
                self.assertIn("conversation.text_completed", types)
        finally:
            await application.registry.stop_all()

    async def test_multiple_independent_tool_calls_pair_one_to_one(self):
        search = ToolCall("call-search", "web.search", {"query": "nba"})
        open_call = ToolCall("call-open", "web.open", {"url": "https://news.example/nba"})
        model = ScriptedModel([
            ModelResponse(
                Message(
                    "batch", MessageRole.ASSISTANT,
                    (ToolCallBlock(search), ToolCallBlock(open_call)),
                ),
                FinishReason.TOOL_CALL, ModelUsage(8, 4),
            ),
            text_response("answer", "Both results arrived."),
        ])
        application, provider = await self._application(model)
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                result = await application.kernel.run_agent_turn(
                    task.task_id, "look it up"
                )
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(provider.calls, ["web.search", "web.open"])
                second = model.requests[1].messages
                tool_ids = [
                    block.result.call_id
                    for message in second if message.role is MessageRole.TOOL
                    for block in message.content
                    if isinstance(block, ToolResultBlock)
                ]
                self.assertEqual(sorted(tool_ids), ["call-open", "call-search"])
        finally:
            await application.registry.stop_all()

    async def test_text_completion_retires_the_checkpoint_for_restart_safety(self):
        model = ScriptedModel([text_response("done", "All set.")])
        application, provider = await self._application(model)
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                await application.kernel.run_agent_turn(task.task_id, "hi")
                stored = await application.kernel.get_task(task.task_id)
                # A finished text turn must not leave a live checkpoint that a
                # process restart could later mislabel as an orphaned run.
                self.assertIsNone(stored.active_agent_checkpoint)
        finally:
            await application.registry.stop_all()

    async def test_trust_refresh_rebinds_checkpoint_before_approval_resume(self):
        model = ScriptedModel([
            tool_response("build", "call-build", "core.run_command", {
                "argv": ["npm", "run", "build"], "cwd": ".",
            }),
            text_response("blocked", "Build was not approved."),
        ])
        application = compose_fixture_application(
            model_adapter=model, tool_adapters=(CoreProcessToolProvider(),),
        )
        await application.registry.start_all()
        try:
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                identity = workspace / "package.json"
                identity.write_text('{"name":"before"}\n', encoding="utf-8")
                task = await _executing(application, workspace, "task-trust-refresh")
                # The task was created under the old fingerprint. This is a
                # benign pre-command change, not a change made while approval
                # is pending, so the refreshed binding must travel with the
                # suspended checkpoint.
                identity.write_text('{"name":"after"}\n', encoding="utf-8")
                suspended = await application.kernel.run_agent_turn(
                    task.task_id, "build the project",
                )

                self.assertIsInstance(suspended, AgentTurnSuspended)
                assert isinstance(suspended, AgentTurnSuspended)
                awaiting = await application.kernel.get_task(task.task_id)
                checkpoint = awaiting.active_agent_checkpoint
                assert checkpoint is not None
                self.assertEqual(
                    checkpoint["workspace_fingerprint"],
                    awaiting.project_fingerprint,
                )
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                self.assertIn(
                    "checkpoint.workspace_binding_refreshed",
                    [event.event_type for event in events],
                )

                resumed = await application.kernel.resolve_agent_approval(
                    suspended.approval_request_id, ApprovalDecision.DENY,
                    "do not run this build",
                )
                self.assertIsInstance(resumed, AgentTurnResult)
                restored = await application.kernel.get_task(task.task_id)
                self.assertIsNot(restored.state, TaskState.CONFLICT)
        finally:
            await application.registry.stop_all()

    async def test_unknown_tool_comes_back_as_a_tool_role_error(self):
        model = ScriptedModel([
            tool_response("bad", "call-bad", "does.not.exist", {}),
            text_response("fixed", "I used the advertised tools instead."),
        ])
        application, provider = await self._application(model)
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                result = await application.kernel.run_agent_turn(task.task_id, "go")
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(provider.calls, [])
                second = model.requests[1].messages
                tool_messages = [
                    message for message in second
                    if message.role is MessageRole.TOOL
                ]
                self.assertEqual(len(tool_messages), 1)
                failures = [
                    block.result for block in tool_messages[0].content
                    if isinstance(block, ToolResultBlock)
                ]
                self.assertFalse(failures[0].ok)
                self.assertEqual(failures[0].error_code, "INVALID_ARGUMENT")
                self.assertTrue(failures[0].retryable)
                self.assertEqual(
                    failures[0].effective_recovery_kind,
                    ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS,
                )
                self.assertEqual(failures[0].recovery_action, {
                    "tool": "does.not.exist",
                    "reason": "arguments_do_not_match_advertised_schema",
                })
                self.assertIn("continue the original user request", failures[0].hint)
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                self.assertIn(
                    "tool.protocol_rejected",
                    [event.event_type for event in events],
                )
        finally:
            await application.registry.stop_all()

    async def test_failed_tool_is_visible_and_a_blocker_answer_is_allowed(self):
        model = ScriptedModel([
            tool_response("fail", "call-fail", "fixture.fail", {"text": "x"}),
            text_response(
                "blocker",
                "I could not retrieve it; the tool failed and nothing else was available.",
            ),
        ])
        application, provider = await self._application(model)
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                result = await application.kernel.run_agent_turn(task.task_id, "try")
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(provider.calls, ["fixture.fail"])
                second = model.requests[1].messages
                rendered = "\n".join(
                    message.text for message in second
                    if message.role is MessageRole.SYSTEM
                )
                self.assertIn("current_execution_state include a failure", rendered)
                self.assertNotIn("recovery_kind", rendered)
                self.assertNotIn("call the same tool again", rendered)
                self.assertNotIn(
                    "EXTERNAL_READ_MISSING",
                    "\n".join(message.text for message in second),
                )
        finally:
            await application.registry.stop_all()

    async def test_model_switches_tools_after_a_failure_without_declaring_intent(self):
        model = ScriptedModel([
            tool_response("fail", "call-fail", "fixture.fail", {"text": "x"}),
            tool_response("retry", "call-retry", "web.search", {"query": "fallback"}),
            text_response("done", "The second tool worked."),
        ])
        application, provider = await self._application(model)
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                result = await application.kernel.run_agent_turn(task.task_id, "try")
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(provider.calls, ["fixture.fail", "web.search"])
                # The Runtime never demanded an intent declaration between the
                # failed call and the replacement.
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                self.assertNotIn(
                    "tool.protocol_rejected",
                    [event.event_type for event in events],
                )
        finally:
            await application.registry.stop_all()

    async def test_exhausted_iteration_budget_is_a_partial_boundary(self):
        model = ScriptedModel([
            tool_response("one", "call-1", "web.search", {"query": "a"}),
            tool_response("two", "call-2", "web.search", {"query": "b"}),
        ])
        application, provider = await self._application(model)
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                result = await application.kernel.run_agent_turn(
                    task.task_id, "keep going", max_model_calls=2,
                )
                self.assertIsInstance(result, AgentTurnIncomplete)
                self.assertEqual(result.reason, "iteration_budget_exhausted")
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                types = [event.event_type for event in events]
                self.assertIn("conversation.partial_boundary", types)
                self.assertIn("turn.incomplete_boundary", types)
                self.assertNotIn("conversation.text_completed", types)
        finally:
            await application.registry.stop_all()

    async def test_nba_news_regression_completes_without_a_completion_gate(self):
        """The observed failure: web.open succeeds, then a plain answer is blocked."""

        model = ScriptedModel([
            tool_response("search", "call-search", "web.search", {
                "query": "today NBA news",
            }),
            tool_response("open", "call-open", "web.open", {
                "url": "https://news.example/nba",
            }),
            text_response(
                "summary",
                "Today the Lakers beat the Celtics 112-108.",
            ),
        ])
        application, provider = await self._application(model)
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                result = await application.kernel.run_agent_turn(
                    task.task_id, "find today's NBA news"
                )
                self.assertIsInstance(result, AgentTurnResult)
                self.assertEqual(
                    result.assistant_message.text,
                    "Today the Lakers beat the Celtics 112-108.",
                )
                self.assertEqual(provider.calls, ["web.search", "web.open"])
                self.assertEqual(len(model.requests), 3)
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                types = [event.event_type for event in events]
                self.assertIn("conversation.text_completed", types)
                for forbidden in (
                    "completion.claim_validated",
                    "completion.shadow_validated",
                    "turn.incomplete_boundary",
                ):
                    self.assertNotIn(forbidden, types)
                blob = "\n".join(message.text for message in result.messages)
                self.assertNotIn("EXTERNAL_READ_MISSING", blob)
                self.assertNotIn("completion_continuation", blob)
                self.assertNotIn("cor.turn_intent", blob)
                self.assertNotIn("core.turn_intent", blob)
        finally:
            await application.registry.stop_all()

    async def test_recoverable_protocol_error_still_respects_the_model_budget(self):
        model = ScriptedModel([
            tool_response("bad-1", "call-bad-1", "does.not.exist", {}),
            tool_response("bad-2", "call-bad-2", "does.not.exist", {}),
        ])
        application, provider = await self._application(model)
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = await _executing(application, Path(directory))
                result = await application.kernel.run_agent_turn(
                    task.task_id, "go", max_model_calls=2,
                )
                self.assertIsInstance(result, AgentTurnIncomplete)
        finally:
            await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
