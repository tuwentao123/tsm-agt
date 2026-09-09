from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    ApprovalDecision, ApprovalKind, ApprovalRequired, TaskState,
)
from tsm_agt.ports import (
    EvidenceQuestion, FinishReason, Message, MessageRole, ModelRequest,
    ModelResponse, ModelUsage, ProviderCapabilities, TextBlock, ToolCall,
    ToolCallBlock, ToolResultBlock,
)


class CatalogFollowUpModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        result = next((
            block.result for message in reversed(request.messages)
            for block in message.content if isinstance(block, ToolResultBlock)
        ), None)
        if result is not None:
            return ModelResponse(
                Message(
                    "follow-up-final", MessageRole.ASSISTANT,
                    (TextBlock("continued from approved historical location"),),
                ),
                FinishReason.STOP, ModelUsage(1, 1),
            )
        context = next(
            json.loads(message.text) for message in request.messages
            if message.message_id.startswith("session-context-")
        )
        resources = context["historical_investigation"]["resources"]
        external_root = next(
            item for item in reversed(resources)
            if item["root_kind"] == "TASK_APPROVED_READ_ROOT"
            and item["resource_kind"] == "ROOT"
        )
        return ModelResponse(
            Message(
                "follow-up-tool", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "follow-up-search", "core.find_files",
                    {
                        "path": external_root["canonical_path"],
                        "pattern": "service.py",
                    },
                    EvidenceQuestion(
                        "Q-follow-up",
                        "Where does the historical service continue?",
                    ),
                )),),
            ),
            FinishReason.TOOL_CALL, ModelUsage(1, 1),
        )


class SessionResourceCatalogTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _executing_task(app, root: Path, task_id: str, session_id: str):
        task = await app.kernel.create_task(
            "inspect related service", root, task_id, session_id=session_id
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await app.kernel.transition_task(task.task_id, state, state.value)
        return task

    async def test_next_task_receives_location_not_authority_and_reapproves(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        workspace = root / "workspace"
        external = root / "related-service"
        workspace.mkdir()
        external.mkdir()
        target = external / "service.py"
        target.write_text("class RelatedService: pass\n", encoding="utf-8")
        database = root / "runtime.db"
        first_model = CatalogFollowUpModel()
        first_app = compose_fixture_application(
            model_adapter=first_model,
            tool_adapters=(CoreReadOnlyToolProvider(),),
            store_adapter=SQLiteRuntimeStore(database),
            require_evidence_questions=True,
        )
        await first_app.registry.start_all()
        try:
            session = await first_app.kernel.create_session(
                "catalog", "session-resource-catalog"
            )
            first = await self._executing_task(
                first_app, workspace, "task-catalog-first", session.session_id
            )
            call = ToolCall(
                "find-first", "core.find_files",
                {"path": str(external), "pattern": "service.py"},
                EvidenceQuestion("Q1", "Where is RelatedService defined?"),
            )
            # Bind the lifecycle exactly as an Agent Tool Call does, then use the
            # public Tool protocol to prove the approval and result ledger.
            await first_app.kernel._bind_evidence_question(
                first.task_id, "turn-first", call
            )
            with self.assertRaises(ApprovalRequired) as first_approval:
                await first_app.kernel.invoke_tool(
                    first.task_id, "turn-first", call
                )
            found = await first_app.kernel.resolve_approval(
                first.task_id, first_approval.exception.request.request_id,
                first_approval.exception.request.payload_hash,
                ApprovalDecision.APPROVE, "allow first Task read",
            )
            self.assertTrue(found.ok)
            inventory, delta = await first_app.kernel._evaluate_evidence_delta(
                first.task_id, "turn-first", call, found, {}
            )
            self.assertIsInstance(inventory, dict)
            await first_app.kernel._observe_evidence_question(
                first.task_id, "turn-first", call, found, delta
            )
            await first_app.kernel._record_session_task_result(
                first.task_id, "turn-first",
                Message("user-first", MessageRole.USER, (TextBlock("inspect"),)),
                Message("assistant-first", MessageRole.ASSISTANT,
                        (TextBlock("found service.py"),)),
            )

            projection = await first_app.kernel.get_session_conversation(
                session.session_id
            )
            external_resources = [
                item for item in projection.resource_catalog
                if item.resolved_root == str(external.resolve())
            ]
            self.assertTrue(external_resources)
            self.assertEqual(
                [item.resource_kind.value for item in external_resources],
                ["ROOT", "ARTIFACT"],
            )
            before_restart = projection.to_data()
            serialized = json.dumps(projection.to_data(), ensure_ascii=False)
            self.assertNotIn('"resource_ref"', serialized)
            self.assertNotIn("workspace_access_grants", serialized)
            self.assertNotIn('"approval"', serialized.casefold())
            self.assertNotIn("allow first Task read", serialized)
            self.assertNotIn("class RelatedService: pass", serialized)
            self.assertNotIn("process_id", serialized)
            self.assertNotIn("pid", serialized)
        finally:
            await first_app.registry.stop_all()

        # A fresh Application must rebuild exactly the same catalog from SQLite.
        # This also proves the following Task does not rely on in-memory grants.
        model = CatalogFollowUpModel()
        app = compose_fixture_application(
            model_adapter=model,
            tool_adapters=(CoreReadOnlyToolProvider(),),
            store_adapter=SQLiteRuntimeStore(database),
            require_evidence_questions=True,
        )
        await app.registry.start_all()
        try:
            rebuilt = await app.kernel.get_session_conversation(session.session_id)
            self.assertEqual(rebuilt.to_data(), before_restart)
            unrelated = await app.kernel.create_session(
                "unrelated", "session-unrelated-catalog"
            )
            unrelated_projection = await app.kernel.get_session_conversation(
                unrelated.session_id
            )
            self.assertEqual(unrelated_projection.resource_catalog, ())
            self.assertEqual(unrelated_projection.question_catalog, ())

            second = await self._executing_task(
                app, workspace, "task-catalog-second", session.session_id
            )
            suspended = await app.kernel.run_agent_turn(
                second.task_id, "continue investigating"
            )
            self.assertEqual(suspended.approval_kind, ApprovalKind.WORKSPACE_READ.value)
            self.assertEqual(suspended.target, str(external.resolve()))
            restored = await app.kernel.get_task(second.task_id)
            self.assertEqual(restored.workspace_access_grants, ())
            context_message = next(
                message for message in model.requests[-1].messages
                if message.message_id.startswith("session-context-")
            )
            context = json.loads(context_message.text)
            recent = next(
                item for item in context["recent_task_summaries"]
                if item["task_id"] == first.task_id
            )
            execution = next(
                item for item in recent["execution_events"]
                if item["call"]["tool"] == "core.find_files"
            )
            self.assertEqual(
                execution["call"]["arguments"]["pattern"], "service.py"
            )
            self.assertEqual(execution["result"]["state"], "COMMITTED")
            self.assertTrue(execution["result"]["ok"])
            self.assertEqual(
                execution["result"]["observation"]["matches"][0][
                    "resolved_path"
                ],
                str(target.resolve()),
            )
            self.assertNotIn("important_actions", recent)
            self.assertIn("not a Tool resource_ref",
                          context["historical_investigation"]["instruction"])
        finally:
            await app.registry.stop_all()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
