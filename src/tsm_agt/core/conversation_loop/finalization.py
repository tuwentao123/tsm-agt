"""Deterministic and semantic finalization stages for Conversation Loop."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
import time
from typing import Any
from uuid import uuid4

from tsm_agt.ports import (
    CompletionReviewAction, CompletionReviewMode, CompletionReviewVerdict,
    FinalizationAction, FinalizationCandidate, FinalizationDecision,
    FinishReason, Message, MessageRole, TextBlock, ToolCallBlock,
    ToolExecutionSummary, ToolResultBlock,
)

from ..agent_loop import TurnState
from ..task_spec import TaskCriterionKind
from ..tool_result_governor import _sanitize_value
from .action_facts import (
    latest_failed_delivery_action, post_mutation_verification_fact,
    project_runtime_action_facts,
)


_RUNTIME_CONTINUATION = (
    "Continue the same turn. Complete the response or issue the tool call still required."
)

_UNRESOLVED_EXECUTION_CONTINUATION = (
    "Continue the same turn once. Runtime records that a requested execution never "
    "started. Use the advertised tools to make a safe next attempt, or give an "
    "evidence-based blocker that does not claim the work was completed."
)

_FAILED_ACTION_CONTINUATION = (
    "Continue the same turn once. Runtime observed that the latest delivery "
    "action failed. Decide the next legal recovery or an evidence-based blocker; "
    "do not merely restate the failed result or claim the request was completed."
)

_POST_MUTATION_VERIFICATION_CONTINUATION = (
    "Continue the same turn once. Runtime recorded a workspace mutation without "
    "a successful recognized post-change verification. Decide whether to run an "
    "available verification, perform another necessary action, or state a "
    "specific evidence-based reason verification is not applicable. Do not call "
    "the change verified without evidence."
)

_FAILED_VERIFICATION_CONTINUATION = (
    "Continue the same turn once. Runtime recorded that the latest recognized "
    "post-change verification did not pass. Decide a legal recovery or state a "
    "specific evidence-based blocker; do not claim the requested change verified."
)

# This is deliberately independent of the user-facing model-call budget.  A
# protocol fact can request one corrective turn; it must never turn a known
# blocker into dozens of empty model completions.
_MAX_RUNTIME_RECOVERY_CONTINUATIONS = 1


class RuntimeFinalizationGate:
    """Enforce protocol invariants without interpreting the user's intent."""

    async def evaluate(
        self, state: TurnState, response: Any,
    ) -> FinalizationDecision:
        has_budget = state.model_call_count < state.checkpoint.max_model_calls
        if response.finish_reason is FinishReason.CANCELLED:
            return FinalizationDecision(
                FinalizationAction.INCOMPLETE, "model_cancelled"
            )
        if response.finish_reason is FinishReason.ERROR:
            return FinalizationDecision(FinalizationAction.FAILED, "model_error")
        if response.finish_reason is FinishReason.LENGTH:
            return FinalizationDecision(
                FinalizationAction.CONTINUE if has_budget else FinalizationAction.INCOMPLETE,
                "output_truncated" if has_budget else "output_truncated_budget_exhausted",
                _RUNTIME_CONTINUATION if has_budget else "",
            )
        if state.pending_tool_calls:
            return FinalizationDecision(
                FinalizationAction.FAILED, "pending_tool_calls_at_finalization"
            )
        batch = state.checkpoint.tool_batch
        if batch is not None and batch.pending_call_ids:
            return FinalizationDecision(
                FinalizationAction.FAILED, "tool_batch_not_terminal"
            )
        # A pending user action is normally returned by the tool-batch path
        # before a candidate can be built. Keep this deterministic guard so a
        # malformed resumed checkpoint can never be committed as success.
        if state.checkpoint.pending_user_action:
            return FinalizationDecision(
                FinalizationAction.INCOMPLETE, "pending_user_action"
            )
        unmatched = _unmatched_tool_call_ids(state.messages)
        if unmatched:
            return FinalizationDecision(
                FinalizationAction.FAILED, "unmatched_tool_call"
            )
        if not response.message.text.strip():
            return FinalizationDecision(
                FinalizationAction.CONTINUE if has_budget else FinalizationAction.INCOMPLETE,
                "empty_final_response" if has_budget else "empty_response_budget_exhausted",
                _RUNTIME_CONTINUATION if has_budget else "",
            )
        if _latest_unstarted_execution(state.messages):
            can_attempt_recovery = (
                state.runtime_continuations
                < _MAX_RUNTIME_RECOVERY_CONTINUATIONS
                and has_budget
            )
            return FinalizationDecision(
                (
                    FinalizationAction.CONTINUE
                    if can_attempt_recovery else FinalizationAction.ACCEPT
                ),
                (
                    "unresolved_execution_outcome"
                    if can_attempt_recovery else "unstarted_execution_blocker_visible"
                ),
                _UNRESOLVED_EXECUTION_CONTINUATION if can_attempt_recovery else "",
            )
        action_facts = project_runtime_action_facts(
            state.messages, state.visible_tool_by_name,
        )
        post_mutation = post_mutation_verification_fact(action_facts)
        action_issue = (
            (
                "post_mutation_verification_failed",
                _FAILED_VERIFICATION_CONTINUATION,
            ) if post_mutation.status == "failed" else
            (
                "post_mutation_verification_missing",
                _POST_MUTATION_VERIFICATION_CONTINUATION,
            ) if post_mutation.status in {"missing", "unknown"} else
            (
                "latest_delivery_action_failed",
                _FAILED_ACTION_CONTINUATION,
            ) if latest_failed_delivery_action(action_facts) is not None else None
        )
        if action_issue is not None:
            reason, instruction = action_issue
            can_attempt_recovery = (
                state.runtime_continuations
                < _MAX_RUNTIME_RECOVERY_CONTINUATIONS
                and has_budget
            )
            return FinalizationDecision(
                FinalizationAction.CONTINUE if can_attempt_recovery
                else FinalizationAction.ACCEPT,
                reason if can_attempt_recovery else reason + "_blocker_visible",
                instruction if can_attempt_recovery else "",
            )
        return FinalizationDecision(FinalizationAction.ACCEPT, "runtime_state_terminal")


def _latest_unstarted_execution(messages: tuple[Message, ...]) -> bool:
    """Read only runtime lifecycle facts, never the model's prose or intent.

    ``failed_known`` is a terminal execution fact: the process did run and its
    failure is evidence the model may report or recover from. It is not an
    *unstarted-execution* veto. The bounded action-fact rule is evaluated
    separately below. Only ``not_started`` lacks an execution fact for the
    proposed action and needs this protocol correction.

    A completed tool batch is an execution boundary. Older results cannot keep
    an unrelated later answer from finalizing.
    """
    for message in reversed(messages):
        results = [
            block.result for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        if not results:
            continue
        for result in reversed(results):
            fields: dict[str, Any] = {}
            if isinstance(result.data, dict):
                fields.update(result.data)
            if isinstance(result.recovery_action, dict):
                fields.update(result.recovery_action)
            if isinstance(result.meta, dict):
                fields.update(result.meta)
            state = str(fields.get("execution_state", "")).strip().lower()
            if state == "not_started":
                return True
            if state in {"succeeded", "failed_known", "running"}:
                return False
        # A completed tool batch is an execution boundary.  Older failures do
        # not keep a later, unrelated result from finalizing forever.
        return False
    return False


class ConversationFinalizationPipeline:
    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel
        self._runtime_gate = RuntimeFinalizationGate()

    async def build_candidate(
        self, state: TurnState, response: Any,
    ) -> FinalizationCandidate:
        source = self._kernel._source_user_message(state.checkpoint.messages)
        if source is None:
            raise RuntimeError("conversation loop lost its source user message")
        calls: dict[str, Any] = {}
        results: dict[str, Any] = {}
        for message in state.messages:
            for block in message.content:
                if isinstance(block, ToolCallBlock):
                    calls[block.call.call_id] = block.call
                elif isinstance(block, ToolResultBlock):
                    results[block.result.call_id] = block.result
        summaries: list[ToolExecutionSummary] = []
        remaining_excerpt = 12_000
        presenter = self._kernel._dependencies.tool_argument_presenter
        for call_id, call in calls.items():
            result = results.get(call_id)
            if result is None:
                continue
            arguments = dict(call.arguments)
            if presenter is not None:
                arguments = dict(
                    presenter.present(call.name, call.arguments).visible_arguments
                )
            raw_content = result.data if result.data is not None else {
                "message": result.message,
                "hint": result.hint,
            }
            safe_content, _ = _sanitize_value(raw_content)
            safe_meta, _ = _sanitize_value(result.meta)
            raw_excerpt = json.dumps(
                safe_content, ensure_ascii=False, sort_keys=True, default=str,
            )
            excerpt = raw_excerpt[:min(2_000, remaining_excerpt)]
            remaining_excerpt -= len(excerpt)
            summaries.append(ToolExecutionSummary(
                call_id=call_id, tool_name=call.name, arguments=arguments,
                ok=result.ok, error_code=result.error_code or "",
                truncated=result.truncated, result_excerpt=excerpt,
                result_meta=dict(safe_meta) if isinstance(safe_meta, dict) else {},
            ))
        candidate = FinalizationCandidate(
            task_id=state.task_id, turn_id=state.turn_id,
            source_user_message=source, assistant_message=response.message,
            finish_reason=response.finish_reason,
            tool_executions=tuple(summaries), model_calls=state.model_call_count,
            tool_calls=state.tool_call_count,
            review_continuations=state.review_continuations,
            available_write_tools=tuple(
                tool.name for tool in state.visible_tools
                if tool.effect.value == "mutate"
            ),
        )
        await self._kernel._append_events(state.task_id, ((
            "turn.finalization_candidate_created", {
                "turn_id": state.turn_id,
                "orchestration_mode": "CONVERSATION_LOOP",
                "candidate_hash": self.candidate_hash(candidate),
                "tool_execution_count": len(candidate.tool_executions),
                "model_call": candidate.model_calls,
                "tool_calls": candidate.tool_calls,
                "review_continuations": candidate.review_continuations,
            },
        ),))
        return candidate

    async def runtime_gate(
        self, state: TurnState, response: Any,
    ) -> FinalizationDecision:
        decision = await self._runtime_gate.evaluate(state, response)
        await self._kernel._append_events(state.task_id, ((
            "turn.finalization_runtime_evaluated", {
                "turn_id": state.turn_id,
                "orchestration_mode": "CONVERSATION_LOOP",
                "model_call": state.model_call_count,
                "tool_calls": state.tool_call_count,
                "action": decision.action.value,
                "reason_code": decision.reason_code,
            },
        ),))
        return decision

    async def run_stop_policies(
        self, candidate: FinalizationCandidate,
    ) -> CompletionReviewVerdict | None:
        for policy in self._kernel._dependencies.turn_stop_policies:
            try:
                decision = await policy.evaluate(candidate)
            except Exception as error:
                await self._kernel._append_events(candidate.task_id, ((
                    "turn.stop_policy_evaluated", {
                        "turn_id": candidate.turn_id,
                        "orchestration_mode": "CONVERSATION_LOOP",
                        "action": FinalizationAction.FAILED.value,
                        "reason_code": "stop_policy_failed",
                        "policy": type(policy).__name__,
                        "error_type": type(error).__name__,
                    },
                ),))
                raise RuntimeError("stop_policy_failed") from error
            await self._kernel._append_events(candidate.task_id, ((
                "turn.stop_policy_evaluated", {
                    "turn_id": candidate.turn_id,
                    "orchestration_mode": "CONVERSATION_LOOP",
                    "action": (
                        decision.action.value if decision is not None else "SKIP"
                    ),
                    "reason_code": (
                        decision.reason_code if decision is not None else "not_applicable"
                    ),
                    "policy": type(policy).__name__,
                },
            ),))
            if decision is not None and decision.action is CompletionReviewAction.CONTINUE:
                return decision
        return None

    async def review(
        self, state: TurnState, candidate: FinalizationCandidate,
    ) -> CompletionReviewVerdict:
        dependencies = self._kernel._dependencies
        if (
            dependencies.completion_review_mode is CompletionReviewMode.OFF
            or dependencies.completion_reviewer is None
        ):
            return CompletionReviewVerdict(
                CompletionReviewAction.ACCEPT, "review_disabled",
                "completion review is disabled",
            )
        if (
            dependencies.completion_review_mode is CompletionReviewMode.REQUIRED
            and not await self._task_requires_completion_review(candidate.task_id)
        ):
            await self._kernel._append_events(candidate.task_id, ((
                "turn.completion_review_skipped", {
                    "turn_id": candidate.turn_id,
                    "orchestration_mode": "CONVERSATION_LOOP",
                    "reason_code": "no_explicit_completion_contract",
                },
            ),))
            return CompletionReviewVerdict(
                CompletionReviewAction.ACCEPT, "review_not_required",
                "the Task has no explicit evidence or delivery contract",
            )
        state.reviewer_call_count += 1
        started_at = time.monotonic()
        await self._kernel._append_events(state.task_id, ((
            "turn.completion_review_started", {
                "turn_id": state.turn_id,
                "orchestration_mode": "CONVERSATION_LOOP",
                "candidate_hash": self.candidate_hash(candidate),
                "reviewer_call_count": state.reviewer_call_count,
                "review_continuations": state.review_continuations,
            },
        ),))
        verdict = await dependencies.completion_reviewer.review(candidate)
        state.input_tokens += verdict.input_tokens
        state.output_tokens += verdict.output_tokens
        await self._kernel._append_events(state.task_id, ((
            "turn.completion_review_completed", {
                "turn_id": state.turn_id,
                "orchestration_mode": "CONVERSATION_LOOP",
                "candidate_hash": self.candidate_hash(candidate),
                "reviewer_call_count": state.reviewer_call_count,
                "action": verdict.action.value,
                "reason_code": verdict.reason_code,
                "review_continuations": state.review_continuations,
                "usage": {
                    "input_tokens": verdict.input_tokens,
                    "output_tokens": verdict.output_tokens,
                },
                "latency_ms": round((time.monotonic() - started_at) * 1000, 2),
            },
        ),))
        return verdict

    def can_continue(self, state: TurnState) -> bool:
        return (
            state.review_continuations
            < min(self._kernel._dependencies.completion_review_max_continuations, 1)
            and state.model_call_count < state.checkpoint.max_model_calls
        )

    async def _task_requires_completion_review(self, task_id: str) -> bool:
        """Use an explicit Task contract, never a text-intent heuristic.

        Completion review is a quality guard for evidence/delivery work, not a
        hidden classifier for ordinary messages.  Runtime-authored goal
        alignment alone is intentionally insufficient.
        """
        spec = await self._kernel.get_task_spec(task_id)
        required_kinds = frozenset({
            TaskCriterionKind.WORKSPACE_INTEGRITY,
            TaskCriterionKind.EVIDENCE_REFERENCE,
            TaskCriterionKind.EXTERNAL_SOURCE_READ,
            TaskCriterionKind.REQUIRED_EFFECT,
        })
        return any(
            criterion.verification_kind in required_kinds
            for criterion in spec.acceptance_criteria
        )

    async def append_runtime_continuation(
        self, state: TurnState, decision: FinalizationDecision,
    ) -> None:
        state.runtime_continuations += 1
        payload = {
            "type": "runtime_finalization_continuation",
            "reason": decision.reason_code,
            "instruction": decision.instruction or _RUNTIME_CONTINUATION,
        }
        await self._append_scaffolding(state, payload, "runtime-finalization")

    async def append_review_continuation(
        self, state: TurnState, verdict: CompletionReviewVerdict, *, source: str,
    ) -> None:
        state.review_continuations += 1
        state.last_review_reason_code = verdict.reason_code
        payload = {
            "type": "completion_review_continuation",
            "source": source,
            "reason_code": verdict.reason_code,
            "reason": verdict.reason,
            "missing": list(verdict.missing),
            "instruction": (
                "Continue the same task. Decide the next action and tools yourself. "
                "Do not merely restate the draft answer."
            ),
        }
        await self._append_scaffolding(state, payload, "completion-review")
        await self._kernel._append_events(state.task_id, ((
            "turn.completion_review_continuation", {
                "turn_id": state.turn_id,
                "orchestration_mode": "CONVERSATION_LOOP",
                "source": source,
                "reason_code": verdict.reason_code,
                "review_continuations": state.review_continuations,
            },
        ),))

    async def persist_finalization_state(self, state: TurnState, reason: str) -> None:
        state.checkpoint = replace(
            state.checkpoint,
            messages=tuple(state.messages),
            finalization_state={
                "review_continuations": state.review_continuations,
                "runtime_continuations": state.runtime_continuations,
                "reviewer_call_count": state.reviewer_call_count,
                "last_candidate_hash": state.last_candidate_hash,
                "last_review_reason_code": state.last_review_reason_code,
            },
        )
        await self._kernel._save_agent_checkpoint(state.checkpoint, reason)

    @staticmethod
    def candidate_hash(candidate: FinalizationCandidate) -> str:
        normalized = re.sub(r"\s+", " ", candidate.assistant_message.text).strip()
        return "sha256:" + hashlib.sha256(normalized.encode()).hexdigest()

    async def _append_scaffolding(
        self, state: TurnState, payload: dict[str, Any], kind: str,
    ) -> None:
        state.messages.append(Message(
            f"{kind}-{uuid4().hex}", MessageRole.USER,
            (TextBlock(json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )),), scaffolding=kind,
        ))
        await self.persist_finalization_state(state, f"conversation-{kind}")


def _unmatched_tool_call_ids(messages: list[Message]) -> tuple[str, ...]:
    calls: set[str] = set()
    results: set[str] = set()
    for message in messages:
        for block in message.content:
            if isinstance(block, ToolCallBlock):
                calls.add(block.call.call_id)
            elif isinstance(block, ToolResultBlock):
                results.add(block.result.call_id)
    return tuple(sorted(calls - results))


__all__ = ["ConversationFinalizationPipeline", "RuntimeFinalizationGate"]
