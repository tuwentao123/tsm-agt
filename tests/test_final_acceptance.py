from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.rule_based_final_acceptance import (
    RuleBasedFinalAcceptancePolicy,
)
from tsm_agt.adapters.rule_based_scope_consistency import (
    RuleBasedToolScopeConsistencyPolicy,
)
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    AcceptanceStatus, ApprovalDecision, ApprovalRequired, InvalidTurnState,
    TaskAcceptanceCriterion,
    TaskCriterionKind, TaskState,
)
from tsm_agt.ports import (
    EvidenceQuestion, RuntimeStorePort, ToolCall,
)


async def executing_task(app, root: Path, task_id: str):
    task = await app.kernel.create_task("inspect implementation", root, task_id)
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await app.kernel.transition_task(task.task_id, state, state.value)
    return task


async def persist_final_answer(app, task_id: str, text: str = "Done.") -> None:
    await app.kernel._append_events(task_id, (
        ("llm.completed", {
            "turn_id": "turn-final",
            "message": {
                "message_id": "answer-final", "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
            "finish_reason": "stop",
        }),
        ("turn.completed", {"turn_id": "turn-final"}),
    ))


async def bind_invoke_observe(app, task_id: str, call: ToolCall):
    await app.kernel._bind_evidence_question(task_id, "turn-investigate", call)
    result = await app.kernel.invoke_tool(task_id, "turn-investigate", call)
    await app.kernel._observe_evidence_question(
        task_id, "turn-investigate", call, result, None
    )
    return result


class FinalAcceptanceTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def application(*, tools=()):
        return compose_fixture_application(
            tool_adapters=tools,
            tool_scope_consistency_policy_adapter=(
                RuleBasedToolScopeConsistencyPolicy()
            ),
            final_acceptance_policy_adapter=RuleBasedFinalAcceptancePolicy(),
        )

    async def test_wrong_scope_cannot_satisfy_question_or_task_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            intended = root / "intended"
            intended.mkdir()
            (root / "service.py").write_text("wrong = True\n", encoding="utf-8")
            app = self.application(tools=(CoreReadOnlyToolProvider(),))
            await app.registry.start_all()
            try:
                task = await executing_task(app, root, "final-wrong-scope")
                call = ToolCall(
                    "wrong-scope-proof", "core.read_file",
                    {"path": "service.py"},
                    EvidenceQuestion(
                        "Q-final-scope", "Read the intended implementation",
                        expected_scope=str(intended),
                    ),
                )
                result = await bind_invoke_observe(app, task.task_id, call)
                self.assertEqual(result.error_code, "TOOL_SCOPE_MISMATCH")
                await app.kernel.revise_task_spec(
                    task.task_id, 1, scope=(str(intended),), constraints=(),
                    acceptance_criteria=(TaskAcceptanceCriterion(
                        "intended-proof", "intended implementation was read",
                        TaskCriterionKind.EVIDENCE_REFERENCE,
                        "tool_call:wrong-scope-proof",
                    ),), operation_id="wrong-scope-spec", writer="test",
                )
                await persist_final_answer(app, task.task_id)
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                by_id = {item.criterion_id: item for item in verification.criteria}
                self.assertEqual(
                    by_id["intended-proof"].status, AcceptanceStatus.BLOCKED
                )
                self.assertEqual(
                    by_id["final-evidence-integrity"].status,
                    AcceptanceStatus.BLOCKED,
                )
                self.assertEqual(verification.status, AcceptanceStatus.BLOCKED)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                evaluated = next(
                    event for event in events
                    if event.event_type == "verify.final_evidence_evaluated"
                )
                codes = {item["code"] for item in evaluated.payload["violations"]}
                self.assertIn("OPEN_QUESTION", codes)
                self.assertIn("WRONG_SCOPE_EVIDENCE", codes)
            finally:
                await app.registry.stop_all()

    async def test_correct_scope_retry_makes_final_evidence_traceable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            intended = root / "intended"
            intended.mkdir()
            (root / "service.py").write_text("wrong = True\n", encoding="utf-8")
            target = intended / "service.py"
            target.write_text("correct = True\n", encoding="utf-8")
            app = self.application(tools=(CoreReadOnlyToolProvider(),))
            await app.registry.start_all()
            try:
                task = await executing_task(app, root, "final-corrected-scope")
                question = EvidenceQuestion(
                    "Q-corrected", "Read the intended implementation",
                    expected_scope=str(intended),
                )
                wrong = ToolCall(
                    "wrong-first", "core.read_file",
                    {"path": "service.py"}, question,
                )
                self.assertEqual(
                    (await bind_invoke_observe(app, task.task_id, wrong)).error_code,
                    "TOOL_SCOPE_MISMATCH",
                )
                correct = ToolCall(
                    "correct-second", "core.read_file",
                    {"path": str(target)}, question,
                )
                self.assertTrue(
                    (await bind_invoke_observe(app, task.task_id, correct)).ok
                )
                await app.kernel.revise_task_spec(
                    task.task_id, 1, scope=(str(intended),), constraints=(),
                    acceptance_criteria=(TaskAcceptanceCriterion(
                        "correct-proof", "correct implementation was read",
                        TaskCriterionKind.EVIDENCE_REFERENCE,
                        "tool_call:correct-second",
                    ),), operation_id="correct-scope-spec", writer="test",
                )
                await persist_final_answer(app, task.task_id, "Confirmed from source.")
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(verification.status, AcceptanceStatus.PASSED)
                integrity = next(
                    item for item in verification.criteria
                    if item.criterion_id == "final-evidence-integrity"
                )
                self.assertEqual(integrity.status, AcceptanceStatus.PASSED)
            finally:
                await app.registry.stop_all()

    async def test_task_approved_external_read_is_trusted_final_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "workspace"
            external = base / "external"
            root.mkdir()
            external.mkdir()
            target = external / "service.txt"
            target.write_text("owner=external-team\n", encoding="utf-8")
            app = self.application(tools=(CoreReadOnlyToolProvider(),))
            await app.registry.start_all()
            try:
                task = await executing_task(app, root, "final-approved-external")
                call = ToolCall(
                    "approved-external-proof", "core.read_file",
                    {"path": "../external/service.txt"},
                    EvidenceQuestion(
                        "Q-approved-external", "Read approved external source",
                        expected_scope="../external/service.txt",
                    ),
                )
                await app.kernel._bind_evidence_question(
                    task.task_id, "turn-external", call
                )
                with self.assertRaises(ApprovalRequired) as approval:
                    await app.kernel.invoke_tool(
                        task.task_id, "turn-external", call
                    )
                request = approval.exception.request
                result = await app.kernel.resolve_approval(
                    task.task_id, request.request_id, request.payload_hash,
                    ApprovalDecision.APPROVE, "approve exact fixture directory",
                )
                self.assertTrue(result.ok)
                await app.kernel._observe_evidence_question(
                    task.task_id, "turn-external", call, result, None
                )
                await persist_final_answer(
                    app, task.task_id, "Confirmed from approved external source."
                )
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify"
                )
                verification = await app.kernel.verify_task_acceptance(
                    task.task_id
                )
                self.assertEqual(verification.status, AcceptanceStatus.PASSED)
                events = await app.registry.require(RuntimeStorePort).read_events(
                    task.task_id
                )
                scoped = next(
                    event for event in events
                    if event.event_type == "tool.scope_consistency_evaluated"
                )
                self.assertEqual(scoped.payload["relation"], "MATCH")
            finally:
                await app.registry.stop_all()

    async def test_blocked_question_cannot_be_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.application(tools=(CoreReadOnlyToolProvider(),))
            await app.registry.start_all()
            try:
                task = await executing_task(app, root, "final-blocked-question")
                call = ToolCall(
                    "missing-proof", "core.read_file",
                    {"path": "missing.py"}, EvidenceQuestion(
                        "Q-missing", "Read required source", expected_scope="."
                    ),
                )
                result = await bind_invoke_observe(app, task.task_id, call)
                self.assertEqual(result.error_code, "NOT_FOUND")
                await persist_final_answer(app, task.task_id, "The feature is complete.")
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(verification.status, AcceptanceStatus.BLOCKED)
                self.assertIn(
                    "final-evidence-integrity",
                    {item.criterion_id for item in verification.criteria},
                )
            finally:
                await app.registry.stop_all()

    async def test_latest_completion_gap_blocks_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.application()
            await app.registry.start_all()
            try:
                task = await executing_task(app, root, "final-required-work")
                await app.kernel._append_events(task.task_id, ((
                    "completion.readiness_evaluated", {
                        "action": "REPORT_BLOCKED",
                        "gaps": [{
                            "gap_id": "execution-failure:turn-1:call-1",
                            "required": True,
                            "kind": "UNRESOLVED_EFFECT_FAILURE",
                        }],
                    },
                ),))
                await persist_final_answer(app, task.task_id)
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(verification.status, AcceptanceStatus.BLOCKED)
                integrity = next(
                    item for item in verification.criteria
                    if item.criterion_id == "final-evidence-integrity"
                )
                # Exactly one violation: the required completion gap. The plan
                # is no longer a delivery contract, so it must not contribute.
                self.assertEqual(len(integrity.evidence), 1)
            finally:
                await app.registry.stop_all()

    async def test_unfinished_plan_step_is_not_a_completion_gate(self) -> None:
        # 完成判定改造SPEC.md §5.5: the working-memory plan is the model's
        # scratchpad, not a delivery contract. A forgotten update must not turn
        # real, recorded work into a blocked Task.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.application()
            await app.registry.start_all()
            try:
                task = await executing_task(app, root, "scratchpad-not-a-gate")
                await app.kernel.update_working_memory(
                    task.task_id, 1, {
                        "goal": task.goal, "constraints": [], "facts": [],
                        "decisions": [], "hypotheses": [],
                        "open_questions": [],
                        "plan": [{
                            "step_id": "verify-output",
                            "description": "Verify the output",
                            "status": "IN_PROGRESS",
                            "completion_criteria": "Output check passes",
                        }],
                        "completed_work": [], "remaining_work": [],
                        "evidence": [],
                    }, "scratchpad", "test",
                )
                await persist_final_answer(app, task.task_id)
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(verification.status, AcceptanceStatus.PASSED)
                self.assertNotIn(
                    "INCOMPLETE_REQUIRED_PLAN",
                    repr(verification.to_data()),
                )
            finally:
                await app.registry.stop_all()

    async def test_plain_answer_and_optional_free_text_are_not_false_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.application()
            await app.registry.start_all()
            try:
                task = await executing_task(app, root, "final-plain-answer")
                await app.kernel.update_working_memory(
                    task.task_id, 1, {
                        "goal": task.goal, "constraints": [], "facts": [],
                        "decisions": [], "hypotheses": [],
                        "open_questions": ["Optional future comparison"],
                        "plan": [], "completed_work": ["Core answer done"],
                        "remaining_work": ["Optional extension"],
                        "evidence": [],
                    }, "optional-notes", "test",
                )
                await persist_final_answer(app, task.task_id, "Here is the explanation.")
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify"
                )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(verification.status, AcceptanceStatus.PASSED)
                finalizing = await app.kernel.transition_task(
                    task.task_id, TaskState.FINALIZING, "verified"
                )
                succeeded = await app.kernel.transition_task(
                    finalizing.task_id, TaskState.SUCCEEDED, "verified"
                )
                self.assertEqual(succeeded.state, TaskState.SUCCEEDED)
            finally:
                await app.registry.stop_all()

    async def test_kernel_cannot_bypass_blocked_or_missing_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.application(tools=(CoreReadOnlyToolProvider(),))
            await app.registry.start_all()
            try:
                task = await executing_task(app, root, "final-state-gate")
                await app.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING, "verify"
                )
                with self.assertRaisesRegex(
                    InvalidTurnState, "latest passed trusted verification"
                ):
                    await app.kernel.transition_task(
                        task.task_id, TaskState.FINALIZING, "bypass"
                    )
                verification = await app.kernel.verify_task_acceptance(task.task_id)
                self.assertEqual(verification.status, AcceptanceStatus.PASSED)
                await app.kernel.transition_task(
                    task.task_id, TaskState.FINALIZING, "verified"
                )
            finally:
                await app.registry.stop_all()

    async def test_same_verifier_contract_covers_three_project_shapes(self) -> None:
        fixtures = (
            ("android", "src/main/kotlin/Feature.kt", "class Feature"),
            ("web", "src/Feature.tsx", "export function Feature"),
            ("backend", "service/feature.py", "def feature"),
        )
        for project_kind, relative_path, source in fixtures:
            with self.subTest(project_kind=project_kind):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    target = root / relative_path
                    target.parent.mkdir(parents=True)
                    target.write_text(source + "\n", encoding="utf-8")
                    app = self.application(tools=(CoreReadOnlyToolProvider(),))
                    await app.registry.start_all()
                    try:
                        task = await executing_task(
                            app, root, f"final-project-{project_kind}"
                        )
                        call = ToolCall(
                            f"read-{project_kind}", "core.read_file",
                            {"path": relative_path}, EvidenceQuestion(
                                f"Q-{project_kind}",
                                "Read the required implementation",
                                expected_scope=str(target),
                            ),
                        )
                        self.assertTrue(
                            (await bind_invoke_observe(
                                app, task.task_id, call
                            )).ok
                        )
                        await app.kernel.revise_task_spec(
                            task.task_id, 1, scope=(relative_path,), constraints=(),
                            acceptance_criteria=(TaskAcceptanceCriterion(
                                f"proof-{project_kind}",
                                "implementation source was read",
                                TaskCriterionKind.EVIDENCE_REFERENCE,
                                f"tool_call:read-{project_kind}",
                            ),), operation_id=f"spec-{project_kind}", writer="test",
                        )
                        await persist_final_answer(
                            app, task.task_id, "Confirmed from implementation."
                        )
                        await app.kernel.transition_task(
                            task.task_id, TaskState.VERIFYING, "verify"
                        )
                        verification = (
                            await app.kernel.verify_task_acceptance(task.task_id)
                        )
                        self.assertEqual(verification.status, AcceptanceStatus.PASSED)
                    finally:
                        await app.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
