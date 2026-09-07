from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.rule_based_progressive_scope import (
    RuleBasedProgressiveScopePolicy,
)
from tsm_agt.adapters.rule_based_semantic_action import (
    RuleBasedSemanticActionClassifier,
)
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskState
from tsm_agt.ports import (
    AdapterDescriptor, EvidenceQuestion, FinishReason, HealthState, HealthStatus,
    Message, MessageRole, ModelRequest, ModelResponse, ProgressiveScopeAction,
    ProgressiveScopePolicyPort, ProgressiveScopeState, ProviderCapabilities,
    RuntimeStorePort, ScopeProbe, TextBlock, ToolCall, ToolCallBlock,
    ToolIdempotency, ToolResult, ToolResultBlock, ToolRisk, ToolSpec,
)


def search_call(
    call_id: str, path: str, *, question_id: str = "Q-scope",
    expansion_reason: str = "",
) -> ToolCall:
    return ToolCall(
        call_id, "core.search_text", {"query": "Target", "path": path},
        EvidenceQuestion(
            question_id, "Where is Target defined?", expansion_reason
        ),
    )


class RuleBasedProgressiveScopePolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.policy = RuleBasedProgressiveScopePolicy()
        self.classifier = RuleBasedSemanticActionClassifier()
        await self.policy.start(None)
        await self.classifier.start(None)

    async def asyncTearDown(self) -> None:
        await self.policy.stop(datetime.now())
        await self.classifier.stop(datetime.now())

    async def _record(
        self, call: ToolCall, probe: ScopeProbe, state=ProgressiveScopeState(),
        *, empty: bool = False,
    ):
        action = await self.classifier.classify(call)
        result = ToolResult(
            call.call_id, True,
            {"matches": [] if empty else [{"path": "src/a.py"}]},
        )
        return (await self.policy.after_result(
            call, result, action, probe, state
        )).state

    async def test_narrowing_allowed_expansion_requires_reason(self):
        workspace = ScopeProbe("workspace", (), 0)
        module = ScopeProbe("module", ("workspace",), 1)
        first = search_call("first", ".")
        state = await self._record(first, workspace)
        narrowed = search_call("narrowed", "src")
        action = await self.classifier.classify(narrowed)
        decision = await self.policy.before_call(narrowed, action, module, state)
        self.assertEqual(decision.action, ProgressiveScopeAction.ALLOW)
        self.assertEqual(decision.relation, "narrowed")
        state = await self._record(narrowed, module, state)

        expanded = search_call("expanded", ".")
        action = await self.classifier.classify(expanded)
        blocked = await self.policy.before_call(expanded, action, workspace, state)
        self.assertEqual(
            blocked.action, ProgressiveScopeAction.REQUIRE_EXPANSION_REASON
        )
        justified = search_call(
            "justified", ".", expansion_reason="Module results were incomplete"
        )
        allowed = await self.policy.before_call(
            justified, await self.classifier.classify(justified), workspace, state
        )
        self.assertEqual(allowed.action, ProgressiveScopeAction.ALLOW)
        self.assertEqual(allowed.reason, "expanded_with_reason")
        self.assertTrue(allowed.expansion_reason_hash)

    async def test_zero_results_and_new_question_allow_expansion(self):
        module = ScopeProbe("module", ("workspace",), 1)
        workspace = ScopeProbe("workspace", (), 0)
        call = search_call("empty", "src")
        state = await self._record(call, module, empty=True)
        expanded = search_call("expanded", ".")
        decision = await self.policy.before_call(
            expanded, await self.classifier.classify(expanded), workspace, state
        )
        self.assertEqual(decision.reason, "expanded_after_zero_results")

        other = search_call("other", ".", question_id="Q-other")
        different = await self.policy.before_call(
            other, await self.classifier.classify(other), workspace, state
        )
        self.assertEqual(different.reason, "first_scope")

    async def test_new_question_cannot_leave_focused_module_without_reason(self):
        workspace = ScopeProbe("workspace", (), 0)
        source = ToolCall(
            "module-source", "core.read_file", {"path": "src/Target.kt"},
            EvidenceQuestion("Q-source", "What does Target implement?"),
        )
        source_action = await self.classifier.classify(source)
        state = (await self.policy.after_result(
            source, ToolResult(source.call_id, True, {"path": "src/Target.kt"}),
            source_action, ScopeProbe("file", ("workspace", "module"), 2),
            ProgressiveScopeState(),
        )).state
        changed_query = search_call(
            "changed-query", ".", question_id="Q-another"
        )
        decision = await self.policy.before_call(
            changed_query, await self.classifier.classify(changed_query),
            workspace, state,
        )
        self.assertEqual(
            decision.action, ProgressiveScopeAction.REQUIRE_EXPANSION_REASON
        )
        self.assertEqual(decision.reason, "expanded_without_reason")

    async def test_second_workspace_search_requires_narrowing_or_reason(self):
        workspace = ScopeProbe("workspace", (), 0)
        first = search_call("workspace-first", ".")
        state = await self._record(first, workspace)
        second = search_call(
            "workspace-second", ".", question_id="Q-second"
        )
        decision = await self.policy.before_call(
            second, await self.classifier.classify(second), workspace, state
        )
        self.assertEqual(
            decision.action, ProgressiveScopeAction.REQUIRE_EXPANSION_REASON
        )
        self.assertEqual(decision.reason, "broad_hits_require_narrowing")

    async def test_reading_document_does_not_lock_in_document_directory(self):
        document = ToolCall(
            "read-doc", "core.read_file", {"path": "docs/guide.md"},
            EvidenceQuestion("Q-doc", "What source path does the guide mention?"),
        )
        action = await self.classifier.classify(document)
        update = await self.policy.after_result(
            document, ToolResult(document.call_id, True, {"path": "docs/guide.md"}),
            action, ScopeProbe("doc-file", ("workspace", "docs"), 2),
            ProgressiveScopeState(),
        )
        self.assertFalse(update.state.focused_scope_hash)

    async def test_reading_source_locks_search_to_source_directory(self):
        source = ToolCall(
            "read-source", "core.read_file", {"path": "src/app/Target.kt"},
            EvidenceQuestion("Q-source", "What does Target implement?"),
        )
        action = await self.classifier.classify(source)
        update = await self.policy.after_result(
            source, ToolResult(source.call_id, True, {"path": "src/app/Target.kt"}),
            action, ScopeProbe("file", ("workspace", "src", "app"), 3),
            ProgressiveScopeState(),
        )
        self.assertEqual(update.state.focused_scope_hash, "app")
        outside = search_call("outside", ".", question_id="Q-outside")
        decision = await self.policy.before_call(
            outside, await self.classifier.classify(outside),
            ScopeProbe("workspace", (), 0), update.state,
        )
        self.assertEqual(
            decision.action, ProgressiveScopeAction.REQUIRE_EXPANSION_REASON
        )


class ScopeSearchTool:
    descriptor = AdapterDescriptor(
        "fixture.scope-search", "1", "ToolProviderPort", "1"
    )

    def __init__(self) -> None:
        self.calls = 0

    async def start(self, context): pass
    async def stop(self, deadline): pass
    async def health(self): return HealthStatus(HealthState.HEALTHY)
    async def list_tools(self):
        return (ToolSpec(
            "core.search_text", "Search fixture",
            {"type": "object", "properties": {
                "query": {"type": "string"}, "path": {"type": "string"},
            }, "required": ["query"], "additionalProperties": False},
            ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
        ),)

    async def invoke(self, call, context):
        self.calls += 1
        return ToolResult(call.call_id, True, {
            "matches": [{"path": "src/module/a.py", "line": 1}]
        })


class ScopeSequenceModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result for message in request.messages for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        if not results:
            call = search_call("scope-workspace", ".")
        elif len(results) == 1:
            call = search_call("scope-module", "src/module")
        elif len(results) == 2:
            call = search_call("scope-expanded-blocked", ".")
        elif results[-1].error_code == "SCOPE_EXPANSION_REASON_REQUIRED":
            call = search_call(
                "scope-expanded-allowed", ".",
                expansion_reason="The module result did not include external callers",
            )
        else:
            return ModelResponse(Message(
                "scope-final", MessageRole.ASSISTANT, (TextBlock("scope guarded"),)
            ))
        return ModelResponse(Message(
            call.call_id, MessageRole.ASSISTANT, (ToolCallBlock(call),)
        ), FinishReason.TOOL_CALL)


class ProgressiveScopeKernelTest(unittest.IsolatedAsyncioTestCase):
    async def test_kernel_blocks_unjustified_expansion_then_allows_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src/module").mkdir(parents=True)
            database = root / "runtime.db"
            tool = ScopeSearchTool()
            app = compose_fixture_application(
                model_adapter=ScopeSequenceModel(), tool_adapters=(tool,),
                store_adapter=SQLiteRuntimeStore(database),
                require_evidence_questions=True,
                semantic_action_classifier_adapter=RuleBasedSemanticActionClassifier(),
                progressive_scope_policy_adapter=RuleBasedProgressiveScopePolicy(),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "scope test", root, "task-progressive-scope"
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                result = await app.kernel.run_agent_turn(task.task_id, "scope test")
                self.assertEqual(result.assistant_message.text, "scope guarded")
                self.assertEqual(tool.calls, 3)
                self.assertEqual(result.tool_calls, 3)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                self.assertEqual(sum(
                    event.event_type == "progressive_scope.expansion_reason_required"
                    for event in events
                ), 1)
                reasons = [
                    event.payload["reason"] for event in events
                    if event.event_type == "progressive_scope.action_evaluated"
                ]
                self.assertEqual(
                    reasons, ["first_scope", "narrowed",
                              "expanded_without_reason", "expanded_with_reason"]
                )
                payloads = str([
                    event.payload for event in events
                    if event.event_type.startswith("progressive_scope.")
                ])
                self.assertNotIn("src/module", payloads)
                self.assertNotIn("external callers", payloads)
            finally:
                await app.registry.stop_all()

            store = SQLiteRuntimeStore(database)
            await store.start(None)
            try:
                events = await store.read_events("task-progressive-scope")
                self.assertTrue(any(
                    event.event_type == "progressive_scope.state_changed"
                    for event in events
                ))
            finally:
                await store.stop(datetime.now())


class ProgressiveScopeCompositionTest(unittest.TestCase):
    def test_fixture_policy_is_optional_and_replaceable(self):
        without = compose_fixture_application()
        self.assertEqual(without.registry.all(ProgressiveScopePolicyPort), ())
        policy = RuleBasedProgressiveScopePolicy()
        app = compose_fixture_application(progressive_scope_policy_adapter=policy)
        self.assertIs(app.registry.require(ProgressiveScopePolicyPort), policy)


if __name__ == "__main__":
    unittest.main()
