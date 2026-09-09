"""Results and terminal errors for one bounded Agent loop."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from tsm_agt.ports import FinishReason, Message, ModelUsage, ToolCall
from .configuration import canonical_hash


class AgentLoopLimitExceeded(RuntimeError):
    def __init__(
        self, turn_id: str, limit: str, maximum: int, *,
        model_calls: int = 0, tool_calls: int = 0,
        last_tool_error: str | None = None,
        max_model_calls: int | None = None, max_tool_calls: int | None = None,
    ) -> None:
        detail = (
            f" after model_calls={model_calls}, tool_calls={tool_calls}"
            if model_calls or tool_calls else ""
        )
        last = f"; last tool issue: {last_tool_error}" if last_tool_error else ""
        super().__init__(
            f"agent loop limit exceeded for {turn_id}: {limit}={maximum}"
            f"{detail}{last}"
        )
        self.turn_id = turn_id
        self.limit = limit
        self.maximum = maximum
        self.model_calls = model_calls
        self.tool_calls = tool_calls
        self.last_tool_error = last_tool_error
        self.max_model_calls = (
            maximum if limit == "max_model_calls" else (max_model_calls or 0)
        )
        self.max_tool_calls = (
            maximum if limit == "max_tool_calls" else (max_tool_calls or 0)
        )


class AgentProgressKind(StrEnum):
    MODEL_STARTED = "model_started"
    MODEL_COMPLETED = "model_completed"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    WRAP_UP = "wrap_up"
    EXPLORATION = "exploration"
    MODEL_RETRY = "model_retry"
    FOCUS = "focus"


@dataclass(frozen=True, slots=True)
class AgentProgress:
    kind: AgentProgressKind
    model_call: int = 0
    max_model_calls: int = 0
    tool_call: int = 0
    max_tool_calls: int = 0
    tool_name: str = ""
    ok: bool | None = None
    error_code: str | None = None
    elapsed_seconds: float = 0.0
    evidence_delta: int | None = None
    consecutive_zero_delta: int = 0
    phase: str = ""
    activity: str = ""
    question_ref: str = ""
    scope: str = "unknown"
    scope_change: str = "unknown"
    budget_score: int | None = None
    value_band: str = ""
    next_action: str = ""
    reason: str = ""
    # The fields below are deliberately live-only.  The Kernel sends them to
    # the process-local callback used by CLI/Web UI clients, but never writes
    # them to the durable Event Log.
    goal: str = ""
    question: str = ""
    operation: str = ""
    operation_arguments: Mapping[str, Any] = field(default_factory=dict)
    operation_presentation: str = ""
    operation_presentation_mode: str = "full"
    scope_target: str = ""
    scope_change_reason: str = ""
    budget_reason: str = ""
    exploration_actions: int = 0
    max_exploration_actions: int = 0
    exploration_tool_calls: int = 0
    max_exploration_tool_calls: int = 0
    exploration_elapsed_seconds: float = 0.0
    max_exploration_elapsed_seconds: float = 0.0
    low_value_streak: int = 0
    max_low_value_streak: int = 0
    reserve_tool_calls: int = 0
    transport_attempt: int = 0
    max_transport_attempts: int = 0
    retry_delay_seconds: float = 0.0

    def to_data(self) -> dict[str, Any]:
        """Return the live UI payload.

        This is intentionally not a redacted audit projection.  The caller is
        an authenticated, loopback UI or the local CLI and needs the concrete
        goal, question, paths, queries, and tool arguments to explain what the
        Agent is doing.
        """
        return {
            "kind": self.kind.value,
            "model_call": self.model_call,
            "max_model_calls": self.max_model_calls,
            "tool_call": self.tool_call,
            "max_tool_calls": self.max_tool_calls,
            "tool_name": self.tool_name,
            "ok": self.ok,
            "error_code": self.error_code,
            "elapsed_seconds": self.elapsed_seconds,
            "evidence_delta": self.evidence_delta,
            "consecutive_zero_delta": self.consecutive_zero_delta,
            "phase": self.phase,
            "activity": self.activity,
            "question_ref": self.question_ref,
            "scope": self.scope,
            "scope_change": self.scope_change,
            "budget_score": self.budget_score,
            "value_band": self.value_band,
            "next_action": self.next_action,
            "reason": self.reason,
            "goal": self.goal,
            "question": self.question,
            "operation": self.operation,
            "operation_arguments": dict(self.operation_arguments),
            "operation_presentation": self.operation_presentation,
            "operation_presentation_mode": self.operation_presentation_mode,
            "scope_target": self.scope_target,
            "scope_change_reason": self.scope_change_reason,
            "budget_reason": self.budget_reason,
            "exploration_actions": self.exploration_actions,
            "max_exploration_actions": self.max_exploration_actions,
            "exploration_tool_calls": self.exploration_tool_calls,
            "max_exploration_tool_calls": self.max_exploration_tool_calls,
            "exploration_elapsed_seconds": self.exploration_elapsed_seconds,
            "max_exploration_elapsed_seconds": (
                self.max_exploration_elapsed_seconds
            ),
            "low_value_streak": self.low_value_streak,
            "max_low_value_streak": self.max_low_value_streak,
            "reserve_tool_calls": self.reserve_tool_calls,
            "transport_attempt": self.transport_attempt,
            "max_transport_attempts": self.max_transport_attempts,
            "retry_delay_seconds": self.retry_delay_seconds,
        }


class ProviderCapabilityMismatch(RuntimeError):
    pass


class AgentCheckpointConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AgentTurnCheckpoint:
    task_id: str
    turn_id: str
    revision: int
    messages: tuple[Message, ...]
    pending_tool_calls: tuple[ToolCall, ...]
    seen_call_ids: tuple[str, ...]
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    max_model_calls: int
    max_tool_calls: int
    max_output_tokens: int
    tool_timeout_seconds: float
    workspace_fingerprint: str = ""
    effective_config_hash: str = ""
    toolset_hash: str = ""
    prompt_manifest_hash: str = ""
    session_id: str = ""
    session_context_hash: str = ""
    working_memory_hash: str = ""
    last_steering_inbound_sequence: int = 0
    goal_revision: int = 1
    action_progress: Mapping[str, Any] = field(default_factory=dict)
    evidence_inventory: Mapping[str, Any] = field(default_factory=dict)
    evidence_question_state: Mapping[str, Any] = field(default_factory=dict)
    read_hits_state: Mapping[str, Any] = field(default_factory=dict)
    artifact_read_state: Mapping[str, Any] = field(default_factory=dict)
    progressive_scope_state: Mapping[str, Any] = field(default_factory=dict)
    exploration_budget_state: Mapping[str, Any] = field(default_factory=dict)
    stop_or_pivot_state: Mapping[str, Any] = field(default_factory=dict)
    evidence_relation_state: Mapping[str, Any] = field(default_factory=dict)
    rejection_loop_state: Mapping[str, Any] = field(default_factory=dict)
    exploration_outcome_state: Mapping[str, Any] = field(default_factory=dict)
    completion_readiness_state: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AgentTurnCheckpoint:
        raw_messages = data.get("messages")
        raw_calls = data.get("pending_tool_calls")
        raw_seen = data.get("seen_call_ids")
        if not isinstance(raw_messages, list) or not isinstance(raw_calls, list):
            raise ValueError("agent checkpoint messages and calls must be lists")
        if not isinstance(raw_seen, list):
            raise ValueError("agent checkpoint seen_call_ids must be a list")
        checkpoint = cls(
            task_id=str(data["task_id"]),
            turn_id=str(data["turn_id"]),
            revision=int(data["revision"]),
            messages=tuple(Message.from_data(item) for item in raw_messages),
            pending_tool_calls=tuple(ToolCall.from_data(item) for item in raw_calls),
            seen_call_ids=tuple(str(item) for item in raw_seen),
            model_calls=int(data["model_calls"]),
            tool_calls=int(data["tool_calls"]),
            input_tokens=int(data["input_tokens"]),
            output_tokens=int(data["output_tokens"]),
            max_model_calls=int(data["max_model_calls"]),
            max_tool_calls=int(data["max_tool_calls"]),
            max_output_tokens=int(data["max_output_tokens"]),
            tool_timeout_seconds=float(data["tool_timeout_seconds"]),
            workspace_fingerprint=str(data.get("workspace_fingerprint", "")),
            effective_config_hash=str(data.get("effective_config_hash", "")),
            toolset_hash=str(data.get("toolset_hash", "")),
            prompt_manifest_hash=str(data.get("prompt_manifest_hash", "")),
            session_id=str(data.get("session_id", "")),
            session_context_hash=str(data.get("session_context_hash", "")),
            working_memory_hash=str(data.get("working_memory_hash", "")),
            last_steering_inbound_sequence=int(
                data.get("last_steering_inbound_sequence", 0)
            ),
            goal_revision=int(data.get("goal_revision", 1)),
            action_progress=(
                dict(data["action_progress"])
                if isinstance(data.get("action_progress"), Mapping) else {}
            ),
            evidence_inventory=(
                dict(data["evidence_inventory"])
                if isinstance(data.get("evidence_inventory"), Mapping) else {}
            ),
            evidence_question_state=(
                dict(data["evidence_question_state"])
                if isinstance(data.get("evidence_question_state"), Mapping) else {}
            ),
            read_hits_state=(
                dict(data["read_hits_state"])
                if isinstance(data.get("read_hits_state"), Mapping) else {}
            ),
            artifact_read_state=(
                dict(data["artifact_read_state"])
                if isinstance(data.get("artifact_read_state"), Mapping) else {}
            ),
            progressive_scope_state=(
                dict(data["progressive_scope_state"])
                if isinstance(data.get("progressive_scope_state"), Mapping) else {}
            ),
            exploration_budget_state=(
                dict(data["exploration_budget_state"])
                if isinstance(data.get("exploration_budget_state"), Mapping) else {}
            ),
            stop_or_pivot_state=(
                dict(data["stop_or_pivot_state"])
                if isinstance(data.get("stop_or_pivot_state"), Mapping) else {}
            ),
            evidence_relation_state=(
                dict(data["evidence_relation_state"])
                if isinstance(data.get("evidence_relation_state"), Mapping) else {}
            ),
            rejection_loop_state=(
                dict(data["rejection_loop_state"])
                if isinstance(data.get("rejection_loop_state"), Mapping) else {}
            ),
            exploration_outcome_state=(
                dict(data["exploration_outcome_state"])
                if isinstance(data.get("exploration_outcome_state"), Mapping) else {}
            ),
            completion_readiness_state=(
                dict(data["completion_readiness_state"])
                if isinstance(data.get("completion_readiness_state"), Mapping) else {}
            ),
        )
        stored_hash = data.get("checkpoint_hash")
        if stored_hash is not None and str(stored_hash) != checkpoint.checkpoint_hash:
            legacy_content = checkpoint._content_data()
            evolved_fields = (
                "evidence_relation_state", "rejection_loop_state",
                "exploration_outcome_state", "evidence_question_state",
                "completion_readiness_state",
            )
            missing_fields = tuple(
                field for field in evolved_fields if field not in data
            )
            for field in missing_fields:
                legacy_content.pop(field, None)
            if (
                not missing_fields
                or str(stored_hash) != canonical_hash(legacy_content)
            ):
                raise ValueError("agent checkpoint integrity hash does not match")
        return checkpoint

    def _content_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "revision": self.revision,
            "messages": [message.to_data() for message in self.messages],
            "pending_tool_calls": [call.to_data() for call in self.pending_tool_calls],
            "seen_call_ids": list(self.seen_call_ids),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "max_model_calls": self.max_model_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_output_tokens": self.max_output_tokens,
            "tool_timeout_seconds": self.tool_timeout_seconds,
            "workspace_fingerprint": self.workspace_fingerprint,
            "effective_config_hash": self.effective_config_hash,
            "toolset_hash": self.toolset_hash,
            "prompt_manifest_hash": self.prompt_manifest_hash,
            "session_id": self.session_id,
            "session_context_hash": self.session_context_hash,
            "working_memory_hash": self.working_memory_hash,
            "last_steering_inbound_sequence": (
                self.last_steering_inbound_sequence
            ),
            "goal_revision": self.goal_revision,
            "action_progress": dict(self.action_progress or {}),
            "evidence_inventory": dict(self.evidence_inventory or {}),
            "evidence_question_state": dict(
                self.evidence_question_state or {}
            ),
            "read_hits_state": dict(self.read_hits_state or {}),
            "artifact_read_state": dict(self.artifact_read_state or {}),
            "progressive_scope_state": dict(self.progressive_scope_state or {}),
            "exploration_budget_state": dict(
                self.exploration_budget_state or {}
            ),
            "stop_or_pivot_state": dict(self.stop_or_pivot_state or {}),
            "evidence_relation_state": dict(self.evidence_relation_state or {}),
            "rejection_loop_state": dict(self.rejection_loop_state or {}),
            "exploration_outcome_state": dict(
                self.exploration_outcome_state or {}
            ),
            "completion_readiness_state": dict(
                self.completion_readiness_state or {}
            ),
        }

    @property
    def checkpoint_hash(self) -> str:
        return canonical_hash(self._content_data())

    def to_data(self) -> dict[str, Any]:
        return {**self._content_data(), "checkpoint_hash": self.checkpoint_hash}


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    turn_id: str
    task_id: str
    assistant_message: Message
    finish_reason: FinishReason
    usage: ModelUsage
    model_calls: int
    tool_calls: int
    messages: tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class AgentTurnSuspended:
    task_id: str
    turn_id: str
    revision: int
    approval_request_id: str
    payload_hash: str
    action: str
    target: str
    preview: str
    risk: str
    network_access: str
    data_transmission: str
    rollback: str
    approval_kind: str = "tool_action"


@dataclass(frozen=True, slots=True)
class AgentClarificationSuspended:
    task_id: str
    turn_id: str
    revision: int
    request_id: str
    question: str
    choices: tuple[tuple[str, str], ...]
    reason: str
    required: bool
    resume_token: str
