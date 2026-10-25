"""Deterministic production journeys across the complete Agent boundary.

These tests use real SQLite, workspace files and built-in tools. Only the
model is scripted so failures remain reproducible in the release gate.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.builtin import (
    CoreReadOnlyToolProvider, CoreWorkspaceMutationToolProvider,
)
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.resilient_model import ResilientModelProvider
from tsm_agt.adapters.rule_based_model_recovery import (
    RuleBasedModelRecoveryPolicy,
)
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.cli import _chat_error_guidance
from tsm_agt.core import (
    AcceptanceStatus, ModelInvocationFailed, TaskState,
)
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, HealthState, HealthStatus, Message,
    MessageRole, ModelRequest, ModelResponse, ModelUsage, ProviderCapabilities,
    RuntimeStorePort, TextBlock, ToolCall, ToolCallBlock, ToolResultBlock,
    ModelAttemptFailed, ModelAttemptFailure,
    ModelFailureCategory, ModelRecoveryAction, ModelRetrySafety, ModelRecoveryExhausted,
)


def answer_spec(goal: str) -> dict:
    return {
        "schema_version": 1, "goal": goal, "scope": ["."],
        "constraints": ["Base the answer on inspected project files"],
        "acceptance_criteria": [{
            "criterion_id": "workspace-integrity",
            "description": "Workspace mutations remain consistent",
            "verification_kind": "workspace_integrity",
        }],
        "outcomes": [{
            "outcome_id": "assessment",
            "description": "Deliver a project architecture assessment",
            "kind": "ANSWER", "required_effects": ["observe"],
            "required": True,
        }],
        "continuation_policy": {"mode": "NONE"},
    }


class ProductionPlanner:
    descriptor = AdapterDescriptor(
        "fixture.production-planner", "1.0", "TaskSpecPlannerPort", "1.0"
    )

    async def start(self, context) -> None:
        pass

    async def health(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY, "ready")

    async def stop(self, deadline: datetime) -> None:
        pass

    async def propose_task_spec(self, goal, context):
        return answer_spec(goal)




class ArchitectureAnalysisModel(EchoModelProvider):
    """List, read, then synthesize like a normal project analysis task."""

    capabilities = ProviderCapabilities(tools=True, context_window=128_000)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result for message in request.messages
            for block in message.content if isinstance(block, ToolResultBlock)
        ]
        if not results:
            return self._call(
                request, "inspect-project", "core.list_files",
                {"path": ".", "recursive": False, "limit": 50},
            )
        if len(results) == 1:
            return self._call(
                request, "read-config", "core.read_file",
                {"path": "pyproject.toml", "start_line": 1, "max_lines": 80},
            )
        return ModelResponse(Message(
            f"final-{request.turn_id}", MessageRole.ASSISTANT,
            (TextBlock(
                "The project uses a src layout and exposes a CLI entry point; "
                "the assessment is based on directory and pyproject evidence."
            ),),
        ), FinishReason.STOP, ModelUsage(20, 20))

    @staticmethod
    def _call(request, call_id, name, arguments):
        return ModelResponse(Message(
            f"model-{call_id}", MessageRole.ASSISTANT,
            (ToolCallBlock(ToolCall(
                call_id, name, arguments,
                outcome_ref="assessment",
            )),),
        ), FinishReason.TOOL_CALL, ModelUsage(10, 5))


class CorrectingOutcomeModel(EchoModelProvider):
    """First proposes an impossible side effect, then obeys correction."""

    capabilities = ProviderCapabilities(tools=True, context_window=128_000)

    def __init__(self, always_invalid: bool = False) -> None:
        super().__init__()
        self.calls = 0
        self.always_invalid = always_invalid
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requests.append(request)
        results = [
            block.result for message in request.messages
            for block in message.content if isinstance(block, ToolResultBlock)
        ]
        if results:
            return ModelResponse(Message(
                "corrected-final", MessageRole.ASSISTANT,
                (TextBlock("Assessment completed from read-only evidence."),),
            ), FinishReason.STOP, ModelUsage(5, 5))
        if self.calls == 1 or self.always_invalid:
            return ModelResponse(Message(
                f"invalid-{self.calls}", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "provider-call-1", "core.apply_patch",
                    {"patch": "not executed"}, outcome_ref="assessment",
                )),),
            ), FinishReason.TOOL_CALL, ModelUsage(5, 5))
        return ModelResponse(Message(
            "corrected-read", MessageRole.ASSISTANT,
            (ToolCallBlock(ToolCall(
                "provider-call-1", "core.list_files",
                {"path": ".", "limit": 20},
                outcome_ref="assessment",
            )),),
        ), FinishReason.TOOL_CALL, ModelUsage(5, 5))


class RecoveringMultiToolModel(ArchitectureAnalysisModel):
    """Fail only while synthesizing after several successful observations."""

    def __init__(self, *, persistent_failure: bool = False) -> None:
        super().__init__()
        self.persistent_failure = persistent_failure
        self.synthesis_attempts = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result for message in request.messages
            for block in message.content if isinstance(block, ToolResultBlock)
        ]
        if len(results) == 0:
            return self._call(
                request, "inspect-project", "core.list_files",
                {"path": ".", "recursive": False, "limit": 50},
            )
        if len(results) == 1:
            return self._call(
                request, "read-config", "core.read_file",
                {"path": "pyproject.toml", "start_line": 1, "max_lines": 80},
            )
        if len(results) == 2:
            return self._call(
                request, "read-module", "core.read_file",
                {"path": "src/module.py", "start_line": 1, "max_lines": 80},
            )
        self.synthesis_attempts += 1
        if self.persistent_failure or self.synthesis_attempts == 1:
            raise ModelAttemptFailed(ModelAttemptFailure(
                ModelFailureCategory.INVALID_RESPONSE,
                ModelRetrySafety.SAFE_RESAMPLE,
                "synthetic_uncommitted_response",
                "synthetic provider response could not be committed",
            ))
        return ModelResponse(Message(
            f"final-{request.turn_id}", MessageRole.ASSISTANT,
            (TextBlock(
                "The project exposes a CLI and delegates its main behavior to "
                "the inspected module; all three observations support this result."
            ),),
        ), FinishReason.STOP, ModelUsage(20, 20))


class ProductionJourneyTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _recovery_application(root: Path, *, persistent_failure: bool = False):
        physical = RecoveringMultiToolModel(
            persistent_failure=persistent_failure
        )
        policy = RuleBasedModelRecoveryPolicy(0)
        resilient = ResilientModelProvider(
            physical, policy, max_provider_attempts=2
        )
        app = compose_legacy_fixture_application(
            model_adapter=resilient,
            model_recovery_policy_adapter=policy,
            tool_adapters=(CoreReadOnlyToolProvider(),),
            store_adapter=SQLiteRuntimeStore(root / "runtime.db"),
            task_spec_planner_adapter=ProductionPlanner(),
        )
        return app, physical

    @staticmethod
    def _write_analysis_project(root: Path) -> None:
        (root / "pyproject.toml").write_text(
            "[project]\nname='journey'\n"
            "[project.scripts]\njourney='module:main'\n",
            encoding="utf-8",
        )
        (root / "src").mkdir()
        (root / "src" / "module.py").write_text(
            "def main():\n    return 'ready'\n", encoding="utf-8"
        )

    @staticmethod
    async def _executing_task(application, root: Path, goal: str):
        session = await application.kernel.create_session("production journey")
        task = await application.kernel.create_task(
            goal, root, session_id=session.session_id
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            task = await application.kernel.transition_task(
                task.task_id, state, f"production {state.value.lower()}"
            )
        return session, task

    @staticmethod
    async def _verify_and_finish(application, task_id: str):
        await application.kernel.transition_task(
            task_id, TaskState.VERIFYING, "production acceptance"
        )
        verification = await application.kernel.verify_task_acceptance(task_id)
        if verification.status is not AcceptanceStatus.PASSED:
            return verification
        await application.kernel.transition_task(
            task_id, TaskState.FINALIZING, "production verified"
        )
        await application.kernel.transition_task(
            task_id, TaskState.SUCCEEDED, "production complete"
        )
        return verification

    async def test_failed_patch_is_observed_without_changing_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "marker.txt"
            marker.write_text("unchanged\n", encoding="utf-8")
            model = CorrectingOutcomeModel()
            app = compose_legacy_fixture_application(
                model_adapter=model,
                tool_adapters=(
                    CoreReadOnlyToolProvider(),
                    CoreWorkspaceMutationToolProvider(),
                ),
                store_adapter=SQLiteRuntimeStore(root / "runtime.db"),
                task_spec_planner_adapter=ProductionPlanner(),
            )
            await app.registry.start_all()
            try:
                _, task = await self._executing_task(app, root, "Assess safely")
                result = await app.kernel.run_agent_turn(task.task_id, task.goal)
                self.assertEqual(result.model_calls, 2)
                self.assertEqual(result.tool_calls, 0)
                self.assertEqual(
                    marker.read_text(encoding="utf-8"), "unchanged\n"
                )
                events = await app.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                self.assertFalse(any(
                    event.event_type == "task_outcome.binding_decided"
                    for event in events
                ))
                started = [
                    event.payload["call"]["name"] for event in events
                    if event.event_type == "tool.started"
                ]
                self.assertEqual(started, [])
            finally:
                await app.registry.stop_all()

    async def test_invalid_patch_result_is_not_reported_as_network_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = CorrectingOutcomeModel(always_invalid=True)
            app = compose_legacy_fixture_application(
                model_adapter=model,
                tool_adapters=(
                    CoreReadOnlyToolProvider(),
                    CoreWorkspaceMutationToolProvider(),
                ),
                store_adapter=SQLiteRuntimeStore(root / "runtime.db"),
                task_spec_planner_adapter=ProductionPlanner(),
            )
            await app.registry.start_all()
            try:
                _, task = await self._executing_task(app, root, "Assess safely")
                result = await app.kernel.run_agent_turn(task.task_id, task.goal)
                self.assertEqual(result.model_calls, 2)
                events = await app.registry.require(RuntimeStorePort).read_events(task.task_id)
                self.assertFalse(any(e.event_type == "llm.failed" for e in events))
                self.assertFalse(any(e.event_type == "tool.started" for e in events))
                failures = [block.result for message in model.requests[-1].messages
                            for block in message.content if isinstance(block, ToolResultBlock)]
                self.assertTrue(failures)
                self.assertFalse(failures[-1].ok)
                self.assertIs((await app.kernel.get_task(task.task_id)).state, TaskState.AWAITING_INPUT)
            finally:
                await app.registry.stop_all()

    async def test_multi_tool_recovery_exhaustion_preserves_checkpoint_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_analysis_project(root)
            app, physical = self._recovery_application(
                root, persistent_failure=True
            )
            await app.registry.start_all()
            try:
                _, task = await self._executing_task(
                    app, root, "Assess the project entry point and module boundary"
                )
                with self.assertRaises(ModelRecoveryExhausted) as caught:
                    await app.kernel.run_agent_turn(task.task_id, task.goal)
                error = caught.exception
                self.assertEqual(
                    error.failure.category, ModelFailureCategory.INVALID_RESPONSE
                )
                self.assertEqual(
                    error.decision.action,
                    ModelRecoveryAction.PRESERVE_AND_INTERRUPT,
                )
                self.assertEqual(physical.synthesis_attempts, 2)
                interrupted = await app.kernel.get_task(task.task_id)
                self.assertEqual(interrupted.state, TaskState.INTERRUPTED)
                self.assertIsNotNone(interrupted.active_agent_checkpoint)
                checkpoint = interrupted.active_agent_checkpoint or {}
                self.assertEqual(checkpoint["tool_calls"], 3)
                self.assertEqual(checkpoint["model_calls"], 3)
                events = await app.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                self.assertEqual(sum(
                    event.event_type == "tool.completed" for event in events
                ), 3)
                terminal = [
                    event for event in events
                    if event.event_type == "model.recovery_decided"
                ][-1]
                self.assertEqual(
                    terminal.payload["recovery_action"],
                    "PRESERVE_AND_INTERRUPT",
                )
                failed = next(
                    event for event in reversed(events)
                    if event.event_type == "llm.failed"
                )
                self.assertEqual(
                    failed.payload["failure_category"], "INVALID_RESPONSE"
                )
                guidance = " ".join(_chat_error_guidance(error, root))
                self.assertNotIn("doctor --model-check", guidance)
                self.assertNotIn("工具执行失败", guidance)
            finally:
                await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()


from tsm_agt.core import OrchestrationMode


def compose_legacy_fixture_application(*args, **kwargs):
    return compose_fixture_application(*args, **kwargs)
