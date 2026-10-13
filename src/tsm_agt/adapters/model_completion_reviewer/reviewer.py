"""Bounded, tool-free semantic review of a proposed final Agent answer."""

from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, CompletionReviewAction,
    CompletionReviewError, CompletionReviewerPort, CompletionReviewVerdict,
    FinalizationCandidate, FinishReason, HealthState, HealthStatus, Message,
    MessageRole, ModelCallPurpose, ModelProviderPort, ModelRequest, TextBlock,
)


_SYSTEM_PROMPT = """You are a turn completion reviewer, not a task execution agent.
You cannot call tools and must not provide a new business answer.

Judge whether the candidate answer satisfies the current user request.
1. Stable general knowledge may be answered without tools.
2. Claims about a current workspace, source repository, web page, external service,
   current state, or performed action require relevant successful tool results.
3. Search hits, repository resolution, directory listings, and paths only prove a
   location was found. Implementation analysis requires actual source content or
   an equivalent read result.
4. Do not reject merely because few tools were used.
5. A legitimate clarification, permission blocker, or unavailable-error report
   may be accepted.
6. ``available_write_tools`` is a Runtime fact. If it is non-empty, an answer
   must not claim that no write tool or write permission exists merely because
   it did not call one. For a request to change/create a workspace artifact,
   such a claim means the requested action is unfinished: return CONTINUE.
7. Return CONTINUE only if a step that materially changes the answer is unfinished.
8. Describe missing facts; never prescribe a tool or tool arguments.

Return exactly one JSON object matching:
{"action":"ACCEPT|CONTINUE","reason_code":"1..64 chars","reason":"1..1000 chars","missing":["up to 8 items"]}
"""


class ModelCompletionReviewer:
    descriptor = AdapterDescriptor(
        "model.completion-reviewer", "1.0", "CompletionReviewerPort", "1.0",
        frozenset({"tool-free", "structured-verdict", "bounded-review"}),
    )

    def __init__(self, model: ModelProviderPort) -> None:
        self._model = model
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "completion reviewer ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def review(
        self, candidate: FinalizationCandidate,
    ) -> CompletionReviewVerdict:
        if not self._started:
            raise CompletionReviewError("completion reviewer is not started")
        payload = {
            "user_message": candidate.source_user_message.text,
            "candidate_answer": candidate.assistant_message.text,
            "finish_reason": candidate.finish_reason.value,
            "tool_executions": [item.to_data() for item in candidate.tool_executions],
            "has_mutation": any(
                str(item.result_meta.get("effect", "")).lower() == "mutate"
                for item in candidate.tool_executions
            ),
            "has_tool_failure": any(not item.ok for item in candidate.tool_executions),
            "has_truncated_result": any(
                item.truncated for item in candidate.tool_executions
            ),
            "available_write_tools": list(candidate.available_write_tools),
            "review_continuations": candidate.review_continuations,
        }
        response = await self._model.complete(ModelRequest(
            turn_id=f"completion-review-{candidate.turn_id}-{uuid4().hex}",
            messages=(
                Message(
                    f"completion-review-system-{uuid4().hex}", MessageRole.SYSTEM,
                    (TextBlock(_SYSTEM_PROMPT),),
                ),
                Message(
                    f"completion-review-input-{uuid4().hex}", MessageRole.USER,
                    (TextBlock(json.dumps(
                        payload, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), default=str,
                    )),),
                ),
            ),
            tools=(), allow_tool_calls=False, max_output_tokens=800,
            purpose=ModelCallPurpose.COMPLETION_REVIEW,
        ))
        if response.finish_reason is not FinishReason.STOP:
            raise CompletionReviewError(
                f"reviewer finish reason was {response.finish_reason.value}"
            )
        try:
            raw = json.loads(response.message.text.strip())
            if not isinstance(raw, dict):
                raise TypeError("review verdict is not an object")
            missing = raw.get("missing", [])
            if not isinstance(missing, list) or any(
                not isinstance(item, str) for item in missing
            ):
                raise TypeError("review missing is not a string array")
            return CompletionReviewVerdict(
                CompletionReviewAction(str(raw["action"])),
                str(raw["reason_code"]), str(raw["reason"]), tuple(missing),
                response.usage.input_tokens, response.usage.output_tokens,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise CompletionReviewError("reviewer returned an invalid verdict") from error


__all__ = ["ModelCompletionReviewer"]
