from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    TASK_SPEC_PROPOSAL_SCHEMA_V1,
    AcceptanceStatus, AgentTurnCheckpoint, ProjectTrustLevel, SteeringKind,
    TaskAcceptanceCriterion, TaskContinuationMode, TaskCriterionKind,
    TaskOutcomeKind, TaskOutcomeCompletionPolicy, TaskOutcomeStatus,
    TaskSpecProjector, TaskSpecProposal,
    TaskSpecSnapshot,
    TaskExecutionFocusProjector, TaskOutcomeEligibilityCalculator,
    TaskState, canonical_hash,
)
from tsm_agt.ports import Message, MessageRole, TextBlock, ToolCall, ToolEffect


async def move(application, task_id, states):
    task = await application.kernel.get_task(task_id)
    for state in states:
        task = await application.kernel.transition_task(task_id, state, state.value)
    return task


class TaskSpecKernelTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _delivery_proposal(goal: str = "fix and verify") -> TaskSpecProposal:
        return TaskSpecProposal.from_data({
            "schema_version": 1,
            "goal": goal,
            "scope": ["src"],
            "constraints": ["preserve compatibility"],
            "outcomes": [{
                "outcome_id": "deliver-fix",
                "description": "Implement the requested fix",
                "kind": "WORKSPACE_DELIVERY",
                "required_effects": ["observe", "mutate", "execute"],
                "required": True,
            }],
            "continuation_policy": {"mode": "AFTER_COMPLETED_UNIT"},
        })

    async def _install_proposal_spec(self, app, task_id: str, goal: str):
        current = await app.kernel.get_task_spec(task_id)
        candidate = TaskSpecSnapshot.from_proposal(
            task_id, current.revision + 1, self._delivery_proposal(goal),
            current.acceptance_criteria,
        )
        await app.kernel._append_events(task_id, ((
            "task_spec.revised", {"snapshot": candidate.to_data()},
        ),))
        return candidate

    def test_proposal_protocol_rejects_runtime_authority_fields(self):
        data = {
            "schema_version": 1,
            "goal": "fix and verify",
            "scope": ["src"],
            "constraints": ["preserve compatibility"],
            "outcomes": [{
                "outcome_id": "deliver-fix",
                "description": "Implement the requested fix",
                "kind": "WORKSPACE_DELIVERY",
                "required_effects": ["observe", "mutate", "execute"],
                "required": True,
                "status": "DELIVERED",
            }],
            "continuation_policy": {"mode": "NONE"},
        }
        with self.assertRaisesRegex(ValueError, "unknown fields: status"):
            TaskSpecProposal.from_data(data)
        self.assertNotIn("task_id", TASK_SPEC_PROPOSAL_SCHEMA_V1["properties"])
        self.assertNotIn("status", (
            TASK_SPEC_PROPOSAL_SCHEMA_V1["properties"]["outcomes"]
            ["items"]["properties"]
        ))

    def test_proposal_becomes_pending_runtime_snapshot_and_round_trips(self):
        proposal = TaskSpecProposal.from_data({
            "schema_version": 1,
            "goal": "fix and verify",
            "scope": ["src"],
            "constraints": ["preserve compatibility"],
            "outcomes": [{
                "outcome_id": "deliver-fix",
                "description": "Implement the requested fix",
                "kind": "WORKSPACE_DELIVERY",
                "required_effects": ["observe", "mutate", "execute"],
                "required": True,
            }],
            "continuation_policy": {"mode": "AFTER_COMPLETED_UNIT"},
        })
        spec = TaskSpecSnapshot.from_proposal(
            "task-proposal", 1, proposal,
            (TaskAcceptanceCriterion(
                "workspace-integrity", "workspace remains consistent",
                TaskCriterionKind.WORKSPACE_INTEGRITY,
            ),),
        )
        self.assertEqual(spec.outcomes[0].status, TaskOutcomeStatus.PENDING)
        self.assertEqual(
            spec.outcomes[0].required_effects,
            (ToolEffect.OBSERVE, ToolEffect.MUTATE, ToolEffect.EXECUTE),
        )
        self.assertEqual(
            spec.continuation_mode, TaskContinuationMode.AFTER_COMPLETED_UNIT
        )
        restored = TaskSpecSnapshot.from_data(spec.to_data())
        self.assertEqual(restored, spec)
        self.assertEqual(restored.content_hash, spec.content_hash)

    def test_optional_outcome_is_eligible_but_not_initially_selected(self):
        data = self._delivery_proposal().to_data()
        data["outcomes"].append({
            "outcome_id": "optional-validation",
            "description": "Run optional validation",
            "kind": "COMMAND_RESULT",
            "required_effects": ["execute"],
            "required": False,
        })
        proposal = TaskSpecProposal.from_data(data)
        spec = TaskSpecSnapshot.from_proposal(
            "task-focus", 1, proposal,
            (TaskAcceptanceCriterion(
                "workspace-integrity", "workspace remains consistent",
                TaskCriterionKind.WORKSPACE_INTEGRITY,
            ),),
        )
        self.assertEqual(
            [item.outcome_id for item in TaskOutcomeEligibilityCalculator.eligible(spec)],
            ["deliver-fix", "optional-validation"],
        )
        focus = TaskExecutionFocusProjector.project(spec, ())
        self.assertEqual(focus.selected_outcome_ids, ("deliver-fix",))
        migrated = TaskExecutionFocusProjector.project(
            spec, (), legacy_active_outcome_ids=("optional-validation",)
        )
        self.assertEqual(
            migrated.selected_outcome_ids, ("optional-validation",)
        )
        self.assertEqual(
            migrated.selection_reason, "legacy_active_outcome_migration"
        )

    def test_outcome_dependency_cycle_is_rejected(self):
        data = self._delivery_proposal().to_data()
        data["outcomes"][0]["depends_on"] = ["verify"]
        data["outcomes"].append({
            "outcome_id": "verify",
            "description": "Verify delivery",
            "kind": "COMMAND_RESULT",
            "required_effects": ["execute"],
            "required": True,
            "depends_on": ["deliver-fix"],
        })
        with self.assertRaisesRegex(ValueError, "contain a cycle"):
            TaskSpecProposal.from_data(data)

    async def test_optional_outcome_can_be_selected_structurally(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("implement and validate", Path(directory))
                current = await app.kernel.get_task_spec(task.task_id)
                data = self._delivery_proposal(task.goal).to_data()
                data["outcomes"].append({
                    "outcome_id": "optional-validation",
                    "description": "Run optional validation",
                    "kind": "COMMAND_RESULT",
                    "required_effects": ["execute"],
                    "required": False,
                })
                spec = TaskSpecSnapshot.from_proposal(
                    task.task_id, 2, TaskSpecProposal.from_data(data),
                    current.acceptance_criteria,
                )
                await app.kernel._append_events(task.task_id, ((
                    "task_spec.revised", {"snapshot": spec.to_data()},
                ),))
                focus = await app.kernel.select_task_outcomes(
                    task.task_id, ("optional-validation",),
                    reason="model selected user's requested next unit",
                    source_input_id="input-1",
                )
                self.assertEqual(
                    focus.selected_outcome_ids, ("optional-validation",)
                )
                restored = await app.kernel.get_task_execution_focus(task.task_id)
                self.assertEqual(restored, focus)
            finally:
                await app.registry.stop_all()

    def test_chained_answer_outcomes_merge_into_one_deliverable(self):
        """One user question yields one conversational Outcome, not a chain.

        The real planner split a single request into "analyse the route issue"
        and "recommend a fix", the second depending on the first. Kept apart,
        every read-only observation is compatible with two open ANSWER outcomes
        at once, so Runtime must either guess or interrupt the turn.
        """
        proposal = TaskSpecProposal.from_data({
            "schema_version": 1,
            "goal": "analyse the route issue and recommend a fix",
            "scope": ["src"], "constraints": [],
            "outcomes": [
                {
                    "outcome_id": "route_issue_analysis",
                    "description": "Give the route root-cause analysis",
                    "kind": "ANSWER", "required_effects": ["observe"],
                    "required": True,
                },
                {
                    "outcome_id": "route_fix_recommendation",
                    "description": "Give the route fix recommendation",
                    "kind": "ANSWER", "required_effects": ["observe"],
                    "required": True,
                    "depends_on": ["route_issue_analysis"],
                },
            ],
            "continuation_policy": {"mode": "NONE"},
        })
        self.assertEqual(len(proposal.outcomes), 1)
        merged = proposal.outcomes[0]
        self.assertEqual(merged.outcome_id, "route_issue_analysis")
        self.assertIs(merged.kind, TaskOutcomeKind.ANSWER)
        self.assertEqual(merged.required_effects, (ToolEffect.OBSERVE,))
        self.assertTrue(merged.required)
        # The internal ordering of the group is gone, not left as a self-loop.
        self.assertEqual(merged.depends_on, ())
        # Neither requested part is silently dropped from the deliverable.
        self.assertIn("root-cause analysis", merged.description)
        self.assertIn("fix recommendation", merged.description)
        # The normalised form round-trips unchanged.
        self.assertEqual(
            TaskSpecProposal.from_data(proposal.to_data()), proposal
        )

    def test_merging_repoints_dependents_and_keeps_other_kinds_separate(self):
        """Only conversational answers merge; other deliverables stay intact."""
        proposal = TaskSpecProposal.from_data({
            "schema_version": 1,
            "goal": "analyse and then deliver a patch",
            "scope": ["src"], "constraints": [],
            "outcomes": [
                {
                    "outcome_id": "inspect",
                    "description": "Collect the required evidence",
                    "kind": "EVIDENCE", "required_effects": ["observe"],
                    "required": True,
                },
                {
                    "outcome_id": "answer-analysis",
                    "description": "Answer the analysis part",
                    "kind": "ANSWER", "required_effects": ["observe"],
                    "required": True,
                },
                {
                    "outcome_id": "answer-summary",
                    "description": "Answer the summary part",
                    "kind": "ANSWER", "required_effects": ["observe"],
                    "required": False,
                    "depends_on": ["answer-analysis"],
                },
                {
                    "outcome_id": "deliver-patch",
                    "description": "Write the requested patch",
                    "kind": "WORKSPACE_DELIVERY",
                    "required_effects": ["mutate"],
                    "required": True,
                    "depends_on": ["answer-summary", "inspect"],
                },
            ],
            "continuation_policy": {"mode": "NONE"},
        })
        by_id = {item.outcome_id: item for item in proposal.outcomes}
        self.assertEqual(
            sorted(by_id), ["answer-analysis", "deliver-patch", "inspect"]
        )
        # A required EVIDENCE deliverable is never absorbed into the answer.
        self.assertIs(by_id["inspect"].kind, TaskOutcomeKind.EVIDENCE)
        # The optional answer contributed its obligation to the merged answer.
        self.assertTrue(by_id["answer-analysis"].required)
        # The dependent deliverable now waits on the merged answer and evidence.
        self.assertEqual(
            by_id["deliver-patch"].depends_on, ("answer-analysis", "inspect")
        )

    def test_atomic_action_answer_is_never_merged(self):
        """A pinned action contract keeps its own acceptance semantics."""
        proposal = TaskSpecProposal.from_data({
            "schema_version": 1,
            "goal": "explain the config and write the report file",
            "scope": ["src"], "constraints": [],
            "outcomes": [
                {
                    "outcome_id": "explain",
                    "description": "Explain the configuration",
                    "kind": "ANSWER", "required_effects": [],
                    "required": True,
                },
                {
                    "outcome_id": "write-report",
                    "description": "Write the report with one exact call",
                    "kind": "ANSWER",
                    "required_effects": [], "required": True,
                    "completion_policy": "ATOMIC_ACTION",
                    "atomic_action": {
                        "tool_name": "core.apply_patch",
                        "arguments": {"path": "REPORT.md"},
                    },
                },
            ],
            "continuation_policy": {"mode": "NONE"},
        })
        self.assertEqual(
            [item.outcome_id for item in proposal.outcomes],
            ["explain", "write-report"],
        )
        self.assertIs(
            proposal.outcomes[1].completion_policy,
            TaskOutcomeCompletionPolicy.ATOMIC_ACTION,
        )

    def test_merged_answer_description_stays_within_bounds(self):
        """Merging respects the bounded description the schema declares."""
        proposal = TaskSpecProposal.from_data({
            "schema_version": 1,
            "goal": "many answers",
            "scope": [], "constraints": [],
            "outcomes": [
                {
                    "outcome_id": f"answer-{index}",
                    "description": f"Deliverable part {index} " + "x" * 400,
                    "kind": "ANSWER", "required_effects": [],
                    "required": True,
                }
                for index in range(4)
            ],
            "continuation_policy": {"mode": "NONE"},
        })
        self.assertEqual(len(proposal.outcomes), 1)
        self.assertLessEqual(len(proposal.outcomes[0].description), 1000)
        self.assertTrue(proposal.outcomes[0].description.endswith("…"))

    def test_duplicate_outcome_ids_are_still_rejected(self):
        """Normalisation must not quietly repair a malformed proposal."""
        with self.assertRaises(ValueError):
            TaskSpecProposal.from_data({
                "schema_version": 1,
                "goal": "duplicate identities",
                "scope": [], "constraints": [],
                "outcomes": [
                    {
                        "outcome_id": "same",
                        "description": "First answer",
                        "kind": "ANSWER", "required_effects": [],
                        "required": True,
                    },
                    {
                        "outcome_id": "same",
                        "description": "Second answer",
                        "kind": "ANSWER", "required_effects": [],
                        "required": True,
                    },
                ],
                "continuation_policy": {"mode": "NONE"},
            })

    def test_legacy_snapshot_replays_with_original_hash(self):
        legacy = {
            "task_id": "task-legacy", "revision": 1,
            "goal": "legacy goal", "scope": [], "constraints": [],
            "acceptance_criteria": [{
                "criterion_id": "workspace-integrity",
                "description": "Committed workspace effects still match the mutation journal",
                "verification_kind": "workspace_integrity",
                "evidence_reference": None,
            }],
        }
        legacy["content_hash"] = canonical_hash(legacy)
        restored = TaskSpecSnapshot.from_data(legacy)
        self.assertEqual(restored.schema_version, 0)
        self.assertFalse(restored.outcomes)
        self.assertEqual(restored.content_hash, legacy["content_hash"])

    async def test_prompt_contains_authoritative_primary_workspace_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = EchoModelProvider()
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=()
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "inspect another repository if the user asks", root
                )
                task = await move(app, task.task_id, (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ))
                message = await app.kernel._task_spec_context_message(task.task_id)
                self.assertEqual(message.role, MessageRole.SYSTEM)
                self.assertIn(str(root.resolve()), message.text)
                self.assertIn("relative_path_base", message.text)
                self.assertIn("not evidence", message.text)
                self.assertIn("Task-scoped approval", message.text)
            finally:
                await app.registry.stop_all()

    async def _verification_for_persisted_answer(
        self, answer_content: list[dict[str, object]],
    ):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        app = compose_fixture_application(tool_adapters=())
        await app.registry.start_all()
        task = await app.kernel.create_task("verify final answer", root)
        task = await move(app, task.task_id, (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ))
        await app.kernel._append_events(task.task_id, (
            ("llm.completed", {
                "turn_id": "turn-answer",
                "message": {
                    "message_id": "assistant-answer",
                    "role": "assistant",
                    "content": answer_content,
                },
                "finish_reason": "stop",
            }),
            ("turn.completed", {"turn_id": "turn-answer"}),
        ))
        await app.kernel.transition_task(
            task.task_id, TaskState.VERIFYING, "verify"
        )
        return directory, app, await app.kernel.verify_task_acceptance(task.task_id)

    async def test_answer_completeness_accepts_user_facing_text(self):
        directory, app, verification = await self._verification_for_persisted_answer([
            {"type": "text", "text": "The requested analysis is complete."}
        ])
        try:
            criterion = next(
                item for item in verification.criteria
                if item.criterion_id == "answer-completeness"
            )
            self.assertEqual(criterion.status, AcceptanceStatus.PASSED)
            self.assertEqual(verification.status, AcceptanceStatus.PASSED)
        finally:
            await app.registry.stop_all()
            directory.cleanup()

    async def test_workspace_integrity_cannot_hide_protocol_as_final_answer(self):
        markup = (
            '<tool_use name="core__read_file" id="call-final">'
            '{"path":"screen.tsx"}</tool_use>'
        )
        directory, app, verification = await self._verification_for_persisted_answer([
            {"type": "text", "text": markup}
        ])
        try:
            by_id = {item.criterion_id: item for item in verification.criteria}
            self.assertEqual(
                by_id["answer-completeness"].status, AcceptanceStatus.FAILED
            )
            self.assertEqual(
                by_id["workspace-integrity"].status, AcceptanceStatus.PASSED
            )
            self.assertEqual(verification.status, AcceptanceStatus.FAILED)
        finally:
            await app.registry.stop_all()
            directory.cleanup()

    async def test_pending_structured_tool_call_is_not_a_final_answer(self):
        directory, app, verification = await self._verification_for_persisted_answer([
            {
                "type": "tool_call",
                "call": {
                    "call_id": "call-final", "name": "core.read_file",
                    "arguments": {"path": "screen.tsx"},
                },
            }
        ])
        try:
            criterion = next(
                item for item in verification.criteria
                if item.criterion_id == "answer-completeness"
            )
            self.assertEqual(criterion.status, AcceptanceStatus.FAILED)
            self.assertEqual(verification.status, AcceptanceStatus.FAILED)
        finally:
            await app.registry.stop_all()
            directory.cleanup()

    async def test_unverified_guess_after_failed_reads_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                tool_adapters=(CoreReadOnlyToolProvider(),)
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "determine behavior from source", root,
                    "task-unverified-answer",
                )
                task = await move(app, task.task_id, (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ))
                failed = await app.kernel.invoke_tool(
                    task.task_id, "turn-unverified", ToolCall(
                        "read-missing", "core.read_file",
                        {"path": "src/Missing.java"},
                    )
                )
                self.assertEqual(failed.error_code, "NOT_FOUND")
                await app.kernel._append_events(task.task_id, (
                    ("llm.completed", {
                        "turn_id": "turn-unverified",
                        "message": {
                            "message_id": "assistant-unverified",
                            "role": "assistant",
                            "content": [{
                                "type": "text",
                                "text": (
                                    "当前这轮我没法继续读取文件内容，所以不能"
                                    "百分百确认。不过大概率这个实现支持角色区分。"
                                ),
                            }],
                        },
                        "finish_reason": "stop",
                    }),
                    ("turn.completed", {"turn_id": "turn-unverified"}),
                ))
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify unverified answer"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                by_id = {item.criterion_id: item for item in verification.criteria}
                self.assertEqual(
                    by_id["answer-evidence-sufficiency"].status,
                    AcceptanceStatus.BLOCKED,
                )
                self.assertEqual(verification.status, AcceptanceStatus.BLOCKED)
            finally:
                await app.registry.stop_all()

    async def test_successful_read_does_not_trigger_unverified_answer_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.py").write_text("ROLE = 'driver'\n")
            app = compose_fixture_application(
                tool_adapters=(CoreReadOnlyToolProvider(),)
            )
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task(
                    "inspect source behavior", root,
                    "task-verified-answer",
                )
                task = await move(app, task.task_id, (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ))
                read = await app.kernel.invoke_tool(
                    task.task_id, "turn-verified", ToolCall(
                        "read-source", "core.read_file",
                        {"path": "source.py"},
                    )
                )
                self.assertTrue(read.ok)
                await app.kernel._append_events(task.task_id, (
                    ("llm.completed", {
                        "turn_id": "turn-verified",
                        "message": {
                            "message_id": "assistant-verified",
                            "role": "assistant",
                            "content": [{
                                "type": "text",
                                "text": "已读取源码；其他路径尚未验证。",
                            }],
                        },
                        "finish_reason": "stop",
                    }),
                    ("turn.completed", {"turn_id": "turn-verified"}),
                ))
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify successful read"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(verification.status, AcceptanceStatus.PASSED)
                self.assertNotIn(
                    "answer-evidence-sufficiency",
                    {item.criterion_id for item in verification.criteria},
                )
            finally:
                await app.registry.stop_all()

    async def test_each_task_has_private_runtime_spec_without_workspace_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            first = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await first.registry.start_all()
            try:
                one = await first.kernel.create_task("first goal", root, "task-one")
                two = await first.kernel.create_task("second goal", root, "task-two")
                installed = await self._install_proposal_spec(
                    first, one.task_id, "first goal"
                )
                self.assertEqual((await first.kernel.get_task_spec(one.task_id)).goal, "first goal")
                self.assertEqual((await first.kernel.get_task_spec(two.task_id)).goal, "second goal")
                self.assertFalse(any(root.glob("*spec*")))
            finally:
                await first.registry.stop_all()
            second = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await second.registry.start_all()
            try:
                restored = await second.kernel.get_task_spec("task-one")
                self.assertEqual(restored, installed)
                self.assertEqual(restored.outcomes[0].outcome_id, "deliver-fix")
            finally:
                await second.registry.stop_all()

    async def test_revision_cannot_change_goal_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("fixed goal", Path(directory))
                installed = await self._install_proposal_spec(
                    app, task.task_id, "fixed goal"
                )
                criterion = TaskAcceptanceCriterion(
                    "evidence", "inspection evidence exists",
                    TaskCriterionKind.EVIDENCE_REFERENCE, "event:1",
                )
                updated = await app.kernel.revise_task_spec(
                    task.task_id, installed.revision, scope=("src",), constraints=("no API changes",),
                    acceptance_criteria=(criterion,), operation_id="spec-op",
                    writer="test",
                )
                replay = await app.kernel.revise_task_spec(
                    task.task_id, installed.revision, scope=("src",), constraints=("no API changes",),
                    acceptance_criteria=(criterion,), operation_id="spec-op",
                    writer="test",
                )
                self.assertEqual(updated, replay)
                self.assertEqual(updated.outcomes, installed.outcomes)
                self.assertEqual(
                    updated.continuation_mode, installed.continuation_mode
                )
                with self.assertRaisesRegex(ValueError, "only through Runtime Replace"):
                    await app.kernel.revise_task_spec(
                        task.task_id, updated.revision, scope=(), constraints=(),
                        acceptance_criteria=(criterion,), operation_id="goal-change",
                        writer="test", goal="changed goal",
                    )
            finally:
                await app.registry.stop_all()

    async def test_verifier_blocks_missing_reference_and_passes_real_event(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("verify", Path(directory))
                task = await move(app, task.task_id, (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ))
                missing = TaskAcceptanceCriterion(
                    "proof", "proof exists", TaskCriterionKind.EVIDENCE_REFERENCE,
                    "event:999999",
                )
                await app.kernel.revise_task_spec(
                    task.task_id, 1, scope=(), constraints=(),
                    acceptance_criteria=(missing,), operation_id="missing", writer="test",
                )
                await app.kernel.transition_task(task.task_id, TaskState.VERIFYING, "verify")
                blocked = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(blocked.status, AcceptanceStatus.BLOCKED)
            finally:
                await app.registry.stop_all()

    async def test_steer_preserves_contract_and_replace_resets_goal(self):
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(tool_adapters=())
            await app.registry.start_all()
            try:
                task = await app.kernel.create_task("old goal", Path(directory))
                task = await move(app, task.task_id, (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
                ))
                installed = await self._install_proposal_spec(
                    app, task.task_id, "old goal"
                )
                checkpoint = AgentTurnCheckpoint(
                    task.task_id, "turn-spec", 1,
                    (Message("user-spec", MessageRole.USER, (TextBlock("start"),)),),
                    (), (), 0, 0, 0, 0, 5, 5, 256, 10.0,
                )
                checkpoint = await app.kernel._bind_agent_checkpoint(
                    task, checkpoint, await app.kernel.list_tools()
                )
                await app.kernel._save_agent_checkpoint(checkpoint, "test")
                await app.kernel.queue_steering(task.task_id, SteeringKind.STEER, "兼容 Windows", "steer-1")
                checkpoint, _ = await app.kernel._apply_pending_steering(checkpoint, "test")
                steered = await app.kernel.get_task_spec(task.task_id)
                self.assertEqual(steered.constraints, installed.constraints)
                self.assertIn("兼容 Windows", checkpoint.messages[-1].text)
                self.assertEqual(steered.outcomes, installed.outcomes)
                self.assertEqual(
                    steered.continuation_mode, installed.continuation_mode
                )
                await app.kernel.queue_steering(task.task_id, SteeringKind.REPLACE, "new goal", "replace-1")
                await app.kernel._apply_pending_steering(checkpoint, "test")
                spec = await app.kernel.get_task_spec(task.task_id)
                self.assertEqual(spec.goal, "new goal")
                self.assertFalse(spec.constraints)
                self.assertFalse(spec.outcomes)
                self.assertEqual(spec.continuation_mode, TaskContinuationMode.NONE)
            finally:
                await app.registry.stop_all()
