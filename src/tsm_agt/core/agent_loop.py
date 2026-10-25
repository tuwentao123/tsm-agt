"""Results and terminal errors for one bounded Agent loop."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from tsm_agt.ports import (
    FinishReason, Message, ModelRecoveryAction, ModelTransportProgress,
    ModelUsage, ToolCall,
)
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
    REASONING_DELTA = "reasoning_delta"
    COMMENTARY = "commentary"
    MODEL_STARTED = "model_started"
    MODEL_COMPLETED = "model_completed"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    WRAP_UP = "wrap_up"
    EXPLORATION = "exploration"
    MODEL_RETRY = "model_retry"
    MODEL_TRANSPORT = "model_transport"
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
    transport_mode: str = ""
    transport_event: str = ""
    diagnostic_code: str = ""
    diagnostic_detail: str = ""
    recovery_action: str = ""
    reasoning_text: str = ""

    def to_data(self) -> dict[str, Any]:
        """Return the live UI payload.

        This is intentionally not a redacted audit projection.  The caller is
        an authenticated, loopback UI or the local CLI and needs the concrete
        goal, question, paths, queries, and tool arguments to explain what the
        Agent is doing.
        """
        return {
            "kind": self.kind.value,
            "reasoning_text": self.reasoning_text,
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
            "transport_mode": self.transport_mode,
            "transport_event": self.transport_event,
            "diagnostic_code": self.diagnostic_code,
            "diagnostic_detail": self.diagnostic_detail,
            "recovery_action": self.recovery_action,
        }


class ProviderCapabilityMismatch(RuntimeError):
    pass


class AgentCheckpointConflict(RuntimeError):
    pass


class ToolBatchStatus(StrEnum):
    """Durable lifecycle of one assistant message's tool-call set.

    A batch is the protocol boundary between one assistant message and all of
    its Tool Results.  Individual tools may run sequentially, but the Runtime
    may not sample the model or inject user input until the batch is closed.
    """

    ACCEPTED = "ACCEPTED"
    EXECUTING = "EXECUTING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    RECONCILING = "RECONCILING"


@dataclass(frozen=True, slots=True)
class ToolBatchSnapshot:
    """Frozen, restart-safe record of one accepted model tool batch."""

    batch_id: str
    source_message_id: str
    calls: tuple[ToolCall, ...]
    pending_call_ids: tuple[str, ...]
    status: ToolBatchStatus
    task_spec_revision: int
    execution_focus_revision: int = 0
    # Retained only when decoding a historical batch. New batches never emit it.
    legacy_execution_focus_revision: bool = False

    def __post_init__(self) -> None:
        call_ids = tuple(call.call_id for call in self.calls)
        if not self.batch_id or not self.source_message_id or not self.calls:
            raise ValueError("tool batch identity and calls are required")
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("tool batch call IDs must be unique")
        if not set(self.pending_call_ids).issubset(call_ids):
            raise ValueError("tool batch pending calls must belong to the batch")
        if self.status is ToolBatchStatus.CLOSED and self.pending_call_ids:
            raise ValueError("closed tool batch cannot contain pending calls")

    @property
    def is_open(self) -> bool:
        return self.status not in {
            ToolBatchStatus.CLOSED, ToolBatchStatus.CANCELLED,
        }

    def with_pending(
        self, pending_calls: tuple[ToolCall, ...], *,
        status: ToolBatchStatus | None = None,
    ) -> ToolBatchSnapshot:
        pending_ids = tuple(call.call_id for call in pending_calls)
        resolved_status = status or (
            ToolBatchStatus.EXECUTING
            if pending_ids else ToolBatchStatus.CLOSED
        )
        return ToolBatchSnapshot(
            self.batch_id, self.source_message_id, self.calls, pending_ids,
            resolved_status, self.task_spec_revision,
            self.execution_focus_revision, self.legacy_execution_focus_revision,
        )

    def to_data(self) -> dict[str, Any]:
        data = {
            "batch_id": self.batch_id,
            "source_message_id": self.source_message_id,
            "calls": [call.to_data() for call in self.calls],
            "pending_call_ids": list(self.pending_call_ids),
            "status": self.status.value,
            "task_spec_revision": self.task_spec_revision,
        }
        if self.legacy_execution_focus_revision:
            data["execution_focus_revision"] = self.execution_focus_revision
        return data

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ToolBatchSnapshot:
        raw_calls = data.get("calls")
        raw_pending = data.get("pending_call_ids")
        if not isinstance(raw_calls, list) or not isinstance(raw_pending, list):
            raise ValueError("tool batch calls and pending IDs must be lists")
        return cls(
            str(data["batch_id"]), str(data["source_message_id"]),
            tuple(ToolCall.from_data(item) for item in raw_calls),
            tuple(str(item) for item in raw_pending),
            ToolBatchStatus(str(data["status"])),
            int(data.get("task_spec_revision", 0)),
            int(data.get("execution_focus_revision", 0)),
            "execution_focus_revision" in data,
        )


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
    #: Per-turn reasoning-effort preference for the main conversation loop.
    #: When set, this value is passed as ``reasoning_effort`` on the
    #: ``ModelRequest``; routing, planning and review calls keep their
    #: bootstrap defaults.  The Session remains the long-lived source of
    #: truth and may be copied here when the checkpoint is bound.
    reasoning_effort: str | None = None
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
    read_hits_state: Mapping[str, Any] = field(default_factory=dict)
    artifact_read_state: Mapping[str, Any] = field(default_factory=dict)
    progressive_scope_state: Mapping[str, Any] = field(default_factory=dict)
    exploration_budget_state: Mapping[str, Any] = field(default_factory=dict)
    stop_or_pivot_state: Mapping[str, Any] = field(default_factory=dict)
    evidence_relation_state: Mapping[str, Any] = field(default_factory=dict)
    rejection_loop_state: Mapping[str, Any] = field(default_factory=dict)
    exploration_outcome_state: Mapping[str, Any] = field(default_factory=dict)
    task_spec_revision: int = 0
    task_spec_hash: str = ""
    # Historical checkpoint payload retained solely for integrity-checked replay.
    execution_focus: Mapping[str, Any] = field(default_factory=dict)
    legacy_execution_focus: bool = False
    tool_batch: ToolBatchSnapshot | None = None
    # Deprecated migration input retained solely for historical replay.
    active_outcome_ids: tuple[str, ...] = ()
    legacy_active_outcome_ids: bool = False
    pending_user_action: Mapping[str, Any] = field(default_factory=dict)
    model_budget_renewal_count: int = 0
    model_budget_total_granted: int = 0
    checkpoint_schema_version: int = 2
    finalization_state: Mapping[str, Any] = field(default_factory=dict)
    enabled_skills: tuple[str, ...] = ()

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> AgentTurnCheckpoint:
        raw_messages = data.get("messages")
        raw_calls = data.get("pending_tool_calls")
        raw_seen = data.get("seen_call_ids")
        if not isinstance(raw_messages, list) or not isinstance(raw_calls, list):
            raise ValueError("agent checkpoint messages and calls must be lists")
        if not isinstance(raw_seen, list):
            raise ValueError("agent checkpoint seen_call_ids must be a list")
        schema_version = int(data.get("checkpoint_schema_version", 1))
        if schema_version == 1:
            return load_checkpoint_v1(data)
        stored_hash = data.get("checkpoint_hash")
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
            reasoning_effort=(
                str(data["reasoning_effort"])
                if data.get("reasoning_effort") is not None else None
            ),
            checkpoint_schema_version=2,
            finalization_state=(
                dict(data["finalization_state"])
                if isinstance(data.get("finalization_state"), Mapping) else {}
            ),
            model_budget_renewal_count=int(
                data.get("model_budget_renewal_count", 0)
            ),
            model_budget_total_granted=int(
                data.get("model_budget_total_granted", 0)
            ),
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
            rejection_loop_state=(
                dict(data["rejection_loop_state"])
                if isinstance(data.get("rejection_loop_state"), Mapping) else {}
            ),
            task_spec_revision=int(data.get("task_spec_revision", 0)),
            task_spec_hash=str(data.get("task_spec_hash", "")),
            execution_focus=(
                dict(data["execution_focus"])
                if isinstance(data.get("execution_focus"), Mapping) else {}
            ),
            legacy_execution_focus="execution_focus" in data,
            enabled_skills=tuple(
                str(skill_id)
                for skill_id in data.get("enabled_skills", [])
            ),
            tool_batch=(
                ToolBatchSnapshot.from_data(data["tool_batch"])
                if isinstance(data.get("tool_batch"), Mapping) else None
            ),
            active_outcome_ids=tuple(
                str(item) for item in data.get("active_outcome_ids", [])
            ),
            legacy_active_outcome_ids="active_outcome_ids" in data,
            pending_user_action=(
                dict(data["pending_user_action"])
                if isinstance(data.get("pending_user_action"), Mapping) else {}
            ),
        )
        if (
            checkpoint.model_budget_renewal_count < 0
            or checkpoint.model_budget_total_granted < 0
        ):
            raise ValueError("agent checkpoint budget renewal values must not be negative")
        if (
            schema_version >= 2 and stored_hash is not None
            and str(stored_hash) != checkpoint.checkpoint_hash
        ):
            legacy_content = checkpoint._content_data()
            evolved_fields = (
                "rejection_loop_state",
                "model_budget_renewal_count", "model_budget_total_granted",
                "task_spec_revision", "task_spec_hash",
                "execution_focus", "active_outcome_ids",
                "pending_user_action", "tool_batch",
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
            "checkpoint_schema_version": 2,
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
            "reasoning_effort": self.reasoning_effort,
            "finalization_state": dict(self.finalization_state or {}),
            "model_budget_renewal_count": self.model_budget_renewal_count,
            "model_budget_total_granted": self.model_budget_total_granted,
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
            "read_hits_state": dict(self.read_hits_state or {}),
            "artifact_read_state": dict(self.artifact_read_state or {}),
            "progressive_scope_state": dict(self.progressive_scope_state or {}),
            "exploration_budget_state": dict(
                self.exploration_budget_state or {}
            ),
            "stop_or_pivot_state": dict(self.stop_or_pivot_state or {}),
            "rejection_loop_state": dict(self.rejection_loop_state or {}),
            "task_spec_revision": self.task_spec_revision,
            "task_spec_hash": self.task_spec_hash,
            **(
                {"execution_focus": dict(self.execution_focus or {})}
                if self.legacy_execution_focus else {}
            ),
            "tool_batch": (
                self.tool_batch.to_data() if self.tool_batch is not None else None
            ),
            **(
                {"active_outcome_ids": list(self.active_outcome_ids)}
                if self.legacy_active_outcome_ids else {}
            ),
            "pending_user_action": dict(self.pending_user_action or {}),
        }

    @property
    def checkpoint_hash(self) -> str:
        return canonical_hash(self._content_data())

    def to_data(self) -> dict[str, Any]:
        return {**self._content_data(), "checkpoint_hash": self.checkpoint_hash}


_LEGACY_CHECKPOINT_PROJECTION_FIELDS = frozenset({
    "evidence_question_state",
    "completion_readiness_state",
    "evidence_relation_state",
    "exploration_outcome_state",
})
_LEGACY_TOOL_CALL_FIELDS = frozenset({"evidence_question", "evidence_intent"})


def load_checkpoint_v1(data: Mapping[str, Any]) -> AgentTurnCheckpoint:
    """Verify one v1 payload before dropping its retired projections.

    The integrity check intentionally uses the exact historical envelope.  The
    conversion only happens afterwards, so a valid v1 checkpoint is never
    measured against the v2 canonical form.
    """
    stored_hash = data.get("checkpoint_hash")
    if stored_hash is not None:
        legacy_payload = dict(data)
        legacy_payload.pop("checkpoint_hash", None)
        if str(stored_hash) != canonical_hash(legacy_payload):
            raise ValueError("agent checkpoint integrity hash does not match")
    legacy_envelope_observed = (
        "evidence_question_state" in data
        or "completion_readiness_state" in data
        or any(
            isinstance(call, Mapping)
            and any(key in call for key in _LEGACY_TOOL_CALL_FIELDS)
            for call in data.get("pending_tool_calls", [])
        )
    )
    upgraded = {
        key: value
        for key, value in data.items()
        if key not in _LEGACY_CHECKPOINT_PROJECTION_FIELDS
        and key != "checkpoint_hash"
    }
    raw_calls = upgraded.get("pending_tool_calls", [])
    if isinstance(raw_calls, list):
        upgraded["pending_tool_calls"] = [
            {
                key: value for key, value in call.items()
                if key not in _LEGACY_TOOL_CALL_FIELDS
            }
            if isinstance(call, Mapping) else call
            for call in raw_calls
        ]
    upgraded["checkpoint_schema_version"] = 2
    upgraded["finalization_state"] = {
        "legacy_evidence_envelope_observed": legacy_envelope_observed,
    }
    return AgentTurnCheckpoint.from_data(upgraded)


@dataclass(slots=True)
class TurnState:
    """Every local the agent turn threads through its loop body.

    Mirrors the reference loop state (the conversation loop's state holder),
    including two of its choices that look like sloppiness and are not:

    * **Most fields are ``Any``.** The reference implementation types only
      ``retry_count`` and ``finish_reason``; everything else is opaque to the
      state holder. The loop
      passes values through without interpreting them, and naming a type per
      field would add an import edge per field for no reader. Concrete types
      appear below only where the initial value carries meaning.
    * **Slots are rebound before any later reader.** That docstring is explicit:
      "Per-iteration slots are rebound by the phases before any later phase reads
      them, exactly as the former inline locals were."

    The point of the object is the contract stated in that same docstring: a new
    phase input or output needs **a field here and nothing else**. Until this
    exists, splitting the 2,133-line turn body
    (``core/kernel.py::_continue_agent_turn``) would only trade one god function
    for several tightly-coupled ones -- every extracted phase would have to take
    twenty-odd values as parameters and return as many.

    Nothing mutates this yet: the turn body still uses its inline locals, and the
    type is introduced first so the split has somewhere to land.
    """

    # Fixed for the turn.
    task_id: str = ""
    turn_id: str = ""
    visible_tools: Any = ()
    initial_tool_message: Any = None
    recover_interrupted: bool = False
    on_text_delta: Any = None
    on_progress: Any = None

    # Resolved once during the prologue.
    live_spec: Any = None
    closed_outcomes_at_entry: Any = None
    checkpoint: Any = None
    visible_tool_by_name: Any = None

    # Per-iteration slots, rebound as the loop progresses.
    messages: Any = None
    pending_tool_calls: Any = None
    seen_call_ids: Any = None
    model_call_count: int = 0
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_tool_error: Any = None
    plan_guard: Any = None

    # Bounded-correction and wrap-up bookkeeping. These start false/zero and are
    # latched by the phases that own them -- the fields a future ``_run_phase``
    # would copy back by name rather than clear.
    no_progress_stop: bool = False
    budget_wrap_up: bool = False
    budget_wrap_up_decision: Any = None
    protocol_recovery_attempts: int = 0
    protocol_correction: Any = None
    #: Latched once when a text answer tried to conclude on discovered paths
    #: alone, so the conversation loop asks at most once and never loops.
    review_continuations: int = 0
    #: Protocol-only recovery prompts are separately bounded from completion
    #: review. They exist for an execution that never started, not to second
    #: guess a known tool outcome or consume the whole model-call budget.
    runtime_continuations: int = 0
    #: A rejected workspace edit gets one bounded opportunity to recover before
    #: an immediate draft-only answer becomes an explicitly partial boundary.
    edit_recovery_continuations: int = 0
    reviewer_call_count: int = 0
    last_candidate_hash: str = ""
    last_review_reason_code: str = ""

    # Produced by the model-call preparation phase and consumed by the call and
    # response phases. Each of these is an intra-iteration fact, not turn-level
    # state: it is recomputed every time the loop goes round, which is why none
    # of them is checkpointed.
    model_call_number: int = 0
    remaining_model_calls: int = 0
    completion_state: Any = None
    legacy_completion_gate: bool = False
    disclosure_only: bool = False
    wrap_up: bool = False
    recovery_batch: Any = None
    prompt: Any = None
    request: Any = None
    prepared: Any = None
    live_goal: Any = None
    model_started_at: float = 0.0
    should_buffer_text: bool = False
    buffered_text: Any = None

    # Produced by the model-call phase and consumed by the response phase.
    response: Any = None
    tool_calls: Any = None
    accepted_batch: Any = None


class PhaseAction(StrEnum):
    """What the caller must do once a phase hands control back.

    Mirrors the reference agent's single control-flow vocabulary
    (``agent/conversation_loop.py:1641-1678``): every phase answers with an
    ``action`` and the loop dispatches on it, so a phase that must end the
    iteration says so instead of breaking out of a loop it does not own.

    Every member must be one the loop actually branches on. ``Kernel.run_agent_turn``
    handles ``RETURN`` and ``CONTINUE`` at explicit branch points and treats ``OK``
    as the fall-through for the rest of the iteration. There is deliberately no
    ``BREAK``: a member the loop does not branch on is a silent no-op, and one sat
    here that way until it was removed.
    """

    OK = "ok"
    CONTINUE = "continue"
    RETURN = "return"


@dataclass(frozen=True, slots=True)
class PhaseVerdict:
    """A phase's answer: what to do next, plus an optional turn result.

    ``result`` is read only for ``RETURN``. Anything else a phase wants to hand
    back belongs on ``TurnState`` -- which is what keeps this carrier from
    growing into a parameter list.
    """

    action: PhaseAction = PhaseAction.OK
    result: Any = None


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
class AgentTurnIncomplete:
    """A completion claim could not be repaired inside the current turn budget."""

    task_id: str
    turn_id: str
    revision: int
    reason: str
    gaps: tuple[Mapping[str, Any], ...]


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
    kind: str = "QUESTION"

    @property
    def input_mode(self) -> str:
        return "single_choice" if self.choices else "free_text"


@dataclass(frozen=True, slots=True)
class AgentContinuationSuspended:
    """A completed unit is visible, while later required Outcomes remain.

    Unlike approval or clarification this grants no authority and carries no
    secret token.  The next ordinary Session input is semantically routed by
    the configured resolver before Runtime allows the same Task to continue.
    """

    task_id: str
    turn_id: str
    revision: int
    completed_outcome_ids: tuple[str, ...]
    remaining_outcome_ids: tuple[str, ...]
    assistant_message: Message


async def record_model_transport_progress(
    kernel: Any, state: TurnState, update: ModelTransportProgress,
    *, goal: str,
) -> None:
    """Record and surface one physical model-attempt transition.

    Both loops owe the same thing: the durable attempt ledger and the live
    progress stream must see each transition before the provider continues, and
    a transport fallback has to read as one. A buffered batch made a
    five-minute stall look like a single instantaneous event after recovery
    finished.
    """

    await kernel._record_model_attempt_events(
        state.task_id, state.turn_id, state.model_call_number, [update]
    )
    retry_actions = {
        ModelRecoveryAction.RETRY_SAME_REQUEST.value,
        ModelRecoveryAction.RESAMPLE.value,
        ModelRecoveryAction.RESAMPLE_WITH_CORRECTION.value,
        ModelRecoveryAction.FALLBACK_TRANSPORT.value,
    }
    is_retry = (
        update.kind == "recovery_decided"
        and update.recovery_action in retry_actions
    )
    reason = update.reason
    if update.recovery_action == ModelRecoveryAction.FALLBACK_TRANSPORT.value:
        reason = "stream_fallback:" + reason
    kernel._notify_agent_progress(state.on_progress, AgentProgress(
        AgentProgressKind.MODEL_RETRY if is_retry
        else AgentProgressKind.MODEL_TRANSPORT,
        model_call=state.model_call_number,
        max_model_calls=state.checkpoint.max_model_calls,
        tool_call=state.tool_call_count,
        max_tool_calls=state.checkpoint.max_tool_calls,
        goal=goal,
        reason=reason,
        transport_attempt=update.attempt,
        max_transport_attempts=update.max_attempts,
        retry_delay_seconds=update.delay_seconds,
        transport_mode=update.transport_mode,
        transport_event=update.kind,
        diagnostic_code=update.diagnostic_code,
        diagnostic_detail=update.diagnostic_detail,
        recovery_action=update.recovery_action,
    ))
