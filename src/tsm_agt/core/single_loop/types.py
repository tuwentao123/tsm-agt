"""Stable data contracts for the single Agent loop migration.

These types deliberately have no dependency on Kernel, Session, or adapter
implementations.  They can therefore read legacy task-created events before the
new orchestration mode is wired into execution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from tsm_agt.ports.tool import ToolEffect


class OrchestrationMode(StrEnum):
    """Execution path fixed when a Task is created."""

    LEGACY = "LEGACY"
    SINGLE_LOOP = "SINGLE_LOOP"


class TaskBindingKind(StrEnum):
    """Durable reason an input was associated with a Task."""

    NEW = "NEW"
    EXPLICIT_REFERENCE = "EXPLICIT_REFERENCE"
    ACTIVE_CONTINUATION = "ACTIVE_CONTINUATION"
    EXPLICIT_CONTINUATION = "EXPLICIT_CONTINUATION"
    USER_SELECTED = "USER_SELECTED"
    USER_INTERACTION = "USER_INTERACTION"
    QUEUED_AFTER_APPROVAL = "QUEUED_AFTER_APPROVAL"
    CHOICE_REQUIRED = "CHOICE_REQUIRED"


class TurnIntentKind(StrEnum):
    """Work the single-loop model declares for the active turn."""

    DIRECT_RESPONSE = "DIRECT_RESPONSE"
    INVESTIGATE = "INVESTIGATE"
    MODIFY = "MODIFY"
    VERIFY = "VERIFY"
    INSPECT_RUNTIME = "INSPECT_RUNTIME"


class IntentSource(StrEnum):
    """Whether intent was declared by the model or inferred from a tool effect."""

    MODEL_DECLARED = "MODEL_DECLARED"
    RUNTIME_INFERRED = "RUNTIME_INFERRED"


class IntentDependencyKind(StrEnum):
    """A bounded, inspectable source of facts required by an intent."""

    WORKSPACE_SOURCE = "WORKSPACE_SOURCE"
    RUNTIME_TASK = "RUNTIME_TASK"
    RUNTIME_SESSION = "RUNTIME_SESSION"
    EXTERNAL = "EXTERNAL"


@dataclass(frozen=True, slots=True)
class IntentDependency:
    """One fact source a turn intends to inspect before making a claim."""

    kind: IntentDependencyKind
    reference: str

    def __post_init__(self) -> None:
        if not self.reference.strip() or len(self.reference) > 500:
            raise ValueError("intent dependency reference must contain 1..500 characters")

    def to_data(self) -> dict[str, str]:
        return {"kind": self.kind.value, "reference": self.reference}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> IntentDependency:
        return cls(
            IntentDependencyKind(str(data["kind"])),
            str(data["reference"]),
        )


@dataclass(frozen=True, slots=True)
class TaskOrchestrationMetadata:
    """Immutable orchestration identity projected from ``task.created``.

    Legacy tasks did not persist this object.  Their original ``task.created``
    goal remains the authoritative root goal and they always project as
    :attr:`OrchestrationMode.LEGACY`.
    """

    schema_version: int
    orchestration_mode: OrchestrationMode
    root_goal: str
    root_input_event_ref: str = ""
    latest_input_event_ref: str = ""
    binding_kind: TaskBindingKind = TaskBindingKind.NEW

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported task orchestration schema version")
        if not self.root_goal.strip() or len(self.root_goal) > 10_000:
            raise ValueError("task root goal must contain 1..10000 characters")
        if any(
            len(value) > 500
            for value in (self.root_input_event_ref, self.latest_input_event_ref)
        ):
            raise ValueError("task input references cannot exceed 500 characters")

    def to_data(self) -> dict[str, str | int]:
        return {
            "schema_version": self.schema_version,
            "orchestration_mode": self.orchestration_mode.value,
            "root_goal": self.root_goal,
            "root_input_event_ref": self.root_input_event_ref,
            "latest_input_event_ref": self.latest_input_event_ref,
            "binding_kind": self.binding_kind.value,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> TaskOrchestrationMetadata:
        required = {
            "schema_version", "orchestration_mode", "root_goal",
            "root_input_event_ref", "latest_input_event_ref", "binding_kind",
        }
        unknown = set(data) - required
        if unknown:
            raise ValueError(
                "unknown task orchestration fields: " + ", ".join(sorted(unknown))
            )
        missing = required - set(data)
        if missing:
            raise ValueError(
                "missing task orchestration fields: " + ", ".join(sorted(missing))
            )
        return cls(
            schema_version=int(data["schema_version"]),
            orchestration_mode=OrchestrationMode(str(data["orchestration_mode"])),
            root_goal=str(data["root_goal"]),
            root_input_event_ref=str(data["root_input_event_ref"]),
            latest_input_event_ref=str(data["latest_input_event_ref"]),
            binding_kind=TaskBindingKind(str(data["binding_kind"])),
        )

    @classmethod
    def from_task_created_payload(
        cls, payload: Mapping[str, Any], *, fallback_goal: str,
    ) -> TaskOrchestrationMetadata:
        """Read new metadata or project the durable legacy task goal."""
        raw = payload.get("single_loop")
        if raw is None:
            return cls(
                schema_version=1,
                orchestration_mode=OrchestrationMode.LEGACY,
                root_goal=fallback_goal,
            )
        if not isinstance(raw, Mapping):
            raise ValueError("task.created single_loop metadata must be an object")
        return cls.from_data(raw)


class IntentAttemptStatus(StrEnum):
    """Terminal outcome of one model request to describe turn intent."""

    ACCEPTED = "accepted"
    NORMALIZED = "normalized"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class IntentValidationErrorData:
    """Bounded, model-visible details for a rejected intent request."""

    field: str
    message: str
    received: str = ""
    expected: tuple[str, ...] = ()
    normalized_to: str | None = None

    def __post_init__(self) -> None:
        if not self.field.strip() or len(self.field) > 200:
            raise ValueError("intent validation field must contain 1..200 characters")
        if not self.message.strip() or len(self.message) > 500:
            raise ValueError("intent validation message must contain 1..500 characters")
        if len(self.received) > 500:
            raise ValueError("intent validation received value cannot exceed 500 characters")
        if len(self.expected) > 20 or any(
            not item.strip() or len(item) > 100 for item in self.expected
        ):
            raise ValueError("intent validation expected values are invalid")
        if self.normalized_to is not None and (
            not self.normalized_to.strip() or len(self.normalized_to) > 100
        ):
            raise ValueError("intent validation normalized value is invalid")

    def to_data(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "message": self.message,
            "received": self.received,
            "expected": list(self.expected),
            "normalized_to": self.normalized_to,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> IntentValidationErrorData:
        expected = data.get("expected", ())
        if not isinstance(expected, Sequence) or isinstance(expected, (str, bytes)):
            raise ValueError("intent validation expected must be a list")
        if any(not isinstance(item, str) for item in expected):
            raise ValueError("intent validation expected must contain strings")
        normalized_to = data.get("normalized_to")
        if normalized_to is not None and not isinstance(normalized_to, str):
            raise ValueError("intent validation normalized_to must be a string")
        return cls(
            field=str(data["field"]),
            message=str(data["message"]),
            received=str(data.get("received", "")),
            expected=tuple(expected),
            normalized_to=normalized_to,
        )


class IntentRequestValidationError(ValueError):
    """Parse failure that carries structured recovery data to the Runtime."""

    def __init__(self, detail: IntentValidationErrorData) -> None:
        super().__init__(detail.message)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ToolIntentRequest:
    """Model-provided high-level intent without model-authored effects.

    ``expected_effects`` is intentionally absent from the durable request. A
    legacy input may contain it, but Runtime only records that compatibility was
    used and derives effects from ``intent`` itself.
    """

    intent: TurnIntentKind | None = None
    dependencies: tuple[IntentDependency, ...] = ()
    summary: str = ""
    continuation_task_id: str | None = None
    legacy_input: bool = False
    normalized_effect_aliases: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if len(self.dependencies) > 20:
            raise ValueError("turn intent cannot have more than 20 dependencies")
        if len(self.summary) > 500:
            raise ValueError("turn intent summary cannot exceed 500 characters")
        if self.continuation_task_id is not None and (
            not self.continuation_task_id.strip()
            or len(self.continuation_task_id) > 500
        ):
            raise ValueError("continuation_task_id must contain 1..500 characters")
        if self.intent is TurnIntentKind.INSPECT_RUNTIME and not any(
            dependency.kind in {
                IntentDependencyKind.RUNTIME_TASK,
                IntentDependencyKind.RUNTIME_SESSION,
            }
            for dependency in self.dependencies
        ):
            raise ValueError(
                "runtime inspection intent requires a runtime dependency"
            )

    @staticmethod
    def implied_effects_for(
        intent: TurnIntentKind | None,
    ) -> tuple[ToolEffect, ...]:
        if intent is TurnIntentKind.INVESTIGATE:
            return (ToolEffect.OBSERVE,)
        if intent is TurnIntentKind.MODIFY:
            return (ToolEffect.MUTATE,)
        if intent is TurnIntentKind.VERIFY:
            return (ToolEffect.EXECUTE,)
        if intent is TurnIntentKind.INSPECT_RUNTIME:
            return (ToolEffect.OBSERVE,)
        return ()

    @property
    def implied_effects(self) -> tuple[ToolEffect, ...]:
        return self.implied_effects_for(self.intent)

    def to_legacy_turn_intent(self, turn_id: str) -> TurnIntent | None:
        """Derive the legacy reader record without trusting model effects."""
        if self.intent is None:
            return None
        return TurnIntent(
            schema_version=1,
            turn_id=turn_id,
            kind=self.intent,
            expected_effects=self.implied_effects,
            dependencies=self.dependencies,
            summary=self.summary,
            source=IntentSource.RUNTIME_INFERRED,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value if self.intent is not None else None,
            "dependencies": [item.to_data() for item in self.dependencies],
            "summary": self.summary,
            "continuation_task_id": self.continuation_task_id,
            "legacy_input": self.legacy_input,
            "normalized_effect_aliases": [
                {"received": received, "canonical": canonical}
                for received, canonical in self.normalized_effect_aliases
            ],
            "implied_effects": [effect.value for effect in self.implied_effects],
        }

    @classmethod
    def from_tool_arguments(cls, data: Mapping[str, Any]) -> ToolIntentRequest:
        allowed = {
            "intent",
            "dependencies",
            "summary",
            "continuation_task_id",
            "expected_effects",
        }
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise IntentRequestValidationError(IntentValidationErrorData(
                field=unknown[0],
                message="unknown turn intent field",
                received=unknown[0],
                expected=tuple(sorted(allowed - {"expected_effects"})),
            ))
        raw_intent = data.get("intent")
        if raw_intent is not None and not isinstance(raw_intent, str):
            raise IntentRequestValidationError(IntentValidationErrorData(
                field="intent",
                message="intent must be a string",
                received=type(raw_intent).__name__,
                expected=tuple(item.value for item in TurnIntentKind),
            ))
        try:
            intent = TurnIntentKind(raw_intent) if raw_intent is not None else None
        except ValueError as error:
            raise IntentRequestValidationError(IntentValidationErrorData(
                field="intent",
                message="unsupported turn intent",
                received=raw_intent,
                expected=tuple(item.value for item in TurnIntentKind),
            )) from error
        raw_dependencies = data.get("dependencies", ())
        if not isinstance(raw_dependencies, Sequence) or isinstance(
            raw_dependencies,
            (str, bytes),
        ):
            raise IntentRequestValidationError(IntentValidationErrorData(
                field="dependencies",
                message="dependencies must be an array",
                received=type(raw_dependencies).__name__,
            ))
        dependencies: list[IntentDependency] = []
        for index, dependency in enumerate(raw_dependencies):
            if not isinstance(dependency, Mapping):
                raise IntentRequestValidationError(IntentValidationErrorData(
                    field=f"dependencies[{index}]",
                    message="dependency must be an object",
                    received=type(dependency).__name__,
                ))
            if set(dependency) != {"kind", "reference"}:
                raise IntentRequestValidationError(IntentValidationErrorData(
                    field=f"dependencies[{index}]",
                    message="dependency requires only kind and reference",
                ))
            try:
                dependencies.append(IntentDependency.from_data(dependency))
            except (KeyError, TypeError, ValueError) as error:
                raise IntentRequestValidationError(IntentValidationErrorData(
                    field=f"dependencies[{index}]",
                    message=str(error),
                )) from error
        summary = data.get("summary", "")
        if not isinstance(summary, str):
            raise IntentRequestValidationError(IntentValidationErrorData(
                field="summary",
                message="summary must be a string",
                received=type(summary).__name__,
            ))
        continuation_task_id = data.get("continuation_task_id")
        if continuation_task_id is not None and not isinstance(
            continuation_task_id,
            str,
        ):
            raise IntentRequestValidationError(IntentValidationErrorData(
                field="continuation_task_id",
                message="continuation_task_id must be a string",
                received=type(continuation_task_id).__name__,
            ))
        aliases: list[tuple[str, str]] = []
        legacy_input = "expected_effects" in data
        raw_effects = data.get("expected_effects", ())
        if not isinstance(raw_effects, Sequence) or isinstance(raw_effects, (str, bytes)):
            raise IntentRequestValidationError(IntentValidationErrorData(
                field="expected_effects",
                message="expected_effects must be an array",
                received=type(raw_effects).__name__,
            ))
        valid_effects = {
            effect.value
            for effect in ToolEffect
            if effect is not ToolEffect.UNSPECIFIED
        }
        for index, raw_effect in enumerate(raw_effects):
            if not isinstance(raw_effect, str):
                raise IntentRequestValidationError(IntentValidationErrorData(
                    field=f"expected_effects[{index}]",
                    message="expected_effects must contain strings",
                    received=type(raw_effect).__name__,
                    expected=tuple(sorted(valid_effects | {"verify"})),
                ))
            normalized = raw_effect.strip().casefold()
            if normalized == "verify":
                aliases.append((raw_effect, ToolEffect.EXECUTE.value))
            elif normalized not in valid_effects:
                raise IntentRequestValidationError(IntentValidationErrorData(
                    field=f"expected_effects[{index}]",
                    message="unsupported legacy effect",
                    received=raw_effect,
                    expected=tuple(sorted(valid_effects | {"verify"})),
                ))
        try:
            return cls(
                intent=intent,
                dependencies=tuple(dependencies),
                summary=summary,
                continuation_task_id=continuation_task_id,
                legacy_input=legacy_input,
                normalized_effect_aliases=tuple(aliases),
            )
        except ValueError as error:
            raise IntentRequestValidationError(IntentValidationErrorData(
                field="intent",
                message=str(error),
                received=raw_intent if isinstance(raw_intent, str) else "",
            )) from error

    @staticmethod
    def requested_intent(data: Mapping[str, Any]) -> str:
        value = data.get("intent")
        return value if isinstance(value, str) else ""

    @staticmethod
    def known_intent(data: Mapping[str, Any]) -> TurnIntentKind | None:
        value = data.get("intent")
        try:
            return TurnIntentKind(value) if isinstance(value, str) else None
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class IntentAttempt:
    """Durable, non-completion-authoritative result of one intent call."""

    schema_version: int
    attempt_id: str
    turn_id: str
    tool_call_id: str
    requested_intent: str = ""
    normalized_intent: TurnIntentKind | None = None
    status: IntentAttemptStatus = IntentAttemptStatus.ACCEPTED
    implied_effects: tuple[ToolEffect, ...] = ()
    continuation_task_id: str | None = None
    legacy_input: bool = False
    normalized_effect_aliases: tuple[tuple[str, str], ...] = ()
    validation_error: IntentValidationErrorData | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported intent attempt schema version")
        if not self.attempt_id.strip() or not self.turn_id.strip() or not self.tool_call_id.strip():
            raise ValueError("intent attempt requires attempt, turn, and call IDs")
        if len(self.requested_intent) > 100:
            raise ValueError("intent attempt requested intent cannot exceed 100 characters")
        if len(self.implied_effects) > 6 or ToolEffect.UNSPECIFIED in self.implied_effects:
            raise ValueError("intent attempt implied effects are invalid")
        if self.status is IntentAttemptStatus.REJECTED:
            if self.validation_error is None:
                raise ValueError("rejected intent attempt requires validation_error")
        elif self.validation_error is not None:
            raise ValueError("accepted intent attempt cannot contain validation_error")

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "turn_id": self.turn_id,
            "tool_call_id": self.tool_call_id,
            "requested_intent": self.requested_intent,
            "normalized_intent": (
                self.normalized_intent.value
                if self.normalized_intent is not None else None
            ),
            "status": self.status.value,
            "implied_effects": [effect.value for effect in self.implied_effects],
            "continuation_task_id": self.continuation_task_id,
            "legacy_input": self.legacy_input,
            "normalized_effect_aliases": [
                {"received": received, "canonical": canonical}
                for received, canonical in self.normalized_effect_aliases
            ],
            "validation_error": (
                self.validation_error.to_data()
                if self.validation_error is not None else None
            ),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> IntentAttempt:
        implied_effects = data.get("implied_effects", ())
        aliases = data.get("normalized_effect_aliases", ())
        validation_error = data.get("validation_error")
        if not isinstance(implied_effects, Sequence) or isinstance(
            implied_effects,
            (str, bytes),
        ) or any(not isinstance(effect, str) for effect in implied_effects):
            raise ValueError("intent attempt implied_effects must be a list of strings")
        if not isinstance(aliases, Sequence) or isinstance(aliases, (str, bytes)):
            raise ValueError("intent attempt normalized_effect_aliases must be a list")
        normalized_aliases: list[tuple[str, str]] = []
        for alias in aliases:
            if not isinstance(alias, Mapping):
                raise ValueError("intent attempt normalization must be an object")
            received = alias.get("received")
            canonical = alias.get("canonical")
            if not isinstance(received, str) or not isinstance(canonical, str):
                raise ValueError("intent attempt normalization values must be strings")
            normalized_aliases.append((received, canonical))
        if validation_error is not None and not isinstance(validation_error, Mapping):
            raise ValueError("intent attempt validation_error must be an object")
        normalized_intent = data.get("normalized_intent")
        if normalized_intent is not None and not isinstance(normalized_intent, str):
            raise ValueError("intent attempt normalized_intent must be a string")
        continuation_task_id = data.get("continuation_task_id")
        if continuation_task_id is not None and not isinstance(continuation_task_id, str):
            raise ValueError("intent attempt continuation_task_id must be a string")
        return cls(
            schema_version=int(data["schema_version"]),
            attempt_id=str(data["attempt_id"]),
            turn_id=str(data["turn_id"]),
            tool_call_id=str(data["tool_call_id"]),
            requested_intent=str(data.get("requested_intent", "")),
            normalized_intent=(
                TurnIntentKind(normalized_intent)
                if normalized_intent is not None else None
            ),
            status=IntentAttemptStatus(str(data["status"])),
            implied_effects=tuple(ToolEffect(effect) for effect in implied_effects),
            continuation_task_id=continuation_task_id,
            legacy_input=bool(data.get("legacy_input", False)),
            normalized_effect_aliases=tuple(normalized_aliases),
            validation_error=(
                IntentValidationErrorData.from_data(validation_error)
                if isinstance(validation_error, Mapping) else None
            ),
        )


@dataclass(frozen=True, slots=True)
class TurnIntent:
    """Bounded intent state declared or inferred inside one Agent turn."""

    schema_version: int
    turn_id: str
    kind: TurnIntentKind
    expected_effects: tuple[ToolEffect, ...] = ()
    dependencies: tuple[IntentDependency, ...] = ()
    summary: str = ""
    source: IntentSource = IntentSource.MODEL_DECLARED

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported turn intent schema version")
        if not self.turn_id.strip() or len(self.turn_id) > 200:
            raise ValueError("turn intent requires a bounded turn_id")
        if len(self.summary) > 500:
            raise ValueError("turn intent summary cannot exceed 500 characters")
        if len(self.dependencies) > 20:
            raise ValueError("turn intent cannot have more than 20 dependencies")
        if len(set(self.expected_effects)) != len(self.expected_effects):
            raise ValueError("turn intent effects must be unique")
        if ToolEffect.UNSPECIFIED in self.expected_effects:
            raise ValueError("turn intent cannot declare an unspecified effect")
        effects = frozenset(self.expected_effects)
        if self.kind is TurnIntentKind.DIRECT_RESPONSE and effects:
            raise ValueError("direct response intent cannot declare effects")
        if self.kind is TurnIntentKind.MODIFY and ToolEffect.MUTATE not in effects:
            raise ValueError("modify intent must declare mutate")
        if self.kind is TurnIntentKind.VERIFY and ToolEffect.EXECUTE not in effects:
            raise ValueError("verify intent must declare execute")
        if self.kind is TurnIntentKind.INSPECT_RUNTIME and (
            ToolEffect.OBSERVE not in effects
            or not any(
                dependency.kind in {
                    IntentDependencyKind.RUNTIME_TASK,
                    IntentDependencyKind.RUNTIME_SESSION,
                }
                for dependency in self.dependencies
            )
        ):
            raise ValueError(
                "runtime inspection intent requires observe and a runtime dependency"
            )

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "turn_id": self.turn_id,
            "intent": self.kind.value,
            "expected_effects": [effect.value for effect in self.expected_effects],
            "dependencies": [item.to_data() for item in self.dependencies],
            "summary": self.summary,
            "source": self.source.value,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> TurnIntent:
        dependencies = data.get("dependencies", [])
        effects = data.get("expected_effects", [])
        if not isinstance(dependencies, Sequence) or isinstance(dependencies, str):
            raise ValueError("turn intent dependencies must be a list")
        if not isinstance(effects, Sequence) or isinstance(effects, str):
            raise ValueError("turn intent expected_effects must be a list")
        if any(not isinstance(item, Mapping) for item in dependencies):
            raise ValueError("turn intent dependencies must contain objects")
        if any(not isinstance(item, str) for item in effects):
            raise ValueError("turn intent expected_effects must contain strings")
        return cls(
            schema_version=int(data["schema_version"]),
            turn_id=str(data["turn_id"]),
            kind=TurnIntentKind(str(data["intent"])),
            expected_effects=tuple(ToolEffect(str(item)) for item in effects),
            dependencies=tuple(
                IntentDependency.from_data(item)
                for item in dependencies if isinstance(item, Mapping)
            ),
            summary=str(data.get("summary", "")),
            source=IntentSource(str(data.get("source", IntentSource.MODEL_DECLARED))),
        )


@dataclass(frozen=True, slots=True)
class CompletionClaim:
    """Runtime-owned finalization input, later checked against durable facts."""

    schema_version: int
    turn_id: str
    assistant_message_id: str
    evidence_refs: tuple[str, ...] = ()
    claimed_effects: tuple[ToolEffect, ...] = ()
    remaining_work: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported completion claim schema version")
        if not self.turn_id.strip() or not self.assistant_message_id.strip():
            raise ValueError("completion claim requires turn and assistant message IDs")
        if len(self.evidence_refs) > 50 or len(self.remaining_work) > 20:
            raise ValueError("completion claim collection exceeds its bound")
        if len(set(self.claimed_effects)) != len(self.claimed_effects):
            raise ValueError("completion claim effects must be unique")
        if ToolEffect.UNSPECIFIED in self.claimed_effects:
            raise ValueError("completion claim cannot declare an unspecified effect")

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "turn_id": self.turn_id,
            "assistant_message_id": self.assistant_message_id,
            "evidence_refs": list(self.evidence_refs),
            "claimed_effects": [effect.value for effect in self.claimed_effects],
            "remaining_work": list(self.remaining_work),
        }


@dataclass(frozen=True, slots=True)
class TaskBindingDecision:
    """Deterministic association between one input event and one Task."""

    schema_version: int
    task_id: str | None
    binding_kind: TaskBindingKind
    current_input_event_ref: str
    source_task_id: str | None = None
    requires_runtime_inspection: bool = False
    continuation_requested: bool = False
    candidate_task_ids: tuple[str, ...] = ()
    reason_code: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported task binding schema version")
        if (
            self.binding_kind not in {
                TaskBindingKind.CHOICE_REQUIRED,
                TaskBindingKind.NEW,
            }
            and not self.task_id
        ):
            raise ValueError(
                "task binding requires task_id unless it creates new work or requires choice"
            )
        if not self.current_input_event_ref.strip():
            raise ValueError("task binding requires current input event reference")

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "binding_kind": self.binding_kind.value,
            "current_input_event_ref": self.current_input_event_ref,
            "source_task_id": self.source_task_id,
            "requires_runtime_inspection": self.requires_runtime_inspection,
            "continuation_requested": self.continuation_requested,
            "candidate_task_ids": list(self.candidate_task_ids),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ActiveContextProjection:
    """Bounded model-visible context with explicit authority separation."""

    schema_version: int
    task: Mapping[str, Any]
    turn: Mapping[str, Any]
    authoritative: Mapping[str, Any]
    non_authoritative: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported active context schema version")
        if not str(self.task.get("task_id", "")).strip():
            raise ValueError("active context requires task.task_id")
        if not str(self.task.get("root_goal", "")).strip():
            raise ValueError("active context requires task.root_goal")

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task": dict(self.task),
            "turn": dict(self.turn),
            "authoritative": dict(self.authoritative),
            "non_authoritative": dict(self.non_authoritative),
            "authority": {
                "task": "AUTHORITATIVE",
                "turn": "AUTHORITATIVE",
                "authoritative": "AUTHORITATIVE",
                "non_authoritative": "NON_AUTHORITATIVE",
            },
        }
