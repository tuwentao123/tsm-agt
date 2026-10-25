import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tsm_agt.adapters.builtin import RuntimeInspectionToolProvider
from tsm_agt.adapters.fixture import EchoModelProvider, InMemoryRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application, compose_readonly_application
from tsm_agt.core import SessionSnapshot, TaskSnapshot, TaskState
from tsm_agt.ports import (
    AdapterContext,
    FinishReason,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    RuntimeEvent,
    RuntimeStorePort,
    RuntimeUnitOfWork,
    SessionEvent,
    SessionUnitOfWork,
    TextBlock,
    ToolCall,
    ToolCallBlock,
    ToolEffect,
    ToolInvocationContext,
    ToolResultAuthority,
)


class RuntimeInspectionModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=16384)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        has_result = any(
            block.__class__.__name__ == "ToolResultBlock"
            for message in request.messages for block in message.content
        )
        if has_result:
            return ModelResponse(
                Message("assistant-final", MessageRole.ASSISTANT, (TextBlock("done"),)),
                FinishReason.STOP,
                ModelUsage(input_tokens=1, output_tokens=1),
            )
        return ModelResponse(
            Message(
                "assistant-tool",
                MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall("inspect-current", "runtime.inspect_task")),),
            ),
            FinishReason.TOOL_CALL,
            ModelUsage(input_tokens=1, output_tokens=1),
        )


class RuntimeInspectionToolProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.store = InMemoryRuntimeStore()
        self.provider = RuntimeInspectionToolProvider(self.store)
        adapter_context = AdapterContext(config={}, emit_event=lambda *_: None)
        await self.store.start(adapter_context)
        await self.provider.start(adapter_context)
        await self._seed_current_scope()
        self.context = ToolInvocationContext(
            invocation_id="inv-current",
            task_id="task-current",
            turn_id="turn-current",
            workspace=self.workspace,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
        )

    async def asyncTearDown(self) -> None:
        await self.provider.stop(datetime.now(timezone.utc))
        await self.store.stop(datetime.now(timezone.utc))
        self.temp_dir.cleanup()

    async def _seed_current_scope(self) -> None:
        session = SessionSnapshot.create("session-current", "user-current", "private")
        session = session.attach_task("task-current")
        await self.store.commit_session(SessionUnitOfWork(
            "session-current",
            0,
            session.to_data(),
            (SessionEvent("session-created", "session-current", 1, "session.created", {
                "subject": "user-current", "title": "private prompt body",
            }),),
        ))
        task = TaskSnapshot.create(
            "task-current", "do not reveal this user prompt", str(self.workspace),
            session_id="session-current",
        )
        await self.store.commit(RuntimeUnitOfWork(
            "task-current",
            0,
            task.to_data(),
            (
                RuntimeEvent("task-created", "task-current", 1, "task.created", {
                    "goal": "do not reveal this user prompt",
                }),
                RuntimeEvent("llm-completed", "task-current", 2, "llm.completed", {
                    "message": {"content": [{"type": "text", "text": "assistant secret"}]},
                    "finish_reason": "stop",
                    "prompt": "prompt body secret",
                    "environment": {"TOKEN": "environment secret"},
                }),
                RuntimeEvent("tool-completed", "task-current", 3, "tool.completed", {
                    "tool": "core.read_file",
                    "arguments": {"path": "secret.txt"},
                    "result": {"content": "raw file content secret"},
                }),
            ),
        ))

    async def _seed_foreign_scope(self) -> None:
        foreign_workspace = self.workspace / "foreign"
        foreign_workspace.mkdir()
        session = SessionSnapshot.create("session-foreign", "user-foreign", "foreign")
        session = session.attach_task("task-foreign")
        await self.store.commit_session(SessionUnitOfWork(
            "session-foreign", 0, session.to_data(),
            (SessionEvent("foreign-session-created", "session-foreign", 1, "session.created"),),
        ))
        task = TaskSnapshot.create(
            "task-foreign", "foreign prompt", str(foreign_workspace),
            session_id="session-foreign",
        )
        await self.store.commit(RuntimeUnitOfWork(
            "task-foreign", 0, task.to_data(),
            (RuntimeEvent("foreign-task-created", "task-foreign", 1, "task.created"),),
        ))

    async def test_task_and_session_inspection_return_allowlisted_metadata(self) -> None:
        task_result = await self.provider.invoke(
            ToolCall("inspect-task", "runtime.inspect_task"), self.context,
        )
        session_result = await self.provider.invoke(
            ToolCall("inspect-session", "runtime.inspect_session"), self.context,
        )

        self.assertTrue(task_result.ok)
        self.assertEqual(task_result.data["task_id"], "task-current")
        self.assertEqual(task_result.data["state"], TaskState.CREATED.value)
        self.assertNotIn("goal", task_result.data)
        self.assertTrue(session_result.ok)
        self.assertEqual(session_result.data["session_id"], "session-current")
        self.assertEqual(session_result.data["task_ids"], ["task-current"])
        self.assertNotIn("subject", session_result.data)
        self.assertNotIn("title", session_result.data)

    async def test_list_events_paginates_and_redacts_payloads(self) -> None:
        first = await self.provider.invoke(
            ToolCall("events-first", "runtime.list_task_events", {"limit": 2}),
            self.context,
        )
        second = await self.provider.invoke(
            ToolCall(
                "events-second", "runtime.list_task_events",
                {"cursor": first.data["next_cursor"], "limit": 2},
            ),
            self.context,
        )

        self.assertTrue(first.ok)
        self.assertEqual([item["sequence"] for item in first.data["data"]], [1, 2])
        self.assertEqual(first.data["next_cursor"], 2)
        llm = first.data["data"][1]
        self.assertEqual(llm["summary"]["finish_reason"], "stop")
        self.assertIn("message_hash", llm["summary"])
        self.assertGreater(llm["summary"]["message_length"], 0)
        self.assertTrue(second.ok)
        self.assertEqual([item["sequence"] for item in second.data["data"]], [3])
        self.assertIsNone(second.data["next_cursor"])
        rendered = str(first.data) + str(second.data)
        for secret in (
            "assistant secret",
            "prompt body secret",
            "environment secret",
            "raw file content secret",
            "secret.txt",
        ):
            self.assertNotIn(secret, rendered)

    async def test_search_history_uses_metadata_only_and_paginates(self) -> None:
        first = await self.provider.invoke(
            ToolCall(
                "search-first", "runtime.search_history",
                {"query": "completed", "limit": 1},
            ),
            self.context,
        )
        second = await self.provider.invoke(
            ToolCall(
                "search-second", "runtime.search_history",
                {"query": "completed", "cursor": first.data["next_cursor"], "limit": 1},
            ),
            self.context,
        )

        self.assertTrue(first.ok)
        self.assertEqual(len(first.data["data"]), 1)
        self.assertEqual(first.data["data"][0]["event_type"], "llm.completed")
        self.assertEqual(first.data["next_cursor"], 1)
        self.assertTrue(second.ok)
        self.assertEqual(second.data["data"][0]["event_type"], "tool.completed")
        self.assertIsNone(second.data["next_cursor"])
        self.assertNotIn("assistant secret", str(first.data) + str(second.data))

    async def test_cross_session_and_workspace_targets_are_denied_auditably(self) -> None:
        await self._seed_foreign_scope()
        cross_task = await self.provider.invoke(
            ToolCall(
                "cross-task", "runtime.inspect_task", {"task_id": "task-foreign"},
            ),
            self.context,
        )
        cross_session = await self.provider.invoke(
            ToolCall(
                "cross-session", "runtime.inspect_session",
                {"session_id": "session-foreign"},
            ),
            self.context,
        )
        cross_workspace = await self.provider.invoke(
            ToolCall(
                "cross-workspace", "runtime.search_history",
                {"query": "created", "workspace": str(self.workspace / "foreign")},
            ),
            self.context,
        )

        for result in (cross_task, cross_session, cross_workspace):
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "PERMISSION_DENIED")
            self.assertEqual(result.data["audit"]["decision"], "denied")
            self.assertIn("confirmation", result.data["audit"]["reason"])

    async def test_specs_are_runtime_observations_and_readonly_composition_registers_them(self) -> None:
        specs = {spec.name: spec for spec in await self.provider.list_tools()}
        self.assertEqual(set(specs), {
            "runtime.inspect_task",
            "runtime.list_task_events",
            "runtime.inspect_session",
            "runtime.search_history",
        })
        for spec in specs.values():
            self.assertEqual(spec.effect, ToolEffect.OBSERVE)
            self.assertEqual(spec.result_authority, ToolResultAuthority.RUNTIME_FACT)

        app = compose_readonly_application()
        await app.registry.start_all()
        try:
            names = {spec.name for spec in await app.kernel.list_tools()}
        finally:
            await app.registry.stop_all()
        self.assertTrue(set(specs).issubset(names))

    async def test_result_is_durably_recorded_when_used_by_the_agent(self) -> None:
        store = InMemoryRuntimeStore()
        provider = RuntimeInspectionToolProvider(store)
        app = compose_fixture_application(
            model_adapter=RuntimeInspectionModel(),
            tool_adapters=(provider,),
            store_adapter=store,
        )
        await app.registry.start_all()
        try:
            task = await app.kernel.create_task("inspect runtime", self.workspace, "task-agent")
            for state in (
                TaskState.INTAKE,
                TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS,
                TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await app.kernel.transition_task(task.task_id, state, state.value)
            await app.kernel.run_agent_turn(task.task_id, "inspect")
            events = await app.registry.require(RuntimeStorePort).read_events(task.task_id)
        finally:
            await app.registry.stop_all()

        completed = [event for event in events if event.event_type == "tool.completed"]
        self.assertEqual(len(completed), 1)
        result_data = completed[0].payload["result"]["data"]
        self.assertEqual(result_data["task_id"], task.task_id)


if __name__ == "__main__":
    unittest.main()
