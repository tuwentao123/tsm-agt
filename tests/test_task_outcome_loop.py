from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from tsm_agt.adapters.builtin import (
    CoreReadOnlyToolProvider, CoreTaskSpecToolProvider,
)
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.model_task_spec_planner import ModelTaskSpecPlanner
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AcceptanceStatus, AgentContinuationSuspended, SessionContinuationMode,
    AgentClarificationSuspended, AgentTurnSuspended, ApprovalDecision,
    ApprovalRequired,
    InvalidToolArguments,
    ProjectTrustLevel, TaskAcceptanceCriterion, TaskCriterionKind,
    TaskOutcomeEligibilityCalculator, TaskOutcomeStatus, TaskSpecProposal,
    TaskSpecSnapshot, TaskState, ToolBatchSnapshot, ToolBatchStatus,
)
from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, FinishReason, HealthState, HealthStatus,
    Message, MessageRole, ModelRequest, ModelResponse, ModelUsage,
    OutcomeBindingMode, ProviderCapabilities, TaskSpecPlannerPort, TextBlock, ToolCall,
    ToolCallBlock, ToolEffect, ToolIdempotency, ToolInvocationContext,
    ToolResult, ToolResultBlock, ToolRisk, ToolSpec,
)


def proposal(goal: str, outcome_ids=("inspect",), continuation="NONE"):
    return {
        "schema_version": 1, "goal": goal, "scope": ["."],
        "constraints": [],
        "outcomes": [{
            "outcome_id": outcome_id,
            "description": f"Collect {outcome_id} evidence",
            "kind": "EVIDENCE", "required_effects": ["observe"],
            "required": True,
        } for outcome_id in outcome_ids],
        "continuation_policy": {"mode": continuation},
    }


class TwoUnitModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    def __init__(self, tool_name: str = "core.list_files"):
        super().__init__()
        self.tool_name = tool_name

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result
            for message in request.messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        if len(results) < 2:
            outcome = "inspect-a" if not results else "inspect-b"
            return ModelResponse(Message(
                f"model-{outcome}", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    f"call-{outcome}", self.tool_name,
                    {"path": "."} if self.tool_name == "core.list_files" else {},
                    outcome_ref=outcome,
                )),),
            ), FinishReason.TOOL_CALL, ModelUsage(1, 1))
        return ModelResponse(Message(
            "model-final", MessageRole.ASSISTANT,
            (TextBlock("Both required units are complete."),),
        ), FinishReason.STOP, ModelUsage(1, 1))


class SequencePlannerModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    def __init__(self, arguments):
        super().__init__()
        self.arguments = list(arguments)
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        arguments = self.arguments.pop(0)
        return ModelResponse(Message(
            f"planner-{self.calls}", MessageRole.ASSISTANT,
            (ToolCallBlock(ToolCall(
                f"planner-call-{self.calls}",
                "planner.submit_task_spec", arguments,
            )),),
        ), FinishReason.TOOL_CALL)


class TextPlannerModel(EchoModelProvider):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(Message(
            f"text-planner-{self.calls}", MessageRole.ASSISTANT,
            (TextBlock(self.responses.pop(0)),),
        ), FinishReason.STOP, ModelUsage(1, 1))


class FixturePlanner:
    descriptor = AdapterDescriptor(
        "fixture.task-spec-planner", "1.0", "TaskSpecPlannerPort", "1.0"
    )

    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.started = False

    async def start(self, context: AdapterContext):
        self.started = True

    async def health(self):
        return HealthStatus(
            HealthState.HEALTHY if self.started else HealthState.UNHEALTHY
        )

    async def stop(self, deadline: datetime):
        self.started = False

    async def propose_task_spec(self, goal, context):
        if self.error is not None:
            raise self.error
        return self.data


class EffectToolProvider:
    """Project-neutral fixture exposing each executable ToolEffect."""

    descriptor = AdapterDescriptor(
        "fixture.outcome-effects", "1.0", "ToolProviderPort", "1.0"
    )

    def __init__(self, effect: ToolEffect, *, delay: float = 0.0):
        self.effect = effect
        self.delay = delay
        self.name = f"fixture.{effect.value}"

    async def start(self, context):
        pass

    async def health(self):
        return HealthStatus(HealthState.HEALTHY)

    async def stop(self, deadline):
        pass

    async def list_tools(self):
        return (ToolSpec(
            self.name, f"Produce one {self.effect.value} result",
            {"type": "object", "properties": {},
             "additionalProperties": False},
            ToolRisk.R0 if self.effect is ToolEffect.OBSERVE else ToolRisk.R1,
            is_read_only=self.effect is ToolEffect.OBSERVE,
            idempotency=ToolIdempotency.IDEMPOTENT,
            effect=self.effect,
        ),)

    async def invoke(self, call: ToolCall, context: ToolInvocationContext):
        if self.delay:
            import asyncio
            await asyncio.sleep(self.delay)
        return ToolResult(call.call_id, True, {"effect": self.effect.value})


class AutoBoundEffectModel(EchoModelProvider):
    """Call one effect tool without outcome_ref to exercise Runtime binding."""

    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    def __init__(self, tool_name: str):
        super().__init__()
        self.tool_name = tool_name

    async def complete(self, request: ModelRequest) -> ModelResponse:
        result = next((
            block.result for message in reversed(request.messages)
            for block in message.content if isinstance(block, ToolResultBlock)
        ), None)
        if result is None:
            return ModelResponse(Message(
                "effect-call", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall("effect-call", self.tool_name, {})),),
            ), FinishReason.TOOL_CALL, ModelUsage(1, 1))
        return ModelResponse(Message(
            "effect-final", MessageRole.ASSISTANT,
            (TextBlock("The requested effect completed."),),
        ), FinishReason.STOP, ModelUsage(1, 1))


class UserDecisionModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        result = next((
            block.result for message in reversed(request.messages)
            for block in message.content if isinstance(block, ToolResultBlock)
        ), None)
        if result is None:
            return ModelResponse(Message(
                "decision-call", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "decision-call", "core.request_input", {
                        "question": "Choose one option",
                        "choices": [{"value": "one", "label": "One"}],
                        "reason": "A user decision is required",
                    }, outcome_ref="decision",
                )),),
            ), FinishReason.TOOL_CALL, ModelUsage(1, 1))
        return ModelResponse(Message(
            "decision-final", MessageRole.ASSISTANT,
            (TextBlock("The selected option was recorded."),),
        ), FinishReason.STOP, ModelUsage(1, 1))


class SharedOutcomeBatchModel(EchoModelProvider):
    """Issue multiple evidence actions for one broad analysis Outcome."""

    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result for message in request.messages
            for block in message.content if isinstance(block, ToolResultBlock)
        ]
        if not results:
            return ModelResponse(Message(
                "shared-outcome-batch", MessageRole.ASSISTANT, (
                    ToolCallBlock(ToolCall(
                        "find-config", "core.find_files",
                        {"path": ".", "pattern": "pyproject.toml"},
                        outcome_ref="inspect",
                    )),
                    ToolCallBlock(ToolCall(
                        "find-source", "core.find_files",
                        {"path": ".", "pattern": "*.py"},
                        outcome_ref="inspect",
                    )),
                ),
            ), FinishReason.TOOL_CALL, ModelUsage(1, 1))
        return ModelResponse(Message(
            "shared-outcome-final", MessageRole.ASSISTANT,
            (TextBlock("The project structure was analyzed from both searches."),),
        ), FinishReason.STOP, ModelUsage(1, 1))


class ExplicitCompletionModel(EchoModelProvider):
    """Complete a composite Outcome through the public internal-state tool."""

    capabilities = ProviderCapabilities(tools=True, context_window=8192)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        results = [
            block.result for message in request.messages
            for block in message.content if isinstance(block, ToolResultBlock)
        ]
        if not results:
            return ModelResponse(Message(
                "explicit-read", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "explicit-read", "core.list_files", {"path": "."},
                    outcome_ref="inspection",
                )),),
            ), FinishReason.TOOL_CALL, ModelUsage(1, 1))
        if len(results) == 1:
            turn_id = request.turn_id
            reference = f"tool:{turn_id}:explicit-read:observe"
            return ModelResponse(Message(
                "explicit-complete", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "explicit-complete", "core.task_outcome_complete", {
                        "outcome_id": "inspection",
                        "completion_summary": "Inspection completed",
                        "evidence_refs": [reference],
                        "remaining_work": [],
                    },
                )),),
            ), FinishReason.TOOL_CALL, ModelUsage(1, 1))
        return ModelResponse(Message(
            "explicit-final", MessageRole.ASSISTANT,
            (TextBlock("Inspection completed and accepted."),),
        ), FinishReason.STOP, ModelUsage(1, 1))


class CrossOutcomeBatchModel(EchoModelProvider):
    """One response closes an Outcome before its second call executes."""

    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    def __init__(self):
        super().__init__()
        self.requests = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        results = [
            block.result for message in request.messages
            for block in message.content if isinstance(block, ToolResultBlock)
        ]
        if not results:
            return ModelResponse(Message(
                "cross-outcome-batch", MessageRole.ASSISTANT, (
                    ToolCallBlock(ToolCall(
                        "close-first", "core.list_files", {"path": "."},
                        outcome_ref="inspect-a",
                    )),
                    ToolCallBlock(ToolCall(
                        "run-second", "core.list_files", {"path": "."},
                        outcome_ref="inspect-b",
                    )),
                ),
            ), FinishReason.TOOL_CALL, ModelUsage(1, 1))
        return ModelResponse(Message(
            "cross-outcome-final", MessageRole.ASSISTANT,
            (TextBlock("Both accepted calls completed."),),
        ), FinishReason.STOP, ModelUsage(1, 1))


class AnswerEvidenceModel(EchoModelProvider):
    """Read project evidence for an ANSWER before synthesizing its text."""

    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        result = next((
            block.result for message in request.messages
            for block in message.content if isinstance(block, ToolResultBlock)
        ), None)
        if result is None:
            return ModelResponse(Message(
                "answer-evidence-call", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "answer-evidence-call", "core.list_files",
                    {"path": "."}, outcome_ref="assessment",
                )),),
            ), FinishReason.TOOL_CALL, ModelUsage(1, 1))
        return ModelResponse(Message(
            "answer-evidence-final", MessageRole.ASSISTANT,
            (TextBlock("The project assessment is based on the observed files."),),
        ), FinishReason.STOP, ModelUsage(1, 1))


class SeparateEvidenceAnswerModel(EchoModelProvider):
    """Collect evidence under one Outcome, then synthesize another."""

    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        result = next((
            block.result for message in request.messages
            for block in message.content if isinstance(block, ToolResultBlock)
        ), None)
        if result is None:
            return ModelResponse(Message(
                "separate-evidence-call", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "separate-evidence-call", "core.list_files",
                    {"path": "."}, outcome_ref="workspace_evidence",
                )),),
            ), FinishReason.TOOL_CALL, ModelUsage(1, 1))
        return ModelResponse(Message(
            "separate-answer-final", MessageRole.ASSISTANT,
            (TextBlock("The answer uses the collected workspace evidence."),),
        ), FinishReason.STOP, ModelUsage(1, 1))


class TaskOutcomeLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_locked_downstream_observation_is_support_only(self):
        """Preparatory reads may inspect active work without completing it."""
        with tempfile.TemporaryDirectory() as directory:
            observe = EffectToolProvider(ToolEffect.OBSERVE)
            mutate = EffectToolProvider(ToolEffect.MUTATE)
            execute = EffectToolProvider(ToolEffect.EXECUTE)
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(),
                tool_adapters=(observe,),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "implement and verify", Path(directory)
                )
                current = await app.kernel.get_task_spec(task.task_id)
                data = proposal(task.goal)
                data["outcomes"] = [
                    {
                        "outcome_id": "implementation",
                        "description": "Implement the change",
                        "kind": "WORKSPACE_DELIVERY",
                        "required_effects": ["mutate"],
                        "required": True,
                    },
                    {
                        "outcome_id": "verification",
                        "description": "Verify the implementation",
                        "kind": "EVIDENCE",
                        "required_effects": ["observe"],
                        "required": True,
                        "depends_on": ["implementation"],
                    },
                    {
                        "outcome_id": "other_implementation",
                        "description": "Implement an unrelated change",
                        "kind": "WORKSPACE_DELIVERY",
                        "required_effects": ["mutate"],
                        "required": True,
                    },
                    {
                        "outcome_id": "other_verification",
                        "description": "Verify the unrelated change",
                        "kind": "EVIDENCE",
                        "required_effects": ["observe"],
                        "required": True,
                        "depends_on": ["other_implementation"],
                    },
                ]
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2, TaskSpecProposal.from_data(data),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                await app.kernel.select_task_outcomes(
                    task.task_id, ("implementation",),
                    reason="fixture_active_implementation",
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )

                supporting = await app.kernel._bind_tool_call_outcome(
                    task.task_id, ToolCall(
                        "supporting-read", observe.name, {},
                        outcome_ref="verification",
                    ), (await observe.list_tools())[0],
                )
                self.assertEqual(
                    supporting.outcome_binding_mode,
                    OutcomeBindingMode.SUPPORTING,
                )
                result = await app.kernel.invoke_tool(
                    task.task_id, "turn-supporting", supporting
                )
                self.assertTrue(result.ok)
                outcomes = {
                    item.outcome_id: item for item in
                    (await app.kernel.get_task_spec(task.task_id)).outcomes
                }
                self.assertEqual(
                    outcomes["verification"].status,
                    TaskOutcomeStatus.PENDING,
                )
                self.assertFalse(outcomes["verification"].fulfillment_refs)
                events = await app.kernel.dependencies.store.read_events(
                    task.task_id
                )
                self.assertTrue(any(
                    event.event_type == "task_outcome.support_observed"
                    and event.payload["outcome_id"] == "verification"
                    for event in events
                ))
                self.assertFalse(any(
                    event.event_type == "task_outcome.state_changed"
                    and event.payload["outcome_id"] == "verification"
                    and event.payload.get("tool_call_id") == "supporting-read"
                    for event in events
                ))

                for provider in (mutate, execute):
                    with self.assertRaisesRegex(
                        InvalidToolArguments,
                        "OUTCOME_DEPENDENCY_UNSATISFIED",
                    ):
                        await app.kernel._bind_tool_call_outcome(
                            task.task_id, ToolCall(
                                f"locked-{provider.effect.value}",
                                provider.name, {}, outcome_ref="verification",
                            ), (await provider.list_tools())[0],
                        )

                with self.assertRaisesRegex(
                    InvalidToolArguments, "OUTCOME_DEPENDENCY_UNSATISFIED"
                ):
                    await app.kernel._bind_tool_call_outcome(
                        task.task_id, ToolCall(
                            "unrelated-read", observe.name, {},
                            outcome_ref="other_verification",
                        ), (await observe.list_tools())[0],
                    )

                await app.kernel._append_events(task.task_id, ((
                    "task_outcome.state_changed", {
                        "outcome_id": "implementation",
                        "status": TaskOutcomeStatus.DELIVERED.value,
                        "fulfillment_ref": "fixture:implementation:mutate",
                        "reason": "fixture_dependency_ready",
                    },
                ),))
                fulfillment = await app.kernel._bind_tool_call_outcome(
                    task.task_id, ToolCall(
                        "verification-read", observe.name, {},
                        outcome_ref="verification",
                    ), (await observe.list_tools())[0],
                )
                self.assertEqual(
                    fulfillment.outcome_binding_mode,
                    OutcomeBindingMode.FULFILLMENT,
                )
            finally:
                await app.registry.stop_all()

    def test_supporting_binding_survives_storage_and_legacy_defaults(self):
        supporting = ToolCall(
            "support", "core.list_files", {"path": "."},
            outcome_ref="verification",
            outcome_binding_mode=OutcomeBindingMode.SUPPORTING,
        )
        restored = ToolCall.from_data(supporting.to_data())
        self.assertEqual(
            restored.outcome_binding_mode, OutcomeBindingMode.SUPPORTING
        )
        legacy = supporting.to_data()
        legacy.pop("outcome_binding_mode")
        self.assertEqual(
            ToolCall.from_data(legacy).outcome_binding_mode,
            OutcomeBindingMode.FULFILLMENT,
        )
        batch = ToolBatchSnapshot(
            "batch-support", "message-support", (supporting,),
            (supporting.call_id,), ToolBatchStatus.ACCEPTED, 2, 3,
        )
        self.assertEqual(
            ToolBatchSnapshot.from_data(batch.to_data()).calls[0]
            .outcome_binding_mode,
            OutcomeBindingMode.SUPPORTING,
        )

    def test_dependency_with_fulfilled_effects_allows_verification_before_acceptance(self):
        """Modification can be verified before its final acceptance closes."""
        proposed = proposal("modify then verify", ("workspace", "verification"))
        proposed["outcomes"][0].update({
            "kind": "WORKSPACE_DELIVERY",
            "required_effects": ["observe", "mutate"],
        })
        proposed["outcomes"][1].update({
            "kind": "EVIDENCE",
            "required_effects": ["execute", "observe"],
            "depends_on": ["workspace"],
        })
        initial = TaskSpecSnapshot.initial("task-dag", "modify then verify")
        spec = TaskSpecSnapshot.from_proposal(
            "task-dag", 2, TaskSpecProposal.from_data(proposed),
            initial.acceptance_criteria,
        )
        workspace = replace(
            spec.outcomes[0], status=TaskOutcomeStatus.IN_PROGRESS,
            fulfillment_refs=(
                "tool:read:observe", "tool:patch:mutate",
            ),
        )
        spec = replace(
            spec, outcomes=(workspace, spec.outcomes[1]), content_hash=""
        )
        eligible = TaskOutcomeEligibilityCalculator.eligible(spec)
        self.assertEqual(
            [item.outcome_id for item in eligible],
            ["workspace", "verification"],
        )
        self.assertFalse(workspace.status.is_closed)

    def test_dependency_missing_one_effect_keeps_downstream_ineligible(self):
        proposed = proposal("modify then verify", ("workspace", "verification"))
        proposed["outcomes"][0].update({
            "kind": "WORKSPACE_DELIVERY",
            "required_effects": ["observe", "mutate"],
        })
        proposed["outcomes"][1].update({
            "required_effects": ["execute"],
            "depends_on": ["workspace"],
        })
        initial = TaskSpecSnapshot.initial("task-dag", "modify then verify")
        spec = TaskSpecSnapshot.from_proposal(
            "task-dag", 2, TaskSpecProposal.from_data(proposed),
            initial.acceptance_criteria,
        )
        workspace = replace(
            spec.outcomes[0], status=TaskOutcomeStatus.IN_PROGRESS,
            fulfillment_refs=("tool:patch:mutate",),
        )
        spec = replace(
            spec, outcomes=(workspace, spec.outcomes[1]), content_hash=""
        )
        self.assertEqual(
            [item.outcome_id for item in
             TaskOutcomeEligibilityCalculator.eligible(spec)],
            ["workspace"],
        )

    async def test_misbound_reads_close_evidence_but_not_workspace_delivery(self):
        """Task-scoped reads can support evidence, never fake a mutation."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "implementation.py").write_text(
                "VISIBLE_THRESHOLD = 0.5\n", encoding="utf-8"
            )
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(),
                tool_adapters=(CoreReadOnlyToolProvider(),),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "locate code, assess reuse, then implement", root
                )
                current = await app.kernel.get_task_spec(task.task_id)
                data = proposal(task.goal)
                data["outcomes"] = [
                    {
                        "outcome_id": "locate_code",
                        "description": "Locate the implementation",
                        "kind": "EVIDENCE",
                        "required_effects": ["observe"],
                        "required": True,
                    },
                    {
                        "outcome_id": "assess_capability",
                        "description": "Assess reusable capability",
                        "kind": "EVIDENCE",
                        "required_effects": ["observe"],
                        "required": True,
                    },
                    {
                        "outcome_id": "deliver_change",
                        "description": "Deliver the workspace change",
                        "kind": "WORKSPACE_DELIVERY",
                        "required_effects": ["observe", "mutate"],
                        "required": True,
                    },
                ]
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2, TaskSpecProposal.from_data(data),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )

                # Reproduce the real failure: both reads were structurally bound
                # to the delivery Outcome instead of the two Evidence Outcomes.
                for call_id in ("read-location", "read-capability"):
                    result = await app.kernel.invoke_tool(
                        task.task_id, "turn-misbound", ToolCall(
                            call_id, "core.read_file",
                            {"path": "implementation.py"},
                            outcome_ref="deliver_change",
                        ),
                    )
                    self.assertTrue(result.ok)

                gaps = await app.kernel._completion_readiness_gaps(
                    task.task_id, await app.kernel.list_tools()
                )
                self.assertEqual(
                    [gap.gap_id for gap in gaps],
                    ["task-outcome:deliver_change"],
                )
                self.assertEqual(gaps[0].required_effects, (ToolEffect.MUTATE,))

                await app.kernel._append_events(task.task_id, (
                    ("llm.completed", {
                        "turn_id": "turn-misbound",
                        "message": {
                            "message_id": "final-analysis",
                            "role": "assistant",
                            "content": [{
                                "type": "text",
                                "text": (
                                    "The code location and reusable capability "
                                    "are identified. The workspace change is not "
                                    "implemented yet."
                                ),
                            }],
                        },
                        "finish_reason": "stop",
                    }),
                    ("turn.completed", {"turn_id": "turn-misbound"}),
                ))
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify partial delivery"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertFalse(verification.passed)
                outcomes = {
                    item.outcome_id: item
                    for item in (await app.kernel.get_task_spec(task.task_id)).outcomes
                }
                self.assertEqual(
                    outcomes["locate_code"].status, TaskOutcomeStatus.DELIVERED
                )
                self.assertEqual(
                    outcomes["assess_capability"].status,
                    TaskOutcomeStatus.DELIVERED,
                )
                self.assertEqual(
                    outcomes["deliver_change"].status,
                    TaskOutcomeStatus.IN_PROGRESS,
                )
                criterion = next(
                    item for item in verification.criteria
                    if item.criterion_id == "task-outcome-fulfillment"
                )
                self.assertEqual(criterion.status, AcceptanceStatus.BLOCKED)
                self.assertEqual(len(criterion.evidence), 1)
                self.assertEqual(
                    criterion.evidence[0].source_step_id, "deliver_change"
                )
            finally:
                await app.registry.stop_all()

    async def test_answer_reuses_observation_from_separate_evidence_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            app = compose_fixture_application(
                model_adapter=SeparateEvidenceAnswerModel(),
                tool_adapters=(CoreReadOnlyToolProvider(),),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("answer from evidence", root)
                current = await app.kernel.get_task_spec(task.task_id)
                data = proposal("answer from evidence")
                data["outcomes"] = [
                    {
                        "outcome_id": "workspace_evidence",
                        "description": "Collect workspace evidence",
                        "kind": "EVIDENCE",
                        "required_effects": ["observe"],
                        "required": True,
                    },
                    {
                        "outcome_id": "final_answer",
                        "description": "Answer from workspace evidence",
                        "kind": "ANSWER",
                        "required_effects": ["observe"],
                        "required": True,
                    },
                ]
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2, TaskSpecProposal.from_data(data),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                turn = await app.kernel.run_agent_turn(task.task_id, task.goal)
                self.assertEqual(turn.tool_calls, 1)
                before = await app.kernel.get_task_spec(task.task_id)
                self.assertEqual(
                    before.outcomes[0].status, TaskOutcomeStatus.IN_PROGRESS
                )
                self.assertEqual(
                    before.outcomes[1].status, TaskOutcomeStatus.PENDING
                )

                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify synthesized answer"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertTrue(verification.passed)
                outcomes = (await app.kernel.get_task_spec(task.task_id)).outcomes
                self.assertTrue(all(
                    item.status is TaskOutcomeStatus.DELIVERED for item in outcomes
                ))
                evidence_ref = outcomes[0].fulfillment_refs[0]
                self.assertIn(evidence_ref, outcomes[1].fulfillment_refs)
                self.assertTrue(evidence_ref.endswith(":observe"))
            finally:
                await app.registry.stop_all()

    async def test_observe_can_support_answer_without_directly_delivering_it(self):
        """Regression: read evidence may support a conversational Outcome."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[project]\n", encoding="utf-8"
            )
            app = compose_fixture_application(
                model_adapter=AnswerEvidenceModel(),
                tool_adapters=(CoreReadOnlyToolProvider(),),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("assess project", root)
                current = await app.kernel.get_task_spec(task.task_id)
                data = proposal("assess project")
                data["outcomes"][0].update({
                    "outcome_id": "assessment",
                    "description": "Provide a project assessment",
                    "kind": "ANSWER",
                    "required_effects": [],
                })
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2, TaskSpecProposal.from_data(data),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                turn = await app.kernel.run_agent_turn(task.task_id, task.goal)
                self.assertEqual(turn.tool_calls, 1)
                supported = (
                    await app.kernel.get_task_spec(task.task_id)
                ).outcomes[0]
                self.assertEqual(supported.status, TaskOutcomeStatus.IN_PROGRESS)
                self.assertTrue(supported.fulfillment_refs[0].endswith(":observe"))

                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify answer"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertTrue(verification.passed)
                delivered = (
                    await app.kernel.get_task_spec(task.task_id)
                ).outcomes[0]
                self.assertEqual(delivered.status, TaskOutcomeStatus.DELIVERED)
            finally:
                await app.registry.stop_all()

    async def test_model_planner_corrects_protocol_once(self):
        model = SequencePlannerModel([
            {"schema_version": 1}, proposal("inspect project"),
        ])
        planner = ModelTaskSpecPlanner(model)
        context = AdapterContext(config={}, emit_event=lambda *_: None)
        await model.start(context)
        await planner.start(context)
        try:
            planned = await planner.propose_task_spec(
                "inspect project", {"workspace": "/tmp/project"}
            )
            self.assertEqual(model.calls, 2)
            self.assertEqual(planned["outcomes"][0]["outcome_id"], "inspect")
        finally:
            await planner.stop(datetime.now())
            await model.stop(datetime.now())

    async def test_text_only_planner_uses_strict_json_and_one_correction(self):
        import json
        expected = proposal("inspect text provider")
        model = TextPlannerModel([
            "not-json", json.dumps(expected, ensure_ascii=False),
        ])
        planner = ModelTaskSpecPlanner(model)
        context = AdapterContext(config={}, emit_event=lambda *_: None)
        await model.start(context)
        await planner.start(context)
        try:
            planned = await planner.propose_task_spec(
                "inspect text provider", {"workspace": "/tmp/project"}
            )
            self.assertEqual(model.calls, 2)
            self.assertEqual(planned, expected)
        finally:
            await planner.stop(datetime.now())
            await model.stop(datetime.now())

    async def test_planner_failure_is_persisted_and_task_remains_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = FixturePlanner(error=ValueError("invalid proposal"))
            app = compose_fixture_application(
                tool_adapters=(), task_spec_planner_adapter=planner,
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("inspect", Path(directory))
                task = await app.kernel.transition_task(
                    task.task_id, TaskState.INTAKE, "intake"
                )
                for state in (
                    TaskState.RESOLVING_PROJECT, TaskState.SELECTING_EXTENSIONS,
                    TaskState.ROUTING, TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                with self.assertRaisesRegex(ValueError, "invalid proposal"):
                    await app.kernel.run_agent_turn(task.task_id, "inspect")
                self.assertEqual(
                    (await app.kernel.get_task(task.task_id)).state, TaskState.EXECUTING
                )
                events = await app.kernel.dependencies.store.read_events(task.task_id)
                failure = [
                    event for event in events
                    if event.event_type == "task_spec.planning_failed"
                ]
                self.assertEqual(len(failure), 1)
                self.assertTrue(failure[0].payload["recoverable"])
            finally:
                await app.registry.stop_all()

    async def _prepared_app(self, root: Path, outcome_ids=("inspect",)):
        app = compose_fixture_application(
            model_adapter=EchoModelProvider(),
            tool_adapters=(CoreReadOnlyToolProvider(),),
        )
        await app.registry.start_all()
        task = await app.kernel.create_task("inspect project", root)
        current = await app.kernel.get_task_spec(task.task_id)
        spec = TaskSpecSnapshot.from_proposal(
            task.task_id, 2, TaskSpecProposal.from_data(
                proposal("inspect project", outcome_ids)
            ), current.acceptance_criteria,
        )
        await app.kernel._append_events(task.task_id, ((
            "task_spec.revised", {"snapshot": spec.to_data()},
        ),))
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await app.kernel.transition_task(task.task_id, state, state.value)
        return app, task

    async def test_successful_observation_accumulates_without_premature_close(self):
        with tempfile.TemporaryDirectory() as directory:
            app, task = await self._prepared_app(Path(directory))
            try:
                result = await app.kernel.invoke_tool(
                    task.task_id, "turn-outcome",
                    ToolCall("list", "core.list_files", {"path": "."}),
                )
                self.assertTrue(result.ok)
                outcome = (await app.kernel.get_task_spec(task.task_id)).outcomes[0]
                self.assertEqual(outcome.status, TaskOutcomeStatus.IN_PROGRESS)
                self.assertTrue(outcome.fulfillment_refs[0].endswith(":observe"))
            finally:
                await app.registry.stop_all()

    async def test_answer_outcome_stays_open_until_visible_answer_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(),
                tool_adapters=(CoreReadOnlyToolProvider(),),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("answer from source", root)
                current = await app.kernel.get_task_spec(task.task_id)
                answer_data = proposal("answer from source")
                answer_data["outcomes"][0]["kind"] = "ANSWER"
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2, TaskSpecProposal.from_data(answer_data),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                result = await app.kernel.invoke_tool(
                    task.task_id, "turn-answer-evidence", ToolCall(
                        "list-answer", "core.list_files", {"path": "."},
                        outcome_ref="inspect",
                    ),
                )
                self.assertTrue(result.ok)
                outcome = (
                    await app.kernel.get_task_spec(task.task_id)
                ).outcomes[0]
                self.assertEqual(outcome.status, TaskOutcomeStatus.IN_PROGRESS)
                self.assertTrue(outcome.fulfillment_refs)
            finally:
                await app.registry.stop_all()

    async def test_multiple_candidates_require_explicit_outcome_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            app, task = await self._prepared_app(
                Path(directory), ("inspect-a", "inspect-b")
            )
            try:
                first = await app.kernel.invoke_tool(
                    task.task_id, "turn-explicit", ToolCall(
                        "list-b", "core.list_files", {"path": "."},
                        outcome_ref="inspect-b",
                    ),
                )
                self.assertTrue(first.ok)
                # Multiple compatible selected outcomes cannot be guessed.
                with self.assertRaisesRegex(
                    Exception, "OUTCOME_SELECTION_AMBIGUOUS"
                ):
                    await app.kernel.invoke_tool(
                        task.task_id, "turn-ambiguous",
                        ToolCall(
                            "list-a", "core.list_files", {"path": "."}
                        ),
                    )
                self.assertTrue(all(
                    outcome.status is TaskOutcomeStatus.PENDING
                    if outcome.outcome_id == "inspect-a" else
                    outcome.status is TaskOutcomeStatus.IN_PROGRESS
                    for outcome in (await app.kernel.get_task_spec(task.task_id)).outcomes
                ))
                outcomes = {
                    item.outcome_id: item
                    for item in (await app.kernel.get_task_spec(task.task_id)).outcomes
                }
                self.assertEqual(
                    outcomes["inspect-b"].status, TaskOutcomeStatus.IN_PROGRESS
                )
                self.assertEqual(
                    outcomes["inspect-a"].status, TaskOutcomeStatus.PENDING
                )
            finally:
                await app.registry.stop_all()

    async def test_closed_ref_auto_binds_unique_open_outcome_before_approval(self):
        """Real P037 shape: stale focus/ref cannot interrupt deterministic work."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = EffectToolProvider(ToolEffect.MUTATE)
            app = compose_fixture_application(tool_adapters=(tool,))
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "implement, wire, and verify tools", root,
                )
                current = await app.kernel.get_task_spec(task.task_id)
                data = {
                    "schema_version": 1,
                    "goal": task.goal,
                    "scope": ["."],
                    "constraints": [],
                    "outcomes": [
                        {
                            "outcome_id": "tool_definitions",
                            "description": "Create tool definitions",
                            "kind": "WORKSPACE_DELIVERY",
                            "required_effects": ["mutate"],
                            "required": True,
                        },
                        {
                            "outcome_id": "supporting_updates",
                            "description": "Wire supporting files",
                            "kind": "WORKSPACE_DELIVERY",
                            "required_effects": ["mutate"],
                            "required": True,
                        },
                        {
                            "outcome_id": "verification",
                            "description": "Run verification",
                            "kind": "COMMAND_RESULT",
                            "required_effects": ["execute"],
                            "required": True,
                        },
                    ],
                    "continuation_policy": {"mode": "NONE"},
                }
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2, TaskSpecProposal.from_data(data),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, (
                    ("task_spec.revised", {"snapshot": spec.to_data()}),
                    ("task_outcome.state_changed", {
                        "outcome_id": "tool_definitions",
                        "status": TaskOutcomeStatus.DELIVERED.value,
                        "fulfillment_ref": "fixture:definitions:mutate",
                        "reason": "fixture_completed",
                    }),
                ))
                await app.kernel.select_task_outcomes(
                    task.task_id, ("verification",),
                    reason="fixture_stale_verification_focus",
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )

                with self.assertRaises(ApprovalRequired) as caught:
                    await app.kernel.invoke_tool(
                        task.task_id, "turn-p037", ToolCall(
                            "mutate-supporting", tool.name, {},
                            outcome_ref="tool_definitions",
                        ),
                    )

                # Runtime repaired only the bookkeeping edge.  The original R1
                # approval remains mandatory and no tool ran before approval.
                request = caught.exception.request
                self.assertEqual(
                    request.call.outcome_ref, "supporting_updates"
                )
                events = await app.kernel.dependencies.store.read_events(
                    task.task_id
                )
                binding = next(
                    event for event in reversed(events)
                    if event.event_type == "task_outcome.binding_decided"
                )
                self.assertEqual(binding.payload["action"], "CORRECT")
                self.assertEqual(
                    binding.payload["reason"],
                    "UNIQUE_COMPATIBLE_OPEN_OUTCOME",
                )
                self.assertEqual(
                    binding.payload["requested_outcome_id"],
                    "tool_definitions",
                )
                self.assertEqual(
                    binding.payload["bound_outcome_id"],
                    "supporting_updates",
                )
                self.assertFalse(any(
                    event.event_type == "tool.started" for event in events
                ))
            finally:
                await app.registry.stop_all()

    async def test_tool_effects_fulfill_matching_outcomes_and_link_events(self):
        """All project-neutral execution effects use the same Outcome path."""
        for effect, kind, outcome_name in (
            (ToolEffect.OBSERVE, "EVIDENCE", "evidence"),
            (ToolEffect.MUTATE, "WORKSPACE_DELIVERY", "workspace"),
            (ToolEffect.MUTATE, "ARTIFACT_DELIVERY", "artifact"),
            (ToolEffect.EXECUTE, "COMMAND_RESULT", "command"),
            (ToolEffect.CONTROL, "PROCESS_STATE", "process"),
        ):
            with self.subTest(outcome=outcome_name), tempfile.TemporaryDirectory() as directory:
                tool = EffectToolProvider(effect)
                model = AutoBoundEffectModel(tool.name)
                app = compose_fixture_application(
                    model_adapter=model, tool_adapters=(tool,),
                )
                await app.registry.start_all()
                try:
                    root = Path(directory)
                    task = await app.kernel.create_task(
                        f"deliver {effect.value}", root,
                        task_id=f"task-effect-{outcome_name}",
                    )
                    if effect is not ToolEffect.OBSERVE:
                        await app.kernel.set_project_trust(
                            root, ProjectTrustLevel.TRUSTED_FULL
                        )
                    current = await app.kernel.get_task_spec(task.task_id)
                    data = proposal(f"deliver {effect.value}")
                    data["outcomes"][0].update({
                        "outcome_id": f"outcome-{outcome_name}",
                        "description": f"Produce {effect.value} result",
                        "kind": kind,
                        "required_effects": [effect.value],
                    })
                    if effect is not ToolEffect.OBSERVE:
                        data["outcomes"][0].update({
                            "completion_policy": "ATOMIC_ACTION",
                            "atomic_action": {
                                "tool_name": tool.name, "arguments": {},
                            },
                        })
                    spec = TaskSpecSnapshot.from_proposal(
                        task.task_id, 2, TaskSpecProposal.from_data(data),
                        current.acceptance_criteria,
                    )
                    await app.kernel._append_events(task.task_id, ((
                        "task_spec.revised", {"snapshot": spec.to_data()},
                    ),))
                    for state in (
                        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                        TaskState.EXECUTING,
                    ):
                        task = await app.kernel.transition_task(
                            task.task_id, state, state.value
                        )
                    result = await app.kernel.run_agent_turn(
                        task.task_id, task.goal
                    )
                    if effect is not ToolEffect.OBSERVE:
                        self.assertIsInstance(result, AgentTurnSuspended)
                        assert isinstance(result, AgentTurnSuspended)
                        result = await app.kernel.resume_agent_turn(
                            task.task_id, result.approval_request_id,
                            result.payload_hash, ApprovalDecision.APPROVE,
                            "approve fixture effect",
                        )
                    outcome = (
                        await app.kernel.get_task_spec(task.task_id)
                    ).outcomes[0]
                    expected = (
                        TaskOutcomeStatus.IN_PROGRESS
                        if kind == "EVIDENCE"
                        else TaskOutcomeStatus.DELIVERED
                    )
                    self.assertEqual(outcome.status, expected)
                    self.assertTrue(outcome.fulfillment_refs[0].endswith(
                        f":{effect.value}"
                    ))

                    events = await app.kernel.dependencies.store.read_events(
                        task.task_id
                    )
                    linked = {
                        "tool.requested": lambda event: event.payload["call"].get(
                            "outcome_ref"
                        ),
                        "policy.evaluated": lambda event: event.payload.get(
                            "outcome_ref"
                        ),
                        "tool.prepared": lambda event: event.payload["call"].get(
                            "outcome_ref"
                        ),
                        "tool.started": lambda event: event.payload["call"].get(
                            "outcome_ref"
                        ),
                        "tool.completed": lambda event: event.payload.get(
                            "outcome_ref"
                        ),
                    }
                    for event_type, reference in linked.items():
                        event = next(
                            item for item in events
                            if item.event_type == event_type
                        )
                        self.assertEqual(
                            reference(event), outcome.outcome_id,
                            event_type,
                        )
                    if effect is not ToolEffect.OBSERVE:
                        requested = next(
                            item for item in events
                            if item.event_type == "approval.requested"
                        )
                        resolved = next(
                            item for item in events
                            if item.event_type == "approval.resolved"
                        )
                        self.assertEqual(
                            requested.payload["call"]["outcome_ref"],
                            outcome.outcome_id,
                        )
                        self.assertEqual(
                            resolved.payload["outcome_ref"], outcome.outcome_id
                        )
                finally:
                    await app.registry.stop_all()

    async def test_explicit_outcome_stays_open_until_completion_request(self):
        """One successful action is progress, not multi-step completion."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                tool_adapters=(CoreReadOnlyToolProvider(),),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("inspect two files", root)
                current = await app.kernel.get_task_spec(task.task_id)
                data = proposal(task.goal)
                data["outcomes"][0].update({
                    "outcome_id": "inspection",
                    "kind": "COMMAND_RESULT",
                    "description": "Complete the full inspection",
                })
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2, TaskSpecProposal.from_data(data),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                await app.kernel.invoke_tool(
                    task.task_id, "turn-explicit", ToolCall(
                        "inspect-one", "core.list_files", {"path": "."},
                        outcome_ref="inspection",
                    ),
                )
                progress = (await app.kernel.get_task_spec(task.task_id)).outcomes[0]
                self.assertEqual(progress.status, TaskOutcomeStatus.IN_PROGRESS)

                rejected = await app.kernel.request_task_outcome_completion(
                    task.task_id, "inspection",
                    completion_summary="The first inspection step completed",
                    evidence_refs=progress.fulfillment_refs,
                    remaining_work=("Inspect the second target",),
                    writer="test-explicit-rejected",
                )
                self.assertFalse(rejected["accepted"])
                self.assertEqual(
                    rejected["completion_gaps"][0]["kind"], "REMAINING_WORK"
                )
                self.assertEqual(
                    (await app.kernel.get_task_spec(task.task_id)).outcomes[0].status,
                    TaskOutcomeStatus.IN_PROGRESS,
                )

                accepted = await app.kernel.request_task_outcome_completion(
                    task.task_id, "inspection",
                    completion_summary="All declared inspection work completed",
                    evidence_refs=progress.fulfillment_refs, remaining_work=(),
                    writer="test-explicit-accepted",
                )
                self.assertTrue(accepted["accepted"])
                self.assertEqual(
                    (await app.kernel.get_task_spec(task.task_id)).outcomes[0].status,
                    TaskOutcomeStatus.DELIVERED,
                )
                events = await app.kernel.dependencies.store.read_events(
                    task.task_id
                )
                statuses = [
                    item.payload.get("status") for item in events
                    if item.event_type == "task_outcome.state_changed"
                    and item.payload.get("outcome_id") == "inspection"
                ]
                self.assertIn("COMPLETION_REQUESTED", statuses)
            finally:
                await app.registry.stop_all()

    async def test_agent_loop_uses_explicit_completion_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=ExplicitCompletionModel(),
                tool_adapters=(
                    CoreReadOnlyToolProvider(), CoreTaskSpecToolProvider(),
                ),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "inspect workspace", Path(directory)
                )
                current = await app.kernel.get_task_spec(task.task_id)
                data = proposal(task.goal)
                data["outcomes"][0].update({
                    "outcome_id": "inspection",
                    "kind": "COMMAND_RESULT",
                    "description": "Complete workspace inspection",
                })
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2, TaskSpecProposal.from_data(data),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                result = await app.kernel.run_agent_turn(task.task_id, task.goal)
                self.assertEqual(result.tool_calls, 2)
                events = await app.kernel.dependencies.store.read_events(task.task_id)
                executions = tuple(
                    (await app.kernel.get_task(task.task_id)).tool_executions.values()
                )
                complete_execution = next((
                    item for item in executions
                    if item.call.name == "core.task_outcome_complete"
                ), None)
                self.assertIsNotNone(
                    complete_execution, {
                        "executions": [item.call.name for item in executions],
                        "messages": [item.to_data() for item in result.messages],
                    }
                )
                assert complete_execution is not None
                self.assertTrue(complete_execution.result.ok)
                outcome = (await app.kernel.get_task_spec(task.task_id)).outcomes[0]
                self.assertEqual(
                    outcome.status, TaskOutcomeStatus.DELIVERED,
                    complete_execution.result.data,
                )
                self.assertTrue(any(
                    item.event_type == "task_outcome.completion_requested"
                    for item in events
                ))
            finally:
                await app.registry.stop_all()

    async def test_legacy_required_effects_stays_open_after_one_tool(self):
        """Old Task streams keep their facts but gain safe completion semantics."""
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                tool_adapters=(CoreReadOnlyToolProvider(),),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "legacy inspection", Path(directory)
                )
                current = await app.kernel.get_task_spec(task.task_id)
                data = proposal(task.goal)
                data["outcomes"][0].update({
                    "kind": "COMMAND_RESULT",
                    "completion_policy": "REQUIRED_EFFECTS",
                })
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2, TaskSpecProposal.from_data(data),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                await app.kernel.invoke_tool(
                    task.task_id, "turn-legacy", ToolCall(
                        "legacy-read", "core.list_files", {"path": "."},
                        outcome_ref="inspect",
                    ),
                )
                outcome = (await app.kernel.get_task_spec(task.task_id)).outcomes[0]
                self.assertEqual(outcome.status, TaskOutcomeStatus.IN_PROGRESS)
                self.assertEqual(
                    outcome.completion_policy.value, "REQUIRED_EFFECTS"
                )
            finally:
                await app.registry.stop_all()

    async def test_user_decision_fulfills_interact_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=UserDecisionModel(), tool_adapters=(),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "obtain user decision", Path(directory)
                )
                current = await app.kernel.get_task_spec(task.task_id)
                data = proposal("obtain user decision")
                data["outcomes"][0].update({
                    "outcome_id": "decision",
                    "description": "Record a material user choice",
                    "kind": "USER_DECISION",
                    "required_effects": ["interact"],
                })
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2, TaskSpecProposal.from_data(data),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                suspended = await app.kernel.run_agent_turn(
                    task.task_id, task.goal
                )
                self.assertIsInstance(suspended, AgentClarificationSuspended)
                assert isinstance(suspended, AgentClarificationSuspended)
                await app.kernel.resolve_agent_clarification(
                    suspended.request_id, suspended.resume_token, "one"
                )
                outcome = (
                    await app.kernel.get_task_spec(task.task_id)
                ).outcomes[0]
                self.assertEqual(outcome.status, TaskOutcomeStatus.DELIVERED)
                self.assertTrue(outcome.fulfillment_refs[0].endswith(":interact"))
                events = await app.kernel.dependencies.store.read_events(
                    task.task_id
                )
                requested = next(
                    event for event in events
                    if event.event_type == "clarification.requested"
                )
                self.assertEqual(requested.payload["outcome_ref"], "decision")
            finally:
                await app.registry.stop_all()

    async def test_failed_tool_does_not_fulfill_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            app, task = await self._prepared_app(Path(directory))
            try:
                result = await app.kernel.invoke_tool(
                    task.task_id, "turn-failed-outcome", ToolCall(
                        "failed-outcome", "core.read_file",
                        {"path": "missing.txt"}, outcome_ref="inspect",
                    ),
                )
                self.assertFalse(result.ok)
                outcome = (
                    await app.kernel.get_task_spec(task.task_id)
                ).outcomes[0]
                self.assertEqual(outcome.status, TaskOutcomeStatus.IN_PROGRESS)
                self.assertFalse(outcome.fulfillment_refs)
                failed = next(
                    event for event in
                    await app.kernel.dependencies.store.read_events(task.task_id)
                    if event.event_type == "tool.failed"
                )
                self.assertEqual(failed.payload["outcome_ref"], "inspect")
            finally:
                await app.registry.stop_all()

    async def test_multiple_calls_share_analysis_outcome_until_final_acceptance(self):
        """Regression for a broad analysis needing more than one observation."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            app = compose_fixture_application(
                model_adapter=SharedOutcomeBatchModel(),
                tool_adapters=(CoreReadOnlyToolProvider(),),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("analyze project", root)
                current = await app.kernel.get_task_spec(task.task_id)
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2,
                    TaskSpecProposal.from_data(proposal("analyze project")),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                turn = await app.kernel.run_agent_turn(task.task_id, task.goal)
                self.assertEqual(turn.tool_calls, 2)
                before_verify = (
                    await app.kernel.get_task_spec(task.task_id)
                ).outcomes[0]
                self.assertEqual(
                    before_verify.status, TaskOutcomeStatus.IN_PROGRESS
                )
                self.assertEqual(len(before_verify.fulfillment_refs), 2)

                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify analysis"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertTrue(verification.passed)
                delivered = (
                    await app.kernel.get_task_spec(task.task_id)
                ).outcomes[0]
                self.assertEqual(delivered.status, TaskOutcomeStatus.DELIVERED)
            finally:
                await app.registry.stop_all()

    async def test_completed_unit_cannot_split_an_open_tool_batch(self):
        """Regression for the real two-call Provider protocol failure."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = CrossOutcomeBatchModel()
            app = compose_fixture_application(
                model_adapter=model,
                tool_adapters=(CoreReadOnlyToolProvider(),),
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("inspect two units", root)
                current = await app.kernel.get_task_spec(task.task_id)
                data = proposal(
                    task.goal, ("inspect-a", "inspect-b"),
                    "AFTER_COMPLETED_UNIT",
                )
                for item in data["outcomes"]:
                    item["kind"] = "COMMAND_RESULT"
                    item["completion_policy"] = "ATOMIC_ACTION"
                    item["atomic_action"] = {
                        "tool_name": "core.list_files",
                        "arguments": {"path": "."},
                    }
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2, TaskSpecProposal.from_data(data),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                turn = await app.kernel.run_agent_turn(task.task_id, task.goal)
                self.assertEqual(turn.tool_calls, 2)
                self.assertEqual(len(model.requests), 2)
                second = model.requests[1].messages
                assistant_index = next(
                    index for index, message in enumerate(second)
                    if message.message_id == "cross-outcome-batch"
                )
                following = second[assistant_index + 1:assistant_index + 3]
                self.assertEqual(
                    [message.role for message in following],
                    [MessageRole.TOOL, MessageRole.TOOL],
                )
                self.assertEqual({
                    block.result.call_id for message in following
                    for block in message.content
                    if isinstance(block, ToolResultBlock)
                }, {"close-first", "run-second"})
            finally:
                await app.registry.stop_all()

    async def test_legacy_pending_call_can_reopen_prematurely_closed_outcome(self):
        """An accepted pre-fix batch survives upgrade without new authority."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            app, task = await self._prepared_app(root)
            try:
                first = await app.kernel.invoke_tool(
                    task.task_id, "legacy-turn", ToolCall(
                        "legacy-first", "core.find_files",
                        {"path": ".", "pattern": "pyproject.toml"},
                        outcome_ref="inspect",
                    ),
                )
                self.assertTrue(first.ok)
                # Recreate the old event shape: the first observation in one
                # accepted model batch incorrectly closed the broad Outcome.
                await app.kernel._append_events(task.task_id, ((
                    "task_outcome.state_changed", {
                        "outcome_id": "inspect",
                        "status": TaskOutcomeStatus.DELIVERED.value,
                        "fulfillment_ref": "legacy:first:observe",
                        "reason": "legacy_premature_close",
                    },
                ),))
                with self.assertRaisesRegex(
                    Exception, "OUTCOME_ALREADY_CLOSED"
                ):
                    await app.kernel.invoke_tool(
                        task.task_id, "new-turn", ToolCall(
                            "new-call", "core.find_files",
                            {"path": ".", "pattern": "*.py"},
                            outcome_ref="inspect",
                        ),
                    )

                resumed = await app.kernel.invoke_tool(
                    task.task_id, "legacy-turn", ToolCall(
                        "legacy-second", "core.find_files",
                        {"path": ".", "pattern": "*.py"},
                        outcome_ref="inspect",
                    ), allow_closed_outcome_ref=True,
                )
                self.assertTrue(resumed.ok)
                outcome = (
                    await app.kernel.get_task_spec(task.task_id)
                ).outcomes[0]
                self.assertEqual(outcome.status, TaskOutcomeStatus.IN_PROGRESS)
                self.assertGreaterEqual(len(outcome.fulfillment_refs), 2)
            finally:
                await app.registry.stop_all()

    async def test_timeout_keeps_active_outcome_across_sqlite_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            slow = EffectToolProvider(ToolEffect.OBSERVE, delay=0.05)
            first = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(slow,),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await first.registry.start_all()
            task = await first.kernel.create_task("observe slowly", root)
            current = await first.kernel.get_task_spec(task.task_id)
            data = proposal("observe slowly")
            spec = TaskSpecSnapshot.from_proposal(
                task.task_id, 2, TaskSpecProposal.from_data(data),
                current.acceptance_criteria,
            )
            await first.kernel._append_events(task.task_id, ((
                "task_spec.revised", {"snapshot": spec.to_data()},
            ),))
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await first.kernel.transition_task(
                    task.task_id, state, state.value
                )
            result = await first.kernel.invoke_tool(
                task.task_id, "turn-timeout", ToolCall(
                    "slow-call", slow.name, {}, outcome_ref="inspect",
                ), timeout_seconds=0.001,
            )
            self.assertEqual(result.error_code, "TIMEOUT")
            timed_out = (await first.kernel.get_task_spec(task.task_id)).outcomes[0]
            self.assertEqual(timed_out.status, TaskOutcomeStatus.IN_PROGRESS)
            self.assertFalse(timed_out.fulfillment_refs)
            await first.registry.stop_all()

            second = compose_fixture_application(
                model_adapter=EchoModelProvider(),
                tool_adapters=(EffectToolProvider(ToolEffect.OBSERVE),),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await second.registry.start_all()
            try:
                restored = (
                    await second.kernel.get_task_spec(task.task_id)
                ).outcomes[0]
                self.assertEqual(restored.status, TaskOutcomeStatus.IN_PROGRESS)
                self.assertFalse(restored.fulfillment_refs)
            finally:
                await second.registry.stop_all()

    async def test_replace_replans_new_outcomes_without_carrying_old_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = FixturePlanner(data=proposal("replacement goal", (
                "replacement-evidence",
            )))
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                task_spec_planner_adapter=planner,
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("old goal", Path(directory))
                current = await app.kernel.get_task_spec(task.task_id)
                old = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2,
                    TaskSpecProposal.from_data(proposal("old goal", ("old",))),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": old.to_data()},
                ),))
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                from tsm_agt.core import AgentTurnCheckpoint, SteeringKind
                checkpoint = AgentTurnCheckpoint(
                    task.task_id, "turn-replace-plan", 1,
                    (Message("replace-user", MessageRole.USER,
                             (TextBlock("old goal"),)),),
                    (), (), 0, 0, 0, 0, 5, 5, 256, 10.0,
                )
                checkpoint = await app.kernel._bind_agent_checkpoint(
                    task, checkpoint, await app.kernel.list_tools()
                )
                await app.kernel._save_agent_checkpoint(checkpoint, "test")
                await app.kernel.queue_steering(
                    task.task_id, SteeringKind.REPLACE,
                    "replacement goal", "replace-plan",
                )
                checkpoint, replaced = await app.kernel._apply_pending_steering(
                    checkpoint, "test"
                )
                self.assertTrue(replaced)
                cleared = await app.kernel.get_task_spec(task.task_id)
                self.assertFalse(cleared.outcomes)
                planned = await app.kernel.plan_task_spec(task.task_id)
                self.assertEqual(planned.goal, "replacement goal")
                self.assertEqual(
                    [item.outcome_id for item in planned.outcomes],
                    ["replacement-evidence"],
                )
                self.assertNotIn("old", [
                    item.outcome_id for item in planned.outcomes
                ])
            finally:
                await app.registry.stop_all()

    async def test_promise_text_cannot_satisfy_workspace_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            app, task = await self._prepared_app(Path(directory))
            try:
                await app.kernel._append_events(task.task_id, (
                    ("llm.completed", {
                        "turn_id": "turn-promise",
                        "message": {
                            "message_id": "promise", "role": "assistant",
                            "content": [{
                                "type": "text",
                                "text": "确认后我会开始读取并完成修改。",
                            }],
                        },
                        "finish_reason": "stop",
                    }),
                    ("turn.completed", {"turn_id": "turn-promise"}),
                ))
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify promise"
                )
                result = await app.kernel.verify_task_acceptance(task.task_id)
                criterion = next(
                    item for item in result.criteria
                    if item.criterion_id == "task-outcome-fulfillment"
                )
                self.assertEqual(criterion.status, AcceptanceStatus.BLOCKED)
                self.assertFalse(result.passed)
            finally:
                await app.registry.stop_all()

    async def test_completed_unit_waits_and_semantically_resumes_same_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = TwoUnitModel()
            app = compose_fixture_application(
                model_adapter=model,
                tool_adapters=(CoreReadOnlyToolProvider(),),
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("two units")
                task = await app.kernel.create_task(
                    "inspect two units", root, session_id=session.session_id
                )
                current = await app.kernel.get_task_spec(task.task_id)
                data = proposal(
                    "inspect two units", ("inspect-a", "inspect-b"),
                    "AFTER_COMPLETED_UNIT",
                )
                for item in data["outcomes"]:
                    item["kind"] = "COMMAND_RESULT"
                    item["completion_policy"] = "ATOMIC_ACTION"
                    item["atomic_action"] = {
                        "tool_name": "core.list_files",
                        "arguments": {"path": "."},
                    }
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2, TaskSpecProposal.from_data(data),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                first = await app.kernel.run_agent_turn(
                    task.task_id, "inspect both"
                )
                self.assertIsInstance(first, AgentContinuationSuspended)
                assert isinstance(first, AgentContinuationSuspended)
                waiting = await app.kernel.get_task(task.task_id)
                self.assertEqual(waiting.state, TaskState.AWAITING_USER)
                checkpoint = waiting.active_agent_checkpoint or {}
                self.assertEqual(
                    checkpoint["pending_user_action"]["kind"], "CONTINUATION"
                )
                decision = await app.kernel.resolve_session_continuation(
                    session.session_id, root, task_id=task.task_id
                )
                self.assertEqual(
                    decision.mode, SessionContinuationMode.RESUME_CONTINUATION
                )
                completed = await app.kernel.resume_agent_continuation(
                    task.task_id, "continue with the remaining unit",
                    input_id="input-resume-unit",
                )
                self.assertNotIsInstance(completed, AgentContinuationSuspended)
                outcomes = (await app.kernel.get_task_spec(task.task_id)).outcomes
                self.assertTrue(all(outcome.status.is_closed for outcome in outcomes))
                live = await app.kernel.get_task(task.task_id)
                self.assertEqual(live.state, TaskState.EXECUTING)
                self.assertEqual(
                    (live.active_agent_checkpoint or {}).get(
                        "pending_user_action", {}
                    ), {},
                )
            finally:
                await app.registry.stop_all()

    async def test_continuation_and_outcomes_survive_sqlite_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first = compose_fixture_application(
                model_adapter=TwoUnitModel(),
                tool_adapters=(CoreReadOnlyToolProvider(),),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await first.registry.start_all()
            session = await first.kernel.create_session("restart units")
            task = await first.kernel.create_task(
                "inspect after restart", root, session_id=session.session_id
            )
            current = await first.kernel.get_task_spec(task.task_id)
            data = proposal(
                "inspect after restart", ("inspect-a", "inspect-b"),
                "AFTER_COMPLETED_UNIT",
            )
            for item in data["outcomes"]:
                item["kind"] = "COMMAND_RESULT"
                item["completion_policy"] = "ATOMIC_ACTION"
                item["atomic_action"] = {
                    "tool_name": "core.list_files",
                    "arguments": {"path": "."},
                }
            spec = TaskSpecSnapshot.from_proposal(
                task.task_id, 2, TaskSpecProposal.from_data(data),
                current.acceptance_criteria,
            )
            await first.kernel._append_events(task.task_id, ((
                "task_spec.revised", {"snapshot": spec.to_data()},
            ),))
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await first.kernel.transition_task(
                    task.task_id, state, state.value
                )
            suspended = await first.kernel.run_agent_turn(
                task.task_id, "inspect both"
            )
            self.assertIsInstance(suspended, AgentContinuationSuspended)
            await first.registry.stop_all()

            second = compose_fixture_application(
                model_adapter=TwoUnitModel(),
                tool_adapters=(CoreReadOnlyToolProvider(),),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await second.registry.start_all()
            try:
                restored = await second.kernel.get_task(task.task_id)
                self.assertEqual(restored.state, TaskState.AWAITING_USER)
                self.assertEqual(
                    restored.active_agent_checkpoint["pending_user_action"]
                    ["kind"],
                    "CONTINUATION",
                )
                outcomes = (
                    await second.kernel.get_task_spec(task.task_id)
                ).outcomes
                self.assertEqual(outcomes[0].status, TaskOutcomeStatus.DELIVERED)
                self.assertEqual(outcomes[1].status, TaskOutcomeStatus.PENDING)
                result = await second.kernel.resume_agent_continuation(
                    task.task_id, "finish the remaining unit",
                    input_id="restart-continuation",
                )
                self.assertNotIsInstance(result, AgentContinuationSuspended)
                self.assertTrue(all(
                    outcome.status.is_closed for outcome in
                    (await second.kernel.get_task_spec(task.task_id)).outcomes
                ))
            finally:
                await second.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
