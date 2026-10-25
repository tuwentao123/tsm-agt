"""Provider-neutral tool contracts shared by the kernel and adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .adapter import RuntimeAdapter
from .process import (
    BackgroundProcessStartResult, ProcessCommandResult, ProcessWaitResult,
)
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


class ToolResultMaterialization(StrEnum):
    """How much source material a successful tool result actually exposes.

    This describes the result that reached the model, not the tool's general
    capability and never implies that the user request is complete.  In
    particular, a search hit is a ``POINTER``/``LOCATOR`` while an opened file
    or document is ``CONTENT``.  ``VERIFICATION`` is reserved for an explicit
    task contract or skill; an exited command is only a runtime observation.
    """

    UNKNOWN = "unknown"
    POINTER = "pointer"
    METADATA = "metadata"
    LOCATOR = "locator"
    CONTENT = "content"
    ACTION_RECEIPT = "action_receipt"
    RUNTIME_OBSERVATION = "runtime_observation"
    VERIFICATION = "verification"
    DERIVED = "derived"


class ToolFactCapability(StrEnum):
    """Specific statements a result can support without interpreting it."""

    LOCATE_RESOURCE = "locate_resource"
    REPORT_METADATA = "report_metadata"
    DESCRIBE_CONTENT = "describe_content"
    REPORT_ACTION = "report_action"
    REPORT_RUNTIME_STATE = "report_runtime_state"
    REPORT_VERIFICATION = "report_verification"
    DESCRIBE_DERIVED = "describe_derived"


@dataclass(frozen=True, slots=True)
class ModelToolContract:
    """Concise, model-visible use protocol for one tool.

    Runtime policy fields remain on :class:`ToolSpec`; this contract only
    explains selection and interpretation to the model.  It deliberately has
    no completion field: a tool can explain what it returned without deciding
    whether the user's task is finished.
    """

    purpose: str
    use_when: tuple[str, ...] = ()
    avoid_when: tuple[str, ...] = ()
    returns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.purpose.strip():
            raise ValueError("model tool contract purpose must not be empty")
        for name in ("use_when", "avoid_when", "returns"):
            values = getattr(self, name)
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(
                    f"model tool contract {name} must contain non-empty strings"
                )

    def to_data(self) -> dict[str, object]:
        return {
            "purpose": self.purpose,
            "use_when": list(self.use_when),
            "avoid_when": list(self.avoid_when),
            "returns": list(self.returns),
        }

    def render(self) -> str:
        sections = [self.purpose]
        if self.use_when:
            sections.append("Use when: " + "; ".join(self.use_when))
        if self.avoid_when:
            sections.append("Do not use when: " + "; ".join(self.avoid_when))
        if self.returns:
            sections.append("Returns: " + "; ".join(self.returns))
        return "\n".join(sections)


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
    RETRY_WITH_CORRECTED_ARGUMENTS = "retry_with_corrected_arguments"
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
    model_contract: ModelToolContract | None = None

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
        if self.model_contract is not None and not isinstance(
            self.model_contract, ModelToolContract
        ):
            raise ValueError("tool model_contract must be ModelToolContract")

    def model_description(self) -> str:
        """Return the concise protocol sent to model providers.

        Existing third-party tools retain their original description.  Built-in
        tools can opt into a small, structured selection contract without
        exposing Runtime-only approval and scheduling internals.
        """
        return (
            self.model_contract.render()
            if self.model_contract is not None else self.description
        )

    def to_data(self) -> dict[str, Any]:
        data: dict[str, object] = {
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
        if self.model_contract is not None:
            data["model_contract"] = self.model_contract.to_data()
        return data


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
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
        return cls(
            call_id=str(data["call_id"]),
            name=str(data["name"]),
            arguments=dict(arguments),
            outcome_ref=(
                str(data["outcome_ref"])
                if data.get("outcome_ref") is not None else None
            ),
            outcome_binding_mode=OutcomeBindingMode(str(
                data.get("outcome_binding_mode", "FULFILLMENT")
            )),
        )


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    """An immutable source file that can support implementation-level claims."""

    source_kind: str
    repository: str
    commit_sha: str
    path: str
    blob_sha: str
    canonical_url: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.source_kind != "github":
            raise ValueError("source artifact kind must be github")
        repository_parts = self.repository.split("/")
        if len(repository_parts) != 2 or any(not part.strip() for part in repository_parts):
            raise ValueError("source artifact repository must be owner/repository")
        for name in ("commit_sha", "blob_sha"):
            value = getattr(self, name)
            if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"source artifact {name} must be a lowercase 40-character SHA")
        if not self.path or self.path.startswith("/") or "\\" in self.path or any(
            part in {"", ".", ".."} for part in self.path.split("/")
        ):
            raise ValueError("source artifact path must be a relative non-traversing path")
        expected_prefix = f"https://github.com/{self.repository}/blob/{self.commit_sha}/"
        if not self.canonical_url.startswith(expected_prefix):
            raise ValueError("source artifact URL must be a canonical GitHub blob URL")
        if len(self.content_hash) != 71 or not self.content_hash.startswith("sha256:") or any(
            char not in "0123456789abcdef" for char in self.content_hash[7:]
        ):
            raise ValueError("source artifact content_hash must be a sha256 digest")

    def to_data(self) -> dict[str, str]:
        return {
            "source_kind": self.source_kind,
            "repository": self.repository,
            "commit_sha": self.commit_sha,
            "path": self.path,
            "blob_sha": self.blob_sha,
            "canonical_url": self.canonical_url,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> SourceArtifact:
        return cls(
            source_kind=str(data.get("source_kind", "")),
            repository=str(data.get("repository", "")),
            commit_sha=str(data.get("commit_sha", "")),
            path=str(data.get("path", "")),
            blob_sha=str(data.get("blob_sha", "")),
            canonical_url=str(data.get("canonical_url", "")),
            content_hash=str(data.get("content_hash", "")),
        )


@dataclass(frozen=True, slots=True)
class ToolFactDescriptor:
    """A normalized description of facts observable from a tool result.

    This is evidence metadata only: it cannot assert business completion or
    repair.  ``ToolResult.meta`` remains opaque compatibility metadata.
    """

    authority: str
    scope: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    observed_at: str | None = None
    content_hash: str | None = None
    source_artifacts: tuple[SourceArtifact, ...] = ()
    materialization: ToolResultMaterialization = ToolResultMaterialization.UNKNOWN
    capabilities: tuple[ToolFactCapability, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.authority, str) or not self.authority.strip():
            raise ValueError("tool fact authority must be a non-empty string")
        for name in ("scope", "artifact_refs"):
            values = getattr(self, name)
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"tool fact {name} must contain non-empty strings")
        if len(set(self.source_artifacts)) != len(self.source_artifacts):
            raise ValueError("tool fact source_artifacts must be unique")
        if not isinstance(self.materialization, ToolResultMaterialization):
            raise ValueError("tool fact materialization must be ToolResultMaterialization")
        if any(not isinstance(value, ToolFactCapability) for value in self.capabilities):
            raise ValueError("tool fact capabilities must contain ToolFactCapability values")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("tool fact capabilities must be unique")
        for name in ("observed_at", "content_hash"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"tool fact {name} must be a non-empty string when present")

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "authority": self.authority,
            "scope": list(self.scope),
            "artifact_refs": list(self.artifact_refs),
            "source_artifacts": [artifact.to_data() for artifact in self.source_artifacts],
            "materialization": self.materialization.value,
            "capabilities": [capability.value for capability in self.capabilities],
        }
        if self.observed_at is not None:
            data["observed_at"] = self.observed_at
        if self.content_hash is not None:
            data["content_hash"] = self.content_hash
        return data

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ToolFactDescriptor:
        authority = data.get("authority")
        if not isinstance(authority, str) or not authority.strip():
            raise ValueError("tool fact authority must be a non-empty string")
        def strings(name: str) -> tuple[str, ...]:
            values = data.get(name, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"tool fact {name} must be a list of non-empty strings")
            return tuple(values)
        def optional_string(name: str) -> str | None:
            value = data.get(name)
            if value is None:
                return None
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"tool fact {name} must be a non-empty string when present")
            return value
        raw_artifacts = data.get("source_artifacts", [])
        if not isinstance(raw_artifacts, list) or not all(
            isinstance(item, Mapping) for item in raw_artifacts
        ):
            raise ValueError("tool fact source_artifacts must be an object list")
        raw_capabilities = data.get("capabilities", [])
        if not isinstance(raw_capabilities, list):
            raise ValueError("tool fact capabilities must be a list")
        try:
            capabilities = tuple(ToolFactCapability(str(value)) for value in raw_capabilities)
            materialization = ToolResultMaterialization(
                str(data.get("materialization", ToolResultMaterialization.UNKNOWN.value))
            )
        except ValueError as error:
            raise ValueError("tool fact materialization or capabilities are invalid") from error
        return cls(
            authority=authority,
            scope=strings("scope"),
            artifact_refs=strings("artifact_refs"),
            source_artifacts=tuple(SourceArtifact.from_data(item) for item in raw_artifacts),
            observed_at=optional_string("observed_at"),
            content_hash=optional_string("content_hash"),
            materialization=materialization,
            capabilities=capabilities,
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
    fact_descriptor: ToolFactDescriptor | None = None

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
        if self.fact_descriptor is not None and not isinstance(
            self.fact_descriptor, ToolFactDescriptor
        ):
            raise ValueError("tool result fact_descriptor must be ToolFactDescriptor")

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
        if self.fact_descriptor is not None:
            result["fact_descriptor"] = self.fact_descriptor.to_data()
        return result

    def to_model_data(self) -> dict[str, Any]:
        """Project invocation facts for the model, not runtime retry policy.

        The durable representation remains unchanged for approval recovery,
        safety checks and older checkpoints.  Known explanatory details must
        not be lost just because an adapter stored them in recovery_action.
        """
        result = self.to_data()
        result.pop("retryable", None)
        result.pop("recovery_kind", None)
        action = result.pop("recovery_action", {})
        if isinstance(action, dict):
            details = {
                key: value for key, value in action.items()
                if key not in {"required_change", "same_call_safe", "action"}
            }
            if details:
                result["error_details"] = details
        return result

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ToolResult:
        meta = data.get("meta", {})
        if not isinstance(meta, Mapping):
            raise ValueError("tool result meta must be an object")
        recovery_action = data.get("recovery_action", {})
        if not isinstance(recovery_action, Mapping):
            raise ValueError("tool result recovery_action must be an object")
        raw_fact_descriptor = data.get("fact_descriptor")
        if raw_fact_descriptor is not None and not isinstance(raw_fact_descriptor, Mapping):
            raise ValueError("tool result fact_descriptor must be an object")
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
            fact_descriptor=(
                ToolFactDescriptor.from_data(raw_fact_descriptor)
                if isinstance(raw_fact_descriptor, Mapping) else None
            ),
        )


class ToolResultGovernorPort(Protocol):
    """Pure boundary that sanitizes and bounds provider-supplied tool results."""

    def govern(
        self, spec: ToolSpec, call: ToolCall, result: ToolResult
    ) -> ToolResult: ...


class ToolProcessControl(Protocol):
    """Task-scoped process operations exposed to an authorized tool call."""

    async def list_processes(self, scope: str, states: tuple[str, ...], limit: int, cursor: int) -> Mapping[str, Any]: ...

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str,
        environment: Mapping[str, str],
        max_output_bytes: int,
        max_lifetime_seconds: float,
        stop_on_task_end: bool,
        mode: str,
        timeout_seconds: float | None,
    ) -> BackgroundProcessStartResult | ProcessCommandResult: ...

    async def wait(
        self, process_id: str, wait_seconds: float
    ) -> ProcessWaitResult: ...

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
