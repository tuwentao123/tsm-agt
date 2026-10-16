from __future__ import annotations

import unittest
import tempfile
import json
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.model_completion_reviewer import ModelCompletionReviewer
from tsm_agt.core import AgentTurnCheckpoint, TurnState
from tsm_agt.core.conversation_loop.finalization import RuntimeFinalizationGate
from tsm_agt.bootstrap.completion_review_configuration import (
    load_completion_review_configuration,
)
from tsm_agt.ports import (
    AdapterContext, CompletionReviewAction, CompletionReviewError,
    FinalizationAction, FinalizationCandidate, FinishReason, Message,
    MessageRole, ModelCallPurpose, ModelRequest, ModelResponse, TextBlock,
    ToolCall, ToolCallBlock, ToolEffect, ToolRecoveryKind, ToolResult,
    ToolResultAuthority, ToolResultBlock, ToolRisk, ToolSpec,
)


def _message(message_id: str, role: MessageRole, text: str) -> Message:
    return Message(message_id, role, (TextBlock(text),))


class _ReviewModel(EchoModelProvider):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            _message("review", MessageRole.ASSISTANT, self.text),
            FinishReason.STOP,
        )


class CompletionReviewerTest(unittest.IsolatedAsyncioTestCase):
    async def test_reviewer_is_tool_free_and_parses_verdict(self) -> None:
        model = _ReviewModel(
            '{"action":"CONTINUE","reason_code":"source_not_read",'
            '"reason":"Source content is missing.","missing":["source content"]}'
        )
        reviewer = ModelCompletionReviewer(model)
        await model.start(AdapterContext({}, lambda _kind, _payload: None))
        await reviewer.start(AdapterContext({}, lambda _kind, _payload: None))
        candidate = FinalizationCandidate(
            "task", "turn", _message("user", MessageRole.USER, "inspect source"),
            _message("answer", MessageRole.ASSISTANT, "It works."),
            FinishReason.STOP, (), 1, 0, 0,
        )
        verdict = await reviewer.review(candidate)
        self.assertIs(verdict.action, CompletionReviewAction.CONTINUE)
        request = model.requests[0]
        self.assertIs(request.purpose, ModelCallPurpose.COMPLETION_REVIEW)
        self.assertFalse(request.allow_tool_calls)
        self.assertEqual(request.tools, ())
        self.assertEqual(
            json.loads(request.messages[1].text)["available_write_tools"], []
        )

    async def test_reviewer_receives_runtime_write_tool_facts(self) -> None:
        model = _ReviewModel(
            '{"action":"CONTINUE","reason_code":"write_unfinished",'
            '"reason":"The requested artifact was not written.","missing":["write"]}'
        )
        reviewer = ModelCompletionReviewer(model)
        await model.start(AdapterContext({}, lambda _kind, _payload: None))
        await reviewer.start(AdapterContext({}, lambda _kind, _payload: None))
        candidate = FinalizationCandidate(
            "task", "turn", _message("user", MessageRole.USER, "write docs"),
            _message("answer", MessageRole.ASSISTANT, "I cannot write."),
            FinishReason.STOP, (), 1, 0, 0,
            ("core.apply_patch_v4a", "core.delete_file"),
        )
        await reviewer.review(candidate)
        self.assertEqual(json.loads(model.requests[0].messages[1].text)["available_write_tools"], [
            "core.apply_patch_v4a", "core.delete_file",
        ])

    async def test_invalid_reviewer_json_fails_closed(self) -> None:
        model = _ReviewModel("not json")
        reviewer = ModelCompletionReviewer(model)
        await model.start(AdapterContext({}, lambda _kind, _payload: None))
        await reviewer.start(AdapterContext({}, lambda _kind, _payload: None))
        candidate = FinalizationCandidate(
            "task", "turn", _message("user", MessageRole.USER, "x"),
            _message("answer", MessageRole.ASSISTANT, "y"),
            FinishReason.STOP, (), 1, 0, 0,
        )
        with self.assertRaises(CompletionReviewError):
            await reviewer.review(candidate)


class RuntimeFinalizationGateTest(unittest.IsolatedAsyncioTestCase):
    def _state(self, *, model_calls: int = 1, max_model_calls: int = 3) -> TurnState:
        checkpoint = AgentTurnCheckpoint(
            "task", "turn", 1, (), (), (), model_calls, 0, 0, 0,
            max_model_calls, 3, 100, 1.0,
        )
        return TurnState(
            task_id="task", turn_id="turn", checkpoint=checkpoint,
            messages=[], pending_tool_calls=[], model_call_count=model_calls,
        )

    @staticmethod
    def _spec(name: str, effect: ToolEffect, authority: ToolResultAuthority) -> ToolSpec:
        return ToolSpec(
            name, name, {"type": "object"}, ToolRisk.R0,
            is_read_only=effect not in {ToolEffect.MUTATE, ToolEffect.EXECUTE},
            effect=effect, result_authority=authority,
        )

    @staticmethod
    def _tool_exchange(
        call_id: str, name: str, arguments: dict[str, object], result: ToolResult,
    ) -> list[Message]:
        return [
            Message(
                "call-" + call_id, MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(call_id, name, arguments)),),
            ),
            Message(
                "result-" + call_id, MessageRole.TOOL,
                (ToolResultBlock(result),),
            ),
        ]

    async def test_length_continues_while_budget_remains(self) -> None:
        decision = await RuntimeFinalizationGate().evaluate(
            self._state(), ModelResponse(
                _message("answer", MessageRole.ASSISTANT, "partial"),
                FinishReason.LENGTH,
            ),
        )
        self.assertIs(decision.action, FinalizationAction.CONTINUE)
        self.assertEqual(decision.reason_code, "output_truncated")

    async def test_empty_response_at_budget_is_incomplete(self) -> None:
        decision = await RuntimeFinalizationGate().evaluate(
            self._state(model_calls=3, max_model_calls=3), ModelResponse(
                _message("answer", MessageRole.ASSISTANT, ""), FinishReason.STOP,
            ),
        )
        self.assertIs(decision.action, FinalizationAction.INCOMPLETE)
        self.assertEqual(decision.reason_code, "empty_response_budget_exhausted")

    async def test_unstarted_execution_gets_one_recovery_turn(self) -> None:
        state = self._state()
        state.messages = [Message(
            "tool-result", MessageRole.TOOL, (ToolResultBlock(ToolResult(
                "call", False, error_code="EXECUTABLE_NOT_FOUND",
                message="not installed",
                recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
                recovery_action={"execution_state": "not_started"},
            )),),
        )]
        decision = await RuntimeFinalizationGate().evaluate(
            state, ModelResponse(
                _message("answer", MessageRole.ASSISTANT, "Please install it."),
                FinishReason.STOP,
            ),
        )
        self.assertIs(decision.action, FinalizationAction.CONTINUE)
        self.assertEqual(decision.reason_code, "unresolved_execution_outcome")

    async def test_unstarted_execution_allows_evidence_based_blocker_after_recovery(self) -> None:
        state = self._state()
        state.runtime_continuations = 1
        state.messages = [Message(
            "tool-result", MessageRole.TOOL, (ToolResultBlock(ToolResult(
                "call", False, error_code="EXECUTABLE_NOT_FOUND",
                message="not installed",
                recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
                recovery_action={"execution_state": "not_started"},
            )),),
        )]
        decision = await RuntimeFinalizationGate().evaluate(
            state, ModelResponse(
                _message("answer", MessageRole.ASSISTANT, "The executable is absent."),
                FinishReason.STOP,
            ),
        )
        self.assertIs(decision.action, FinalizationAction.ACCEPT)
        self.assertEqual(decision.reason_code, "unstarted_execution_blocker_visible")

    async def test_known_process_failure_gets_one_recovery_turn(self) -> None:
        state = self._state()
        state.visible_tool_by_name = {
            "core.run_command": self._spec(
                "core.run_command", ToolEffect.EXECUTE,
                ToolResultAuthority.PROCESS_FACT,
            ),
        }
        state.messages = self._tool_exchange(
            "call", "core.run_command", {"argv": ["node", "server.ts"]},
            ToolResult(
                "call", True, message="command exited 1",
                data={
                    "execution_state": "failed_known", "succeeded": False,
                    "failure_code": "PROCESS_EXIT_NON_ZERO", "exit_code": 1,
                },
            ),
        )
        decision = await RuntimeFinalizationGate().evaluate(
            state, ModelResponse(
                _message("answer", MessageRole.ASSISTANT, "The command exited with code 1."),
                FinishReason.STOP,
            ),
        )
        self.assertIs(decision.action, FinalizationAction.CONTINUE)
        self.assertEqual(decision.reason_code, "latest_delivery_action_failed")

    async def test_failed_delivery_action_allows_blocker_after_one_continuation(self) -> None:
        state = self._state()
        state.runtime_continuations = 1
        state.visible_tool_by_name = {
            "core.run_command": self._spec(
                "core.run_command", ToolEffect.EXECUTE,
                ToolResultAuthority.PROCESS_FACT,
            ),
        }
        state.messages = self._tool_exchange(
            "call", "core.run_command", {"argv": ["node", "server.ts"]},
            ToolResult(
                "call", True,
                data={"succeeded": False, "failure_code": "PROCESS_EXIT_NON_ZERO"},
            ),
        )
        decision = await RuntimeFinalizationGate().evaluate(
            state, ModelResponse(
                _message("answer", MessageRole.ASSISTANT, "The dependency is absent."),
                FinishReason.STOP,
            ),
        )
        self.assertIs(decision.action, FinalizationAction.ACCEPT)
        self.assertEqual(
            decision.reason_code, "latest_delivery_action_failed_blocker_visible",
        )

    async def test_mutation_without_post_change_verification_gets_one_turn(self) -> None:
        state = self._state()
        state.visible_tool_by_name = {
            "core.apply_patch": self._spec(
                "core.apply_patch", ToolEffect.MUTATE,
                ToolResultAuthority.MUTATION_FACT,
            ),
        }
        state.messages = self._tool_exchange(
            "call", "core.apply_patch", {"path": "app.py"},
            ToolResult("call", True, data={"mutation_id": "mutation-1"}),
        )
        decision = await RuntimeFinalizationGate().evaluate(
            state, ModelResponse(
                _message("answer", MessageRole.ASSISTANT, "Changed app.py."),
                FinishReason.STOP,
            ),
        )
        self.assertIs(decision.action, FinalizationAction.CONTINUE)
        self.assertEqual(
            decision.reason_code, "post_mutation_verification_missing",
        )

    async def test_failed_post_change_verification_gets_one_turn(self) -> None:
        state = self._state()
        state.visible_tool_by_name = {
            "core.apply_patch": self._spec(
                "core.apply_patch", ToolEffect.MUTATE,
                ToolResultAuthority.MUTATION_FACT,
            ),
            "core.run_command": self._spec(
                "core.run_command", ToolEffect.EXECUTE,
                ToolResultAuthority.PROCESS_FACT,
            ),
        }
        state.messages = self._tool_exchange(
            "patch", "core.apply_patch", {"path": "app.py"},
            ToolResult("patch", True, data={"mutation_id": "mutation-1"}),
        ) + self._tool_exchange(
            "test", "core.run_command", {"argv": ["python", "-m", "pytest"]},
            ToolResult(
                "test", True,
                data={"succeeded": False, "failure_code": "PROCESS_EXIT_NON_ZERO"},
            ),
        )
        decision = await RuntimeFinalizationGate().evaluate(
            state, ModelResponse(
                _message("answer", MessageRole.ASSISTANT, "The test failed."),
                FinishReason.STOP,
            ),
        )
        self.assertIs(decision.action, FinalizationAction.CONTINUE)
        self.assertEqual(
            decision.reason_code, "post_mutation_verification_failed",
        )


class CompletionReviewConfigurationTest(unittest.TestCase):
    def test_completion_review_is_opt_in_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration = load_completion_review_configuration(
                Path(directory) / ".env", {}
            )
        self.assertEqual(configuration.mode.value, "off")
        self.assertEqual(configuration.failure_policy.value, "visible_unverified")

    def test_env_file_and_environment_override_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "TSM_AGT_COMPLETION_REVIEW_MODE=off\n"
                "TSM_AGT_COMPLETION_REVIEW_MAX_CONTINUATIONS=4\n",
                encoding="utf-8",
            )
            configuration = load_completion_review_configuration(
                path, {"TSM_AGT_COMPLETION_REVIEW_MODE": "always"}
            )
        self.assertEqual(configuration.mode.value, "always")
        self.assertEqual(configuration.max_continuations, 4)
