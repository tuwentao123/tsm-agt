from __future__ import annotations

import unittest

from tsm_agt.core import AgentTurnCheckpoint, TurnState
from tsm_agt.core.conversation_loop.action_facts import (
    failed_processes_without_output_observation, latest_failed_delivery_action,
    post_mutation_verification_fact, project_runtime_action_facts,
)
from tsm_agt.core.conversation_loop.prompt import latest_tool_result_instruction
from tsm_agt.ports import (
    Message, MessageRole, TextBlock, ToolCall, ToolCallBlock, ToolEffect,
    ToolResult, ToolResultAuthority, ToolResultBlock, ToolRisk, ToolSpec,
)


def _spec(name: str, effect: ToolEffect, authority: ToolResultAuthority) -> ToolSpec:
    return ToolSpec(
        name, name, {"type": "object"}, ToolRisk.R0,
        is_read_only=effect not in {ToolEffect.MUTATE, ToolEffect.EXECUTE},
        effect=effect, result_authority=authority,
    )


def _exchange(
    call_id: str, tool: str, arguments: dict[str, object], result: ToolResult,
) -> tuple[Message, Message]:
    return (
        Message(
            "call-" + call_id, MessageRole.ASSISTANT,
            (ToolCallBlock(ToolCall(call_id, tool, arguments)),),
        ),
        Message(
            "result-" + call_id, MessageRole.TOOL,
            (ToolResultBlock(result),),
        ),
    )


class _Kernel:
    @staticmethod
    def _latest_recoverable_tool_batch(_messages):
        return ()


class RuntimeActionFactsTest(unittest.TestCase):
    def test_verification_guidance_follows_observed_history(self) -> None:
        patch = _spec("core.apply_patch", ToolEffect.MUTATE, ToolResultAuthority.MUTATION_FACT)
        process = _spec("core.run_command", ToolEffect.EXECUTE, ToolResultAuthority.PROCESS_FACT)
        read = _spec("core.read_file", ToolEffect.OBSERVE, ToolResultAuthority.RUNTIME_FACT)
        edit = _exchange("edit", patch.name, {"path": "app.py"},
                         ToolResult("edit", True, data={"mutation_id": "m1"}))
        readback = _exchange("read", read.name, {"path": "app.py"},
                             ToolResult("read", True, data={"content": "changed"}))
        def check(call_id, data):
            return _exchange(call_id, process.name, {"argv": ["python", "-m", "pytest"]},
                             ToolResult(call_id, True, data=data))
        passing = check("pass", {"succeeded": True, "exit_code": 0})
        failing = check("fail", {"succeeded": False, "exit_code": 1,
                                 "failure_code": "PROCESS_EXIT_NON_ZERO"})
        running = check("running", {"state": "RUNNING", "execution_state": "running"})
        cases = (
            (edit + readback, "missing", "Choose a sufficient check"),
            (passing + edit, "missing", "after the latest mutation"),
            (edit + failing, "failed", "repair relevant failures"),
            (edit + running, "unknown", "Do not start a duplicate check"),
            (edit + failing + passing, "passed", "do not repeat a still-valid passing check"),
        )
        for messages, status, guidance in cases:
            with self.subTest(status=status, history=[m.message_id for m in messages]):
                checkpoint = AgentTurnCheckpoint(
                    "task", "turn", 1, (), (), (), 1, 0, 0, 0, 3, 3, 100, 1.0,
                )
                state = TurnState(
                    task_id="task", turn_id="turn", checkpoint=checkpoint,
                    messages=list(messages), pending_tool_calls=[], model_call_count=1,
                    visible_tool_by_name={s.name: s for s in (patch, process, read)},
                )
                instruction = latest_tool_result_instruction(_Kernel(), state)
                self.assertIn("check: " + status + ".", instruction)
                self.assertIn(guidance, instruction)
                self.assertIn("Respect scope, denials, and approval boundaries", instruction)
                self.assertNotIn("Decide whether an available verification is needed", instruction)
                self.assertNotIn('"call_id"', instruction)
                if status == "missing":
                    self.assertIn("other tool evidence may already cover it", instruction)
                    self.assertIn("Prose needs no mandatory runtime test", instruction)

    def test_read_only_work_does_not_inject_post_mutation_guidance(self) -> None:
        read = _spec("core.read_file", ToolEffect.OBSERVE, ToolResultAuthority.RUNTIME_FACT)
        checkpoint = AgentTurnCheckpoint(
            "task", "turn", 1, (), (), (), 1, 0, 0, 0, 3, 3, 100, 1.0,
        )
        state = TurnState(
            task_id="task", turn_id="turn", checkpoint=checkpoint,
            messages=list(_exchange("read", read.name, {"path": "app.py"},
                                    ToolResult("read", True, data={"content": "code"}))),
            pending_tool_calls=[], model_call_count=1, visible_tool_by_name={read.name: read},
        )
        self.assertEqual(latest_tool_result_instruction(_Kernel(), state), "")

    def test_process_observation_preserves_invocation_and_operation_status(self) -> None:
        process = _spec(
            "core.run_command", ToolEffect.EXECUTE,
            ToolResultAuthority.PROCESS_FACT,
        )
        messages = _exchange(
            "run", "core.run_command", {"argv": ["node", "server.ts"]},
            ToolResult(
                "run", True,
                data={
                    "execution_state": "failed_known", "succeeded": False,
                    "failure_code": "PROCESS_EXIT_NON_ZERO", "exit_code": 1,
                },
            ),
        )

        fact = project_runtime_action_facts(messages, {process.name: process})[0]

        self.assertTrue(fact.invocation_succeeded)
        self.assertTrue(fact.failed)
        self.assertEqual(fact.prompt_data()["operation_outcome"], "failed")
        self.assertEqual(fact.prompt_data()["exit_code"], 1)
        self.assertIs(latest_failed_delivery_action((fact,)), fact)

    def test_preflight_observation_preserves_no_start_facts_without_a_repair(self) -> None:
        process = _spec(
            "core.run_command", ToolEffect.EXECUTE,
            ToolResultAuthority.PROCESS_FACT,
        )
        messages = _exchange(
            "preflight", "core.run_command", {"argv": ["python", "-m", "pytest"]},
            ToolResult(
                "preflight", False, error_code="EXECUTABLE_NOT_FOUND",
                data={
                    "execution_state": "not_started",
                    "stage": "resolve_executable",
                    "requested_executable": "python",
                },
            ),
        )
        fact = project_runtime_action_facts(messages, {process.name: process})[0]

        self.assertTrue(fact.did_not_start_execution)
        self.assertEqual(fact.prompt_data(), {
            "tool": "core.run_command",
            "call_id": "preflight",
            "invocation_status": "failed",
            "effect": "execute",
            "operation_outcome": "failed",
            "execution_state": "not_started",
            "stage": "resolve_executable",
            "requested_executable": "python",
            "failure_code": "EXECUTABLE_NOT_FOUND",
        })
        self.assertEqual(
            post_mutation_verification_fact((fact,)).status, "not_applicable"
        )

        checkpoint = AgentTurnCheckpoint(
            "task", "turn", 1, (), (), (), 1, 0, 0, 0, 3, 3, 100, 1.0,
        )
        state = TurnState(
            task_id="task", turn_id="turn", checkpoint=checkpoint,
            messages=list(messages), pending_tool_calls=[], model_call_count=1,
            visible_tool_by_name={process.name: process},
        )
        instruction = latest_tool_result_instruction(_Kernel(), state)
        self.assertIn("did not start in the governed execution environment", instruction)
        self.assertIn("project-declared entry points", instruction)
        self.assertNotIn("python3", instruction)
        self.assertNotIn("install it", instruction)

    def test_unstarted_check_after_a_mutation_is_unobserved_not_failed(self) -> None:
        patch = _spec(
            "core.apply_patch", ToolEffect.MUTATE,
            ToolResultAuthority.MUTATION_FACT,
        )
        process = _spec(
            "core.run_command", ToolEffect.EXECUTE,
            ToolResultAuthority.PROCESS_FACT,
        )
        messages = (
            *_exchange(
                "patch", patch.name, {"path": "app.py"},
                ToolResult("patch", True, data={"mutation_id": "m1"}),
            ),
            *_exchange(
                "preflight", process.name, {"argv": ["python", "-m", "pytest"]},
                ToolResult(
                    "preflight", False, error_code="EXECUTABLE_NOT_FOUND",
                    data={
                        "execution_state": "not_started",
                        "stage": "resolve_executable",
                        "requested_executable": "python",
                    },
                ),
            ),
        )

        facts = project_runtime_action_facts(
            messages, {patch.name: patch, process.name: process},
        )

        self.assertFalse(facts[-1].is_recognized_verification)
        self.assertEqual(post_mutation_verification_fact(facts).status, "missing")

    def test_post_mutation_verification_tracks_only_the_later_attempt(self) -> None:
        patch = _spec(
            "core.apply_patch", ToolEffect.MUTATE,
            ToolResultAuthority.MUTATION_FACT,
        )
        process = _spec(
            "core.run_command", ToolEffect.EXECUTE,
            ToolResultAuthority.PROCESS_FACT,
        )
        messages = (
            *_exchange(
                "patch", "core.apply_patch", {"path": "app.py"},
                ToolResult("patch", True, data={"mutation_id": "mutation-1"}),
            ),
            *_exchange(
                "test", "core.run_command", {"argv": ["python", "-m", "pytest"]},
                ToolResult(
                    "test", True,
                    data={"succeeded": False, "failure_code": "PROCESS_EXIT_NON_ZERO"},
                ),
            ),
        )

        facts = project_runtime_action_facts(
            messages, {patch.name: patch, process.name: process},
        )
        verification = post_mutation_verification_fact(facts)

        self.assertEqual(verification.status, "failed")
        self.assertEqual(verification.mutation.call_id, "patch")
        self.assertEqual(verification.verification.call_id, "test")

    def test_unknown_outcome_is_not_relabelled_as_an_ordinary_failure(self) -> None:
        process = _spec(
            "core.run_command", ToolEffect.EXECUTE,
            ToolResultAuthority.PROCESS_FACT,
        )
        messages = _exchange(
            "run", "core.run_command", {"argv": ["tool"]},
            ToolResult(
                "run", False, error_code="UNKNOWN_OUTCOME",
                message="the process may have started",
            ),
        )

        fact = project_runtime_action_facts(messages, {process.name: process})[0]

        self.assertEqual(fact.effect_status.value, "unknown")
        self.assertEqual(fact.failure_code, "UNKNOWN_OUTCOME")
        self.assertIsNone(latest_failed_delivery_action((fact,)))

    def test_dynamic_instruction_states_facts_without_selecting_the_repair(self) -> None:
        process = _spec(
            "core.run_command", ToolEffect.EXECUTE,
            ToolResultAuthority.PROCESS_FACT,
        )
        checkpoint = AgentTurnCheckpoint(
            "task", "turn", 1, (), (), (), 1, 0, 0, 0, 3, 3, 100, 1.0,
        )
        state = TurnState(
            task_id="task", turn_id="turn", checkpoint=checkpoint,
            messages=list(_exchange(
                "run", "core.run_command", {"argv": ["node", "server.ts"]},
                ToolResult(
                    "run", True,
                    data={
                        "succeeded": False,
                        "failure_code": "PROCESS_EXIT_NON_ZERO",
                    },
                ),
            )),
            pending_tool_calls=[], model_call_count=1,
        )

        instruction = latest_tool_result_instruction(_Kernel(), state, (process,))

        self.assertIn("Runtime action facts", instruction)
        self.assertIn("include a failure", instruction)
        self.assertIn("not authority or a completion verdict", instruction)
        self.assertNotIn("npm install", instruction)

    def test_nonzero_process_requires_output_observation_before_attribution(self) -> None:
        process = _spec(
            "core.run_command", ToolEffect.EXECUTE,
            ToolResultAuthority.PROCESS_FACT,
        )
        logs = _spec(
            "core.process_logs", ToolEffect.OBSERVE,
            ToolResultAuthority.PROCESS_FACT,
        )
        failed = _exchange(
            "run", process.name, {"argv": ["tool"]},
            ToolResult(
                "run", True,
                data={
                    "execution_state": "failed_known", "succeeded": False,
                    "failure_code": "PROCESS_EXIT_NON_ZERO", "exit_code": 1,
                    "process_id": "process-failed",
                },
            ),
        )
        checkpoint = AgentTurnCheckpoint(
            "task", "turn", 1, (), (), (), 1, 0, 0, 0, 3, 3, 100, 1.0,
        )
        state = TurnState(
            task_id="task", turn_id="turn", checkpoint=checkpoint,
            messages=list(failed), pending_tool_calls=[], model_call_count=1,
            visible_tool_by_name={tool.name: tool for tool in (process, logs)},
        )

        facts = project_runtime_action_facts(
            failed, {tool.name: tool for tool in (process, logs)},
        )
        self.assertEqual(
            [fact.process_id for fact in failed_processes_without_output_observation(facts)],
            ["process-failed"],
        )
        instruction = latest_tool_result_instruction(_Kernel(), state, (process, logs))
        self.assertIn("stdout/stderr for that process has not been observed", instruction)
        self.assertIn("The exit code establishes failure, not its cause", instruction)
        self.assertNotIn("pip install", instruction)

        initial_observation = _exchange(
            "run-with-output", process.name, {"argv": ["tool"]},
            ToolResult(
                "run-with-output", True,
                data={
                    "execution_state": "failed_known", "succeeded": False,
                    "failure_code": "PROCESS_EXIT_NON_ZERO", "exit_code": 1,
                    "process_id": "process-failed", "output_observed": True,
                    "stdout": {"text": ""},
                    "stderr": {"text": "actual failure detail"},
                },
            ),
        )
        initial_facts = project_runtime_action_facts(
            initial_observation, {tool.name: tool for tool in (process, logs)},
        )
        self.assertEqual(
            failed_processes_without_output_observation(initial_facts), ()
        )

        before_failure = (*_exchange(
            "old-logs", logs.name, {"process_id": "process-failed"},
            ToolResult("old-logs", True, data={"stderr": "old trace"}),
        ), *failed)
        before_failure_facts = project_runtime_action_facts(
            before_failure, {tool.name: tool for tool in (process, logs)},
        )
        self.assertEqual(
            [fact.process_id for fact in failed_processes_without_output_observation(
                before_failure_facts
            )],
            ["process-failed"],
        )

        observed = (*failed, *_exchange(
            "logs", logs.name, {"process_id": "process-failed"},
            ToolResult("logs", True, data={"stderr": "trace"}),
        ))
        observed_state = TurnState(
            task_id="task", turn_id="turn", checkpoint=checkpoint,
            messages=list(observed), pending_tool_calls=[], model_call_count=1,
            visible_tool_by_name={tool.name: tool for tool in (process, logs)},
        )
        observed_facts = project_runtime_action_facts(
            observed, {tool.name: tool for tool in (process, logs)},
        )
        self.assertEqual(failed_processes_without_output_observation(observed_facts), ())
        self.assertNotIn(
            "stdout/stderr for that process has not been observed",
            latest_tool_result_instruction(_Kernel(), observed_state, (process, logs)),
        )

    def test_dynamic_instruction_keeps_prior_tool_semantics_when_profile_narrows(self) -> None:
        process = _spec(
            "core.run_command", ToolEffect.EXECUTE,
            ToolResultAuthority.PROCESS_FACT,
        )
        checkpoint = AgentTurnCheckpoint(
            "task", "turn", 1, (), (), (), 1, 0, 0, 0, 3, 3, 100, 1.0,
        )
        state = TurnState(
            task_id="task", turn_id="turn", checkpoint=checkpoint,
            messages=list(_exchange(
                "run", "core.run_command", {"argv": ["tool"]},
                ToolResult(
                    "run", True,
                    data={"succeeded": False, "failure_code": "PROCESS_EXIT_NON_ZERO"},
                ),
            )),
            pending_tool_calls=[], model_call_count=1,
            visible_tool_by_name={process.name: process},
        )

        instruction = latest_tool_result_instruction(_Kernel(), state, ())

        self.assertIn("Runtime action facts", instruction)
        self.assertIn("include a failure", instruction)


if __name__ == "__main__":
    unittest.main()
