"""Provider-neutral tool contracts shared by the kernel and adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .adapter import RuntimeAdapter
from .workspace_path import WorkspacePathPort


class ToolRisk(StrEnum):
    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    R4 = "R4"


class ToolIdempotency(StrEnum):
    IDEMPOTENT = "idempotent"
    KEYED = "keyed"
    NON_IDEMPOTENT = "non_idempotent"


class ToolEffect(StrEnum):
    """Primary effect of a model-visible tool.

    The effect describes what the action does; it does not grant authority or
    replace the existing risk/approval policy.  ``UNSPECIFIED`` preserves the
    conservative behavior of third-party tools created before this metadata
    existed.
    """

    UNSPECIFIED = "unspecified"
    OBSERVE = "observe"
    MUTATE = "mutate"
    EXECUTE = "execute"
    INTERACT = "interact"
    CONTROL = "control"
    INTERNAL = "internal"


class ToolResultAuthority(StrEnum):
    """Kind of fact a successful result is allowed to establish."""

    UNSPECIFIED = "unspecified"
    NONE = "none"
    USER_INTENT = "user_intent"
    WORKSPACE_FACT = "workspace_fact"
    PROCESS_FACT = "process_fact"
    MUTATION_FACT = "mutation_fact"
    RUNTIME_FACT = "runtime_fact"
    EXTERNAL_SERVICE = "external_service"
    DERIVED = "derived"


class ToolProtocol(StrEnum):
    """How the Runtime coordinates an action with its host environment."""

    IMMEDIATE = "immediate"
    WAIT_USER = "wait_user"
    POLICY_GATED = "policy_gated"
    BACKGROUND_CAPABLE = "background_capable"


class ToolRecoveryKind(StrEnum):
    """Machine-readable recovery semantics for a failed tool invocation.

    The producer that understands the failure assigns the category. The Agent
    loop consumes it without parsing human error text or choosing a business
    action on the model's behalf.
    """

    NONE = "none"
    RETRY_SAME = "retry_same"
    RETRY_AFTER_STATE_CHANGE = "retry_after_state_change"
    USER_ACTION_REQUIRED = "user_action_required"
    TERMINAL = "terminal"
    UNKNOWN_OUTCOME = "unknown_outcome"


class OutcomeBindingMode(StrEnum):
    """Runtime-owned meaning of a ToolCall→Outcome relationship."""

    FULFILLMENT = "FULFILLMENT"
    SUPPORTING = "SUPPORTING"


class WebEgressMode(StrEnum):
    """Which component decides where an outbound document fetch may go.

    ``DIRECT`` means the fetching process opens the socket itself, so it owns
    the decision: resolve the host once, refuse any non-public answer, then
    connect to that exact address so a second resolution cannot substitute an
    internal host (DNS rebinding).

    ``DELEGATED`` means the destination is reached through infrastructure the
    process does not control - a forward proxy, VPN, or TUN device. Locally
    resolved addresses then describe the tunnel rather than the destination, so
    classifying them in-process would assert a guarantee that does not exist.
    Deployments choosing this mode must enforce egress in that infrastructure.
    """

    DIRECT = "DIRECT"
    DELEGATED = "DELEGATED"


@dataclass(frozen=True, slots=True)
class EvidenceQuestion:
    """The concrete unknown that one model-requested tool call should resolve."""

    question_id: str
    question: str
    scope_expansion_reason: str = ""
    expected_scope: str = ""

    def __post_init__(self) -> None:
        normalized_id = self.question_id.strip()
        normalized_question = self.question.strip()
        normalized_reason = self.scope_expansion_reason.strip()
        normalized_scope = self.expected_scope.strip()
        if not normalized_id or len(normalized_id) > 64:
            raise ValueError("evidence question_id must contain 1..64 characters")
        if not normalized_question or len(normalized_question) > 500:
            raise ValueError("evidence question must contain 1..500 characters")
        if len(normalized_reason) > 500:
            raise ValueError("scope expansion reason must contain at most 500 characters")
        if len(normalized_scope) > 1000:
            raise ValueError("expected scope must contain at most 1000 characters")

    def to_data(self) -> dict[str, str]:
        data = {
            "question_id": self.question_id.strip(),
            "question": self.question.strip(),
        }
        if self.scope_expansion_reason.strip():
            data["scope_expansion_reason"] = self.scope_expansion_reason.strip()
        if self.expected_scope.strip():
            data["expected_scope"] = self.expected_scope.strip()
        return data

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> EvidenceQuestion:
        return cls(
            question_id=str(data.get("question_id", "")),
            question=str(data.get("question", "")),
            scope_expansion_reason=str(data.get("scope_expansion_reason", "")),
            expected_scope=str(data.get("expected_scope", "")),
        )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]
    risk: ToolRisk
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    idempotency: ToolIdempotency = ToolIdempotency.NON_IDEMPOTENT
    max_result_tokens: int = 2000
    requires_network: bool = False
    data_transmission: str = "none"
    rollback: str = "tool-specific; review before approval"
    is_internal_state: bool = False
    effect: ToolEffect = ToolEffect.UNSPECIFIED
    result_authority: ToolResultAuthority = ToolResultAuthority.UNSPECIFIED
    protocol: ToolProtocol = ToolProtocol.IMMEDIATE

    def __post_init__(self) -> None:
        if (
            "." not in self.name
            or any(part == "" for part in self.name.split("."))
        ):
            raise ValueError("tool name must be a non-empty dotted name")
        if not self.description.strip():
            raise ValueError("tool description must not be empty")
        if self.parameters.get("type") != "object":
            raise ValueError("tool parameters must be an object schema")
        if self.max_result_tokens <= 0:
            raise ValueError("max_result_tokens must be positive")
        if not self.data_transmission.strip():
            raise ValueError("tool data_transmission must not be empty")
        if not self.rollback.strip():
            raise ValueError("tool rollback description must not be empty")
        if self.is_internal_state and (
            self.risk is not ToolRisk.R0
            or self.requires_network
            or self.data_transmission != "none"
        ):
            raise ValueError(
                "internal state tools must be R0 with no network or transmission"
            )
        if self.effect is ToolEffect.INTERACT and (
            self.result_authority is not ToolResultAuthority.USER_INTENT
            or self.protocol is not ToolProtocol.WAIT_USER
        ):
            raise ValueError(
                "interaction tools must return user_intent via wait_user protocol"
            )

    @property
    def requires_evidence_question(self) -> bool:
        """Whether model calls need a fact-finding obligation envelope.

        ``UNSPECIFIED`` deliberately stays conservative for existing external
        adapters.  User interaction, mutation journals and Runtime control facts
        have their own lifecycle and must not be forced into Evidence Questions.
        """

        return self.result_authority in {
            ToolResultAuthority.UNSPECIFIED,
            ToolResultAuthority.WORKSPACE_FACT,
            ToolResultAuthority.PROCESS_FACT,
            ToolResultAuthority.EXTERNAL_SERVICE,
            ToolResultAuthority.DERIVED,
        }

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
            "risk": self.risk.value,
            "is_read_only": self.is_read_only,
            "is_concurrency_safe": self.is_concurrency_safe,
            "idempotency": self.idempotency.value,
            "max_result_tokens": self.max_result_tokens,
            "requires_network": self.requires_network,
            "data_transmission": self.data_transmission,
            "rollback": self.rollback,
            "is_internal_state": self.is_internal_state,
            "effect": self.effect.value,
            "result_authority": self.result_authority.value,
            "protocol": self.protocol.value,
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    evidence_question: EvidenceQuestion | None = None
    outcome_ref: str | None = None
    # Runtime-owned and persisted after binding. Provider adapters never accept
    # this value from model-generated arguments.
    outcome_binding_mode: OutcomeBindingMode = OutcomeBindingMode.FULFILLMENT

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            raise ValueError("tool call_id must not be empty")
        if not self.name.strip():
            raise ValueError("tool name must not be empty")
        if self.outcome_ref is not None and not self.outcome_ref.strip():
            raise ValueError("tool outcome_ref must not be empty")

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "call_id": self.call_id,
            "name": self.name,
            "arguments": dict(self.arguments),
        }
        if self.evidence_question is not None:
            data["evidence_question"] = self.evidence_question.to_data()
        if self.outcome_ref is not None:
            data["outcome_ref"] = self.outcome_ref
        if self.outcome_binding_mode is not OutcomeBindingMode.FULFILLMENT:
            data["outcome_binding_mode"] = self.outcome_binding_mode.value
        return data

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ToolCall:
        arguments = data.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise ValueError("tool call arguments must be an object")
        raw_question = data.get("evidence_question")
        if raw_question is not None and not isinstance(raw_question, Mapping):
            raise ValueError("tool call evidence_question must be an object")
        return cls(
            call_id=str(data["call_id"]),
            name=str(data["name"]),
            arguments=dict(arguments),
            evidence_question=(
                EvidenceQuestion.from_data(raw_question)
                if isinstance(raw_question, Mapping) else None
            ),
            outcome_ref=(
                str(data["outcome_ref"])
                if data.get("outcome_ref") is not None else None
            ),
            outcome_binding_mode=OutcomeBindingMode(str(
                data.get("outcome_binding_mode", "FULFILLMENT")
            )),
        )


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    ok: bool
    data: Any | None = None
    error_code: str | None = None
    message: str | None = None
    hint: str | None = None
    retryable: bool = False
    recovery_kind: ToolRecoveryKind = ToolRecoveryKind.NONE
    recovery_action: Mapping[str, Any] = field(default_factory=dict)
    truncated: bool = False
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            raise ValueError("tool result call_id must not be empty")
        if self.ok and self.error_code is not None:
            raise ValueError("successful tool result cannot have an error_code")
        if not self.ok and not self.error_code:
            raise ValueError("failed tool result must have an error_code")
        if self.ok and self.recovery_kind is not ToolRecoveryKind.NONE:
            raise ValueError("successful tool result cannot require recovery")
        if self.ok and self.recovery_action:
            raise ValueError("successful tool result cannot have a recovery action")

    @property
    def effective_recovery_kind(self) -> ToolRecoveryKind:
        """Interpret legacy retryable results without breaking old adapters."""
        if self.recovery_kind is not ToolRecoveryKind.NONE:
            return self.recovery_kind
        if self.error_code == "UNKNOWN_OUTCOME":
            return ToolRecoveryKind.UNKNOWN_OUTCOME
        if self.retryable:
            return ToolRecoveryKind.RETRY_SAME
        return ToolRecoveryKind.NONE if self.ok else ToolRecoveryKind.TERMINAL

    def to_data(self) -> dict[str, Any]:
        result = {
            "call_id": self.call_id,
            "ok": self.ok,
            "data": self.data,
            "error_code": self.error_code,
            "message": self.message,
            "hint": self.hint,
            "retryable": self.retryable,
            "truncated": self.truncated,
            "meta": dict(self.meta),
        }
        if not self.ok:
            result["recovery_kind"] = self.effective_recovery_kind.value
            if self.recovery_action:
                result["recovery_action"] = dict(self.recovery_action)
        return result

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ToolResult:
        meta = data.get("meta", {})
        if not isinstance(meta, Mapping):
            raise ValueError("tool result meta must be an object")
        recovery_action = data.get("recovery_action", {})
        if not isinstance(recovery_action, Mapping):
            raise ValueError("tool result recovery_action must be an object")
        return cls(
            call_id=str(data["call_id"]),
            ok=bool(data["ok"]),
            data=data.get("data"),
            error_code=(
                str(data["error_code"]) if data.get("error_code") is not None else None
            ),
            message=(str(data["message"]) if data.get("message") is not None else None),
            hint=str(data["hint"]) if data.get("hint") is not None else None,
            retryable=bool(data.get("retryable", False)),
            recovery_kind=ToolRecoveryKind(
                str(data.get("recovery_kind", "none"))
            ),
            recovery_action=dict(recovery_action),
            truncated=bool(data.get("truncated", False)),
            meta=dict(meta),
        )


class ToolProcessControl(Protocol):
    """Task-scoped process operations exposed to an authorized tool call."""

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str,
        environment: Mapping[str, str],
        background: bool,
        timeout_seconds: float,
        termination_grace_seconds: float,
        max_output_bytes: int,
        max_lifetime_seconds: float,
        stop_on_task_end: bool,
    ) -> Mapping[str, Any]: ...

    async def status(self, process_id: str) -> Mapping[str, Any]: ...

    async def logs(
        self, process_id: str, stdout_cursor: int, stderr_cursor: int
    ) -> Mapping[str, Any]: ...

    async def stop(
        self, process_id: str, grace_seconds: float
    ) -> Mapping[str, Any]: ...


class ToolWorkspaceControl(Protocol):
    """Task-scoped workspace mutation available only to authorized calls."""

    async def apply_patch(
        self, path: str, expected_hash: str | None,
        edits: tuple[Mapping[str, str], ...],
    ) -> Mapping[str, Any]: ...

    async def apply_patches(
        self, patches: tuple[Mapping[str, Any], ...],
    ) -> tuple[Mapping[str, Any], ...]: ...

    async def delete_file(
        self, path: str, expected_hash: str
    ) -> Mapping[str, Any]: ...

    async def rollback_mutation(
        self, mutation_id: str
    ) -> Mapping[str, Any]: ...

    async def rollback_mutations(
        self, mutation_ids: tuple[str, ...]
    ) -> tuple[Mapping[str, Any], ...]: ...

    async def rollback_mutation_batch(
        self, mutation_ids: tuple[str, ...]
    ) -> tuple[Mapping[str, Any], ...]: ...

    async def rollback_mutation_groups(
        self, mutation_groups: tuple[tuple[str, ...], ...]
    ) -> tuple[Mapping[str, Any], ...]: ...


class ToolMemoryControl(Protocol):
    """Task-scoped durable memory operations exposed after Kernel policy."""

    async def list(self, include_stale: bool) -> tuple[Mapping[str, Any], ...]: ...

    async def remember(
        self, scope: str, content: str, source_kind: str,
        source_reference: str, source_hash: str | None, operation_id: str,
    ) -> Mapping[str, Any]: ...

    async def verify(
        self, memory_id: str, source_hash: str | None, operation_id: str,
    ) -> Mapping[str, Any]: ...

    async def forget(
        self, memory_id: str, operation_id: str,
    ) -> Mapping[str, Any]: ...


class ToolWorkingMemoryControl(Protocol):
    """Task-local scratchpad operations; never grants durable Memory access."""

    async def read(self) -> Mapping[str, Any]: ...

    async def update(
        self, expected_revision: int, state: Mapping[str, Any], operation_id: str,
    ) -> Mapping[str, Any]: ...


class ToolTaskSpecControl(Protocol):
    """Task completion-contract operations; never grants workspace authority."""

    async def read(self) -> Mapping[str, Any]: ...

    async def update(
        self, expected_revision: int, scope: tuple[str, ...],
        constraints: tuple[str, ...],
        acceptance_criteria: tuple[Mapping[str, Any], ...],
        operation_id: str,
    ) -> Mapping[str, Any]: ...

    async def select_outcomes(
        self, outcome_ids: tuple[str, ...], reason: str, source_input_id: str,
    ) -> Mapping[str, Any]: ...

    async def complete_outcome(
        self, outcome_id: str, completion_summary: str,
        evidence_refs: tuple[str, ...], remaining_work: tuple[str, ...],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ToolInvocationContext:
    invocation_id: str
    task_id: str
    turn_id: str
    workspace: Path
    deadline: datetime
    policy_decision_id: str = ""
    authorized_risk: ToolRisk = ToolRisk.R0
    payload_hash: str = ""
    approval_request_id: str | None = None
    idempotency_key: str | None = None
    process_control: ToolProcessControl | None = None
    workspace_control: ToolWorkspaceControl | None = None
    memory_control: ToolMemoryControl | None = None
    working_memory_control: ToolWorkingMemoryControl | None = None
    task_spec_control: ToolTaskSpecControl | None = None
    workspace_path: WorkspacePathPort | None = None
    additional_read_roots: tuple[Path, ...] = ()
    resource_paths: Mapping[str, str] = field(default_factory=dict)
    resource_candidates: Mapping[str, tuple[Mapping[str, str], ...]] = field(
        default_factory=dict
    )


class ToolProviderPort(RuntimeAdapter, Protocol):
    async def list_tools(self) -> tuple[ToolSpec, ...]: ...

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult: ...
