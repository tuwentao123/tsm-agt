from __future__ import annotations

import unittest

from tsm_agt.core import AgentTurnCheckpoint, TurnState
from tsm_agt.core.conversation_loop.action_facts import (
    latest_failed_delivery_action, post_mutation_verification_fact,
    project_runtime_action_facts,
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
        self.assertIn('"invocation_status": "succeeded"', instruction)
        self.assertIn('"operation_outcome": "failed"', instruction)
        self.assertIn("does not establish completion", instruction)
        self.assertNotIn("npm install", instruction)

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
        self.assertIn('"operation_outcome": "failed"', instruction)


if __name__ == "__main__":
    unittest.main()
