from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    ONBOARDING_PHASES, OnboardingCheckpoint, ProjectOnboardingSnapshot,
    ProjectTrustLevel, TaskState,
)
from tsm_agt.ports import MessageRole, RuntimeStorePort


class ProjectOnboardingTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _project(root: Path) -> None:
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "package.json").write_text(
            '{"scripts":{"build":"tsc","test":"vitest","deploy":"no"}}',
            encoding="utf-8",
        )
        (root / "src" / "index.ts").write_text("export const x = 1;", encoding="utf-8")
        (root / "tests" / "app.ts").write_text("// test", encoding="utf-8")
        (root / "AGENTS.md").write_text("Untrusted ordinary document.", encoding="utf-8")
        (root / ".env").write_text("SECRET=never-read", encoding="utf-8")

    async def test_automatic_onboarding_is_readonly_sourced_and_trust_gates_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._project(root)
            application = compose_fixture_application(tool_adapters=())
            await application.registry.start_all()
            try:
                task = await application.kernel.create_task("inspect", root, "task-onboarding")
                task = await application.kernel.transition_task(task.task_id, TaskState.INTAKE, "intake")
                task = await application.kernel.transition_task(
                    task.task_id, TaskState.RESOLVING_PROJECT, "resolve"
                )
                self.assertEqual(len(task.onboarding_snapshots), 1)
                snapshot = task.onboarding_snapshots[0]
                values = {(fact.category, fact.value) for fact in snapshot.facts}
                self.assertIn(("language", "JavaScript/TypeScript"), values)
                self.assertIn(("build_system", "Node.js"), values)
                self.assertIn(("entry_candidate", "src/index.ts"), values)
                self.assertIn(("candidate_command", "npm run build"), values)
                self.assertIn(("candidate_command", "npm run test"), values)
                self.assertFalse(snapshot.trusted_rules_read)
                self.assertNotIn(("trusted_rule_file", "AGENTS.md"), values)
                self.assertNotIn("never-read", str(snapshot.to_data()))
                events = await application.registry.require(RuntimeStorePort).read_events(task.task_id)
                self.assertEqual(
                    sum(event.event_type == "onboarding.phase_completed" for event in events),
                    len(ONBOARDING_PHASES),
                )
                self.assertIn("onboarding.completed", [event.event_type for event in events])
                self.assertFalse(any(event.event_type.startswith("process.") for event in events))
            finally:
                await application.registry.stop_all()

    async def test_cache_reused_across_tasks_and_change_creates_new_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._project(root)
            application = compose_fixture_application(tool_adapters=())
            await application.registry.start_all()
            try:
                first = await application.kernel.create_task("first", root, "task-first")
                first = await application.kernel.transition_task(first.task_id, TaskState.INTAKE, "intake")
                first = await application.kernel.transition_task(first.task_id, TaskState.RESOLVING_PROJECT, "resolve")
                second = await application.kernel.create_task("second", root, "task-second")
                second = await application.kernel.transition_task(second.task_id, TaskState.INTAKE, "intake")
                second = await application.kernel.transition_task(second.task_id, TaskState.RESOLVING_PROJECT, "resolve")
                self.assertEqual(second.onboarding_snapshots[0].snapshot_hash, first.onboarding_snapshots[0].snapshot_hash)
                events = await application.registry.require(RuntimeStorePort).read_events(second.task_id)
                self.assertIn("onboarding.reused", [event.event_type for event in events])
                self.assertNotIn("onboarding.started", [event.event_type for event in events])

                (root / "src" / "main.ts").write_text("console.log('new')", encoding="utf-8")
                refreshed = await application.kernel.run_project_onboarding(second.task_id)
                self.assertIsInstance(refreshed, ProjectOnboardingSnapshot)
                assert isinstance(refreshed, ProjectOnboardingSnapshot)
                self.assertEqual(refreshed.revision, 2)
                self.assertEqual(len((await application.kernel.get_task(second.task_id)).onboarding_snapshots), 2)
            finally:
                await application.registry.stop_all()

    async def test_phase_checkpoint_resumes_after_restart(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        self._project(root)
        database = root / ".agent" / "runtime.db"
        first = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(database), tool_adapters=()
        )
        await first.registry.start_all()
        task = await first.kernel.create_task("resume", root, "task-resume-onboarding")
        task = await first.kernel.transition_task(task.task_id, TaskState.INTAKE, "intake")
        # Enter the state without using transition_task's automatic full run.
        stored = await first.registry.require(RuntimeStorePort).load_task(task.task_id)
        assert stored is not None
        resolving = task.transition(TaskState.RESOLVING_PROJECT)
        from tsm_agt.ports import RuntimeEvent, RuntimeUnitOfWork
        await first.registry.require(RuntimeStorePort).commit(RuntimeUnitOfWork(
            task.task_id, stored.version, resolving.to_data(),
            (RuntimeEvent("evt-manual-resolving", task.task_id, stored.last_event_sequence + 1,
                          "task.state_changed", {"previous_state": "INTAKE", "next_state": "RESOLVING_PROJECT", "reason": "test"}),),
        ))
        partial = await first.kernel.run_project_onboarding(task.task_id, max_phases=2)
        self.assertIsInstance(partial, OnboardingCheckpoint)
        assert isinstance(partial, OnboardingCheckpoint)
        self.assertEqual(partial.completed_phases, ONBOARDING_PHASES[:2])
        await first.registry.stop_all()

        second = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(database), tool_adapters=()
        )
        await second.registry.start_all()
        try:
            completed = await second.kernel.run_project_onboarding(task.task_id)
            self.assertIsInstance(completed, ProjectOnboardingSnapshot)
            events = await second.registry.require(RuntimeStorePort).read_events(task.task_id)
            phases = [
                event.payload["phase"] for event in events
                if event.event_type == "onboarding.phase_completed"
            ]
            self.assertEqual(phases, list(ONBOARDING_PHASES))
        finally:
            await second.registry.stop_all()
            temporary.cleanup()

    async def test_fixed_project_instructions_are_trusted_separate_context(self) -> None:
        from tsm_agt.adapters.fixture import EchoModelProvider

        class CaptureModel(EchoModelProvider):
            def __init__(self):
                super().__init__()
                self.request = None

            async def complete(self, request):
                self.request = request
                return await super().complete(request)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._project(root)
            (root / ".agent").mkdir()
            (root / ".agent" / "projectInstructions.md").write_text(
                "Use strict TypeScript.", encoding="utf-8"
            )
            model = CaptureModel()
            application = compose_fixture_application(model_adapter=model, tool_adapters=())
            await application.registry.start_all()
            try:
                await application.kernel.set_project_trust(root, ProjectTrustLevel.TRUSTED_READ)
                task = await application.kernel.create_task("context", root, "task-context")
                for state in (TaskState.INTAKE, TaskState.RESOLVING_PROJECT, TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING):
                    task = await application.kernel.transition_task(task.task_id, state, state.value)
                self.assertTrue(task.onboarding_snapshots[0].trusted_rules_read)
                self.assertNotIn("AGENTS.md", [fact.value for fact in task.onboarding_snapshots[0].facts])
                await application.kernel.run_text_turn(task.task_id, "summarize")
                assert model.request is not None
                messages = [
                    message for message in model.request.messages
                    if message.message_id.startswith("project-instructions-context-")
                ]
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0].role, MessageRole.SYSTEM)
                self.assertIn("cannot override Runtime Policy", messages[0].text)
                self.assertIn("Use strict TypeScript", messages[0].text)
            finally:
                await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
