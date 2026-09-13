"""Deterministic, user-visible Session conversation projection.

The projection is rebuilt from Session events.  It deliberately carries only
user-visible text and explicit structured state; approvals, tool protocol,
process handles, credentials, and hidden reasoning never become conversation
history.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tsm_agt.ports import Message, MessageRole, SessionEvent, TextBlock

from .configuration import canonical_hash
from .execution import ToolExecutionRecord
from .session import SessionSnapshot
from .session_resources import (
    SessionQuestionReference, SessionResourceKind, SessionResourceReference,
)
from .runtime_input import SessionResumeCandidate


@dataclass(frozen=True, slots=True)
class SessionConversationMessage:
    message_id: str
    role: MessageRole
    text: str
    task_id: str
    turn_id: str
    source_event_sequence: int

    def __post_init__(self) -> None:
        if self.role not in (MessageRole.USER, MessageRole.ASSISTANT):
            raise ValueError("Session conversation only accepts user-visible roles")
        if not all((self.message_id, self.text, self.task_id, self.turn_id)):
            raise ValueError("Session conversation message fields must not be empty")
        if self.source_event_sequence < 1:
            raise ValueError("source event sequence must be positive")

    def source_data(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role.value,
            "text": self.text,
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "source_event_sequence": self.source_event_sequence,
        }


@dataclass(frozen=True, slots=True)
class SessionWorkingState:
    """Explicit Session state slots; A5 will add their active maintenance."""

    goal: str | None = None
    constraints: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    completed_work: tuple[str, ...] = ()
    remaining_work: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "constraints": list(self.constraints),
            "decisions": list(self.decisions),
            "open_questions": list(self.open_questions),
            "completed_work": list(self.completed_work),
            "remaining_work": list(self.remaining_work),
        }


@dataclass(frozen=True, slots=True)
class SessionTaskSummary:
    """Bounded, authority-free handoff for one completed visible Turn.

    This is the missing bridge between conversation text and the durable Task
    ledger.  It keeps only inspectable execution facts needed by the next Turn;
    raw Tool result bodies, approvals, credentials, process ownership and hidden
    reasoning remain in their authoritative stores.
    """

    task_id: str
    turn_id: str
    goal: str
    recorded_task_state: str
    tool_counts: tuple[tuple[str, int], ...] = ()
    important_actions: tuple[Mapping[str, Any], ...] = ()
    confirmed: tuple[Mapping[str, Any], ...] = ()
    completed_work: tuple[str, ...] = ()
    remaining_work: tuple[str, ...] = ()
    workspace_roots: tuple[str, ...] = ()
    mutations: tuple[Mapping[str, Any], ...] = ()
    verification_status: str | None = None
    task_spec_revision: int = 0
    continuation_mode: str = "NONE"
    outcomes: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not all((self.task_id, self.turn_id, self.goal, self.recorded_task_state)):
            raise ValueError("session Task summary identity fields are required")
        if (
            len(self.important_actions) > 20
            or len(self.confirmed) > 20
            or len(self.mutations) > 20
            or len(self.outcomes) > 30
        ):
            raise ValueError("session Task summary exceeds its bounded limit")
        if self.task_spec_revision < 0:
            raise ValueError("session Task summary SPEC revision is invalid")

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "goal": self.goal,
            "recorded_task_state": self.recorded_task_state,
            "tool_counts": {name: count for name, count in self.tool_counts},
            "important_actions": [dict(item) for item in self.important_actions],
            "confirmed": [dict(item) for item in self.confirmed],
            "completed_work": list(self.completed_work),
            "remaining_work": list(self.remaining_work),
            "workspace_roots": list(self.workspace_roots),
            "mutations": [dict(item) for item in self.mutations],
            "verification_status": self.verification_status,
            "task_spec_revision": self.task_spec_revision,
            "continuation_mode": self.continuation_mode,
            "outcomes": [dict(item) for item in self.outcomes],
        }

    def prompt_data(
        self, execution_events: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Render one non-redundant model view of this Task handoff."""
        data = self.to_data()
        if execution_events:
            # Paired recent execution facts supersede the older coarse action
            # list. Keep important_actions only as a durable compatibility
            # fallback for old databases or unavailable Task ledgers.
            data.pop("important_actions", None)
            data["execution_events"] = [dict(item) for item in execution_events]
        return data

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> SessionTaskSummary:
        raw_counts = data.get("tool_counts", {})
        raw_actions = data.get("important_actions", [])
        raw_confirmed = data.get("confirmed", [])
        raw_completed = data.get("completed_work", [])
        raw_remaining = data.get("remaining_work", [])
        raw_roots = data.get("workspace_roots", [])
        raw_mutations = data.get("mutations", [])
        raw_outcomes = data.get("outcomes", [])
        if not isinstance(raw_counts, Mapping):
            raise ValueError("session Task summary tool counts must be an object")
        if not all(isinstance(item, list) for item in (
            raw_actions, raw_confirmed, raw_completed, raw_remaining, raw_roots,
            raw_mutations, raw_outcomes,
        )):
            raise ValueError("session Task summary collections must be lists")
        return cls(
            task_id=str(data["task_id"]), turn_id=str(data["turn_id"]),
            goal=str(data["goal"]),
            recorded_task_state=str(data["recorded_task_state"]),
            tool_counts=tuple(sorted(
                (str(name), max(0, int(count)))
                for name, count in raw_counts.items()
            )),
            important_actions=tuple(
                dict(item) for item in raw_actions if isinstance(item, Mapping)
            )[:20],
            confirmed=tuple(
                dict(item) for item in raw_confirmed if isinstance(item, Mapping)
            )[:20],
            completed_work=tuple(
                str(item) for item in raw_completed if str(item)
            ),
            remaining_work=tuple(str(item) for item in raw_remaining if str(item)),
            workspace_roots=tuple(str(item) for item in raw_roots if str(item)),
            mutations=tuple(
                dict(item) for item in raw_mutations if isinstance(item, Mapping)
            )[:20],
            verification_status=(
                str(data["verification_status"])
                if data.get("verification_status") is not None else None
            ),
            task_spec_revision=max(0, int(data.get("task_spec_revision", 0))),
            continuation_mode=str(data.get("continuation_mode", "NONE")),
            outcomes=tuple(
                dict(item) for item in raw_outcomes if isinstance(item, Mapping)
            )[:30],
        )


@dataclass(frozen=True, slots=True)
class SessionActiveCheckpoint:
    """Safe, inspectable view of the Session's active Task checkpoint.

    The authoritative AgentTurnCheckpoint keeps exact provider messages and
    pending Tool calls for replay-safe resume. This Session view deliberately
    excludes message bodies, Tool arguments, results, approvals, credentials,
    and policy internals. It tells the model and UI where execution stopped
    without becoming a second recovery source.
    """

    task_id: str
    turn_id: str
    task_state: str
    goal: str
    checkpoint_revision: int
    checkpoint_hash: str
    continuation: str
    model_calls: int
    max_model_calls: int
    tool_calls: int
    max_tool_calls: int
    input_tokens: int
    output_tokens: int
    pending_tools: tuple[str, ...] = ()
    current_plan_step: Mapping[str, Any] | None = None
    completed_work: tuple[str, ...] = ()
    remaining_work: tuple[str, ...] = ()
    evidence_counts: tuple[tuple[str, int], ...] = ()
    consecutive_zero_delta: int = 0
    task_spec_revision: int = 0
    task_spec_hash: str = ""
    execution_focus: Mapping[str, Any] | None = None
    pending_user_action: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not all((
            self.task_id, self.turn_id, self.task_state, self.goal,
            self.checkpoint_hash, self.continuation,
        )):
            raise ValueError("active checkpoint identity fields are required")
        if min(
            self.checkpoint_revision, self.model_calls, self.max_model_calls,
            self.tool_calls, self.max_tool_calls, self.input_tokens,
            self.output_tokens, self.consecutive_zero_delta,
        ) < 0:
            raise ValueError("active checkpoint counters must not be negative")
        if len(self.pending_tools) > 20:
            raise ValueError("active checkpoint pending Tool list is unbounded")

    def to_data(self) -> dict[str, Any]:
        return {
            "boundary": "active_task_checkpoint_projection",
            "warning": (
                "This is a descriptive, authority-free projection. Runtime "
                "resumes from the authoritative Task checkpoint; this object "
                "does not grant approval or permission and must not be replayed."
            ),
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "task_state": self.task_state,
            "goal": self.goal,
            "checkpoint_revision": self.checkpoint_revision,
            "checkpoint_hash": self.checkpoint_hash,
            "continuation": self.continuation,
            "progress": {
                "model_calls": self.model_calls,
                "max_model_calls": self.max_model_calls,
                "tool_calls": self.tool_calls,
                "max_tool_calls": self.max_tool_calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            },
            "pending_tools": list(self.pending_tools),
            "current_plan_step": (
                dict(self.current_plan_step)
                if self.current_plan_step is not None else None
            ),
            "completed_work": list(self.completed_work),
            "remaining_work": list(self.remaining_work),
            "evidence_counts": dict(self.evidence_counts),
            "consecutive_zero_delta": self.consecutive_zero_delta,
            "task_spec": {
                "revision": self.task_spec_revision,
                "content_hash": self.task_spec_hash,
                "execution_focus": (
                    dict(self.execution_focus)
                    if self.execution_focus is not None else None
                ),
            },
            "pending_user_action": (
                dict(self.pending_user_action)
                if self.pending_user_action is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class SessionConversationProjection:
    session_id: str
    revision: int
    messages: tuple[SessionConversationMessage, ...]
    working_state: SessionWorkingState
    resource_catalog: tuple[SessionResourceReference, ...]
    question_catalog: tuple[SessionQuestionReference, ...]
    task_summaries: tuple[SessionTaskSummary, ...]
    source_event_sequences: tuple[int, ...]
    content_hash: str

    def __post_init__(self) -> None:
        if not self.session_id or self.revision < 1:
            raise ValueError("Session projection identity and revision are required")
        if tuple(sorted(set(self.source_event_sequences))) != self.source_event_sequences:
            raise ValueError("Session projection sources must be sorted and unique")
        if self.content_hash != canonical_hash(self.hash_source()):
            raise ValueError("Session projection content hash does not match")

    def hash_source(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "session_id": self.session_id,
            "revision": self.revision,
            "messages": [message.source_data() for message in self.messages],
            "working_state": self.working_state.to_data(),
            "resource_catalog": [
                item.to_data() for item in self.resource_catalog
            ],
            "question_catalog": [
                item.to_data() for item in self.question_catalog
            ],
            "task_summaries": [item.to_data() for item in self.task_summaries],
            "source_event_sequences": list(self.source_event_sequences),
        }

    def to_data(self) -> dict[str, Any]:
        return {**self.hash_source(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class SessionPromptProjection:
    message: Message | None
    revision: int
    content_hash: str
    source_event_sequences: tuple[int, ...]
    recent_message_count: int
    summarized_message_count: int
    summary_revision: int
    summary_hash: str
    summary_source_event_sequences: tuple[int, ...]
    summary_source_event_ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class SessionContextProjector:
    recent_execution_limit: int = 12
    recent_execution_per_task_limit: int = 6
    execution_result_item_limit: int = 10
    small_read_content_characters: int = 2000

    def __post_init__(self) -> None:
        if min(
            self.recent_execution_limit, self.recent_execution_per_task_limit,
            self.execution_result_item_limit, self.small_read_content_characters,
        ) < 1:
            raise ValueError("Session context projection limits must be positive")

    def project(
        self, snapshot: SessionSnapshot, events: Sequence[SessionEvent],
    ) -> SessionConversationProjection:
        messages: list[SessionConversationMessage] = []
        seen_message_ids: set[str] = set()
        sources: set[int] = set()
        explicit_state: dict[str, Any] = {}
        resources: dict[str, SessionResourceReference] = {}
        questions: dict[str, SessionQuestionReference] = {}
        task_summaries: dict[str, SessionTaskSummary] = {}

        for event in sorted(events, key=lambda item: item.sequence):
            if event.session_id != snapshot.session_id:
                raise ValueError("Session event belongs to a different Session")
            if event.event_type == "session.task_result_recorded":
                task_id = str(event.payload.get("task_id") or "")
                turn_id = str(event.payload.get("turn_id") or "")
                for key, role in (("user_message", MessageRole.USER),
                                  ("assistant_message", MessageRole.ASSISTANT)):
                    projected = self._visible_message(
                        event.payload.get(key), role, task_id, turn_id, event.sequence
                    )
                    if projected is None or projected.message_id in seen_message_ids:
                        continue
                    seen_message_ids.add(projected.message_id)
                    messages.append(projected)
                    sources.add(event.sequence)
                self._apply_working_state(explicit_state, event.payload)
                self._apply_catalogs(resources, questions, event.payload)
                raw_summary = event.payload.get("task_summary")
                if isinstance(raw_summary, Mapping):
                    summary = SessionTaskSummary.from_data(raw_summary)
                else:
                    # Older runtime.db files predate task_summary. Rebuild the
                    # smallest useful handoff from fields those events already
                    # persisted so upgrading the CLI does not erase continuity.
                    summary = self._legacy_task_summary(
                        event.payload, task_id, turn_id, messages, resources
                    )
                if summary is not None:
                    task_summaries.pop(summary.task_id, None)
                    task_summaries[summary.task_id] = summary
            elif event.event_type == "session.context_state_updated":
                self._apply_explicit_state(explicit_state, event.payload)
                sources.add(event.sequence)
            elif event.event_type == "session.task_state_updated":
                task_id = str(event.payload.get("task_id") or "")
                state = str(event.payload.get("task_state") or "")
                prior = task_summaries.get(task_id)
                if prior is not None and state:
                    task_summaries[task_id] = SessionTaskSummary(
                        task_id=prior.task_id, turn_id=prior.turn_id,
                        goal=prior.goal, recorded_task_state=state,
                        tool_counts=prior.tool_counts,
                        important_actions=prior.important_actions,
                        confirmed=prior.confirmed,
                        completed_work=prior.completed_work,
                        remaining_work=prior.remaining_work,
                        workspace_roots=prior.workspace_roots,
                        mutations=prior.mutations,
                        verification_status=(
                            str(event.payload["verification_status"])
                            if event.payload.get("verification_status") is not None
                            else prior.verification_status
                        ),
                        task_spec_revision=prior.task_spec_revision,
                        continuation_mode=prior.continuation_mode,
                        outcomes=prior.outcomes,
                    )
                    sources.add(event.sequence)

        latest_goal = next(
            (message.text for message in reversed(messages)
             if message.role is MessageRole.USER),
            None,
        )
        state = SessionWorkingState(
            goal=self._optional_text(explicit_state.get("goal")) or latest_goal,
            constraints=self._text_tuple(explicit_state.get("constraints")),
            decisions=self._text_tuple(explicit_state.get("decisions")),
            open_questions=self._text_tuple(explicit_state.get("open_questions")),
            completed_work=self._text_tuple(explicit_state.get("completed_work")),
            remaining_work=self._text_tuple(explicit_state.get("remaining_work")),
        )
        source_sequences = tuple(sorted(sources))
        hash_source = {
            "schema_version": 1, "session_id": snapshot.session_id,
            "revision": snapshot.context_revision,
            "messages": [message.source_data() for message in messages],
            "working_state": state.to_data(),
            "resource_catalog": [
                item.to_data() for item in resources.values()
            ],
            "question_catalog": [
                item.to_data() for item in questions.values()
            ],
            "task_summaries": [
                item.to_data() for item in task_summaries.values()
            ],
            "source_event_sequences": list(source_sequences),
        }
        return SessionConversationProjection(
            snapshot.session_id, snapshot.context_revision, tuple(messages), state,
            tuple(resources.values()), tuple(questions.values()),
            tuple(task_summaries.values()),
            source_sequences, canonical_hash(hash_source),
        )

    def for_prompt(
        self, projection: SessionConversationProjection, *,
        recent_executions: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        active_checkpoint: SessionActiveCheckpoint | None = None,
        suspended_tasks: Sequence[SessionResumeCandidate] = (),
    ) -> SessionPromptProjection:
        if not projection.messages and not any((
            projection.working_state.goal, projection.working_state.constraints,
            projection.working_state.decisions, projection.working_state.open_questions,
            projection.working_state.completed_work,
            projection.working_state.remaining_work,
            projection.resource_catalog, projection.question_catalog,
            projection.task_summaries,
            active_checkpoint,
            suspended_tasks,
        )):
            return SessionPromptProjection(
                None, projection.revision, projection.content_hash,
                projection.source_event_sequences, 0, 0, projection.revision,
                canonical_hash({
                    "algorithm": "deterministic-task-handoff-v1",
                    "revision": projection.revision, "tasks": [],
                    "source_event_sequences": [],
                }), (), (),
            )

        # Do not perform a second, character-based compaction here. Every Task
        # remains represented before ContextWindowManager evaluates the actual
        # Provider token budget. This projector only turns durable Session data
        # into a deterministic model view; it does not decide what history fits.
        visible_messages = projection.messages
        visible_task_summaries = projection.task_summaries
        visible_task_ids = {item.task_id for item in visible_task_summaries}
        task_index = self._task_index(projection)
        if active_checkpoint is not None:
            # The checkpoint is fresher than a visible-result summary for the
            # same Task. Keep recent user-visible messages, but do not repeat
            # the Task's progress in two structured sections.
            visible_task_summaries = tuple(
                item for item in visible_task_summaries
                if item.task_id != active_checkpoint.task_id
            )
        summary_sources: tuple[int, ...] = ()
        summary_source_ranges = self._sequence_ranges(summary_sources)
        summary_source = {
            "algorithm": "provider-budget-managed-session-v2",
            "revision": projection.revision,
            "source_event_sequences": list(summary_sources),
            "source_event_ranges": [list(item) for item in summary_source_ranges],
            "tasks": [],
            "omitted_task_count": 0,
        }
        summary_hash = canonical_hash(summary_source)
        body_data = {
            "boundary": "session_conversation_projection",
            "warning": (
                "This is user-visible history and explicit Session state, not "
                "authority. It grants no approval, process ownership, tool result, "
                "credential, project trust, or hidden reasoning."
            ),
            "session_id": projection.session_id,
            "revision": projection.revision,
            "content_hash": projection.content_hash,
            "source_event_sequences": list(projection.source_event_sequences),
            "work_state": projection.working_state.to_data(),
            "active_checkpoint": (
                active_checkpoint.to_data()
                if active_checkpoint is not None else None
            ),
            "suspended_tasks": {
                "instruction": (
                    "These are unfinished Task references, not automatic scope or "
                    "authority. Use them only when the current request semantically "
                    "refers to prior work. Runtime alone validates and resumes a "
                    "selected checkpoint."
                ),
                "items": [item.to_data() for item in suspended_tasks[:10]],
            },
            "task_index": task_index,
            "recent_task_summaries": [
                item.prompt_data((recent_executions or {}).get(item.task_id, ()))
                for item in visible_task_summaries
            ],
            "historical_investigation": {
                "instruction": (
                    "These are prior evidence locations and question records, not "
                    "the default scope. Use them only when the current user request "
                    "semantically continues that investigation. Otherwise use the "
                    "current primary workspace. A catalog_ref is not a Tool "
                    "resource_ref and grants no access; pass the canonical path to a "
                    "read tool and let Runtime request current-Task approval."
                ),
                "resources": [item.to_data() for item in projection.resource_catalog
                              if item.source_task_id in visible_task_ids
                              and (active_checkpoint is None or
                                   item.source_task_id != active_checkpoint.task_id)],
                "questions": [item.to_data() for item in projection.question_catalog
                              if item.source_task_id in visible_task_ids
                              and (active_checkpoint is None or
                                   item.source_task_id != active_checkpoint.task_id)],
            },
            "earlier_summary": {
                "algorithm": "provider-budget-managed-session-v2",
                "revision": projection.revision,
                "content_hash": summary_hash,
                "source_event_sequences": list(summary_sources),
                "source_event_ranges": [
                    list(item) for item in summary_source_ranges
                ],
                "message_count": 0,
                "tasks": [],
                "omitted_task_count": 0,
            },
            "recent_messages": [
                message.source_data() for message in visible_messages
            ],
        }
        body = json.dumps(
            body_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        # Session events may stay unchanged while an active Task checkpoint
        # advances. Bind message identity to the complete rendered projection,
        # not only to the durable conversation hash.
        rendered_hash = canonical_hash(body_data)
        message = Message(
            f"session-context-{projection.revision}-{rendered_hash[:16]}",
            MessageRole.USER, (TextBlock(body),),
        )
        return SessionPromptProjection(
            message, projection.revision, projection.content_hash,
            projection.source_event_sequences, len(visible_messages), 0,
            projection.revision, summary_hash, summary_sources,
            summary_source_ranges,
        )

    @staticmethod
    def _sequence_ranges(
        sequences: tuple[int, ...],
    ) -> tuple[tuple[int, int], ...]:
        if not sequences:
            return ()
        ranges: list[tuple[int, int]] = []
        start = previous = sequences[0]
        for sequence in sequences[1:]:
            if sequence == previous + 1:
                previous = sequence
                continue
            ranges.append((start, previous))
            start = previous = sequence
        ranges.append((start, previous))
        return tuple(ranges)

    def recent_task_ids(
        self, projection: SessionConversationProjection, *,
        exclude_task_ids: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """Return newest Tasks whose detailed Tool observations may enter."""
        excluded = set(exclude_task_ids)
        selected: list[str] = []
        for message in reversed(projection.messages):
            if message.task_id in excluded or message.task_id in selected:
                continue
            selected.append(message.task_id)
            if len(selected) >= self.recent_execution_limit:
                break
        return tuple(reversed(selected))

    def project_recent_executions(
        self,
        executions_by_task: Mapping[str, Sequence[ToolExecutionRecord]],
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        """Build bounded atomic call/result observations for recent Tasks.

        The returned objects are descriptive Session data, never native Provider
        tool-protocol messages and never capabilities. Raw bodies remain in the
        Task ledger. Small read-only observations may be copied transiently into
        the prompt; commands, mutations and third-party Tools expose status only.
        """
        candidates: list[tuple[str, ToolExecutionRecord]] = []
        for task_id, values in executions_by_task.items():
            ordered = sorted(
                values, key=lambda item: (item.updated_at, item.execution_id)
            )[-self.recent_execution_per_task_limit:]
            candidates.extend((task_id, item) for item in ordered)
        candidates.sort(key=lambda item: (
            item[1].updated_at, item[1].execution_id
        ))
        selected = candidates[-self.recent_execution_limit:]
        projected: dict[str, list[dict[str, Any]]] = {}
        for task_id, execution in selected:
            projected.setdefault(task_id, []).append(
                self._execution_event(execution)
            )
        return {task_id: tuple(items) for task_id, items in projected.items()}

    def _task_index(
        self, projection: SessionConversationProjection,
    ) -> list[dict[str, Any]]:
        """Build one minimal, traceable directory entry for every Task.

        The index is intentionally smaller than a Task summary, but it keeps the
        facts most likely to be referenced later: identity, goal, state, concrete
        artifacts, completed work, remaining work, and source Event sequences.
        It is retained when the unified context manager compacts old detail.
        """
        messages_by_task: dict[str, list[SessionConversationMessage]] = {}
        for message in projection.messages:
            messages_by_task.setdefault(message.task_id, []).append(message)
        resources_by_task: dict[str, list[str]] = {}
        for resource in projection.resource_catalog:
            if resource.resource_kind is not SessionResourceKind.ARTIFACT:
                continue
            resources_by_task.setdefault(resource.source_task_id, []).append(
                resource.canonical_path
            )
        entries: list[dict[str, Any]] = []
        for summary in projection.task_summaries:
            artifacts = list(dict.fromkeys(
                resources_by_task.get(summary.task_id, ())
            ))
            for mutation in summary.mutations:
                path = mutation.get("path")
                if isinstance(path, str) and path and path not in artifacts:
                    artifacts.append(path)
            task_messages = messages_by_task.get(summary.task_id, ())
            entries.append({
                "task_id": summary.task_id,
                "goal": self._bounded_text(summary.goal, 500),
                "status": summary.recorded_task_state,
                "verification_status": summary.verification_status,
                "artifacts": artifacts[:50],
                "completed_work": self._bounded_texts(
                    summary.completed_work, 10, 500
                ),
                "remaining_work": self._bounded_texts(
                    summary.remaining_work, 10, 500
                ),
                "confirmed": [
                    self._bounded_mapping(item) for item in summary.confirmed[:10]
                ],
                "outcomes": [
                    self._bounded_mapping(item) for item in summary.outcomes[:10]
                ],
                "source_turn_id": summary.turn_id,
                "source_event_sequences": sorted({
                    item.source_event_sequence for item in task_messages
                }),
            })
        return entries

    def _execution_event(
        self, execution: ToolExecutionRecord,
    ) -> dict[str, Any]:
        call = execution.call
        safe_tool = call.name in {
            "core.list_files", "core.find_files",
            "core.read_file", "core.search_text",
        }
        call_data: dict[str, Any] = {
            "call_id": call.call_id, "tool": call.name,
        }
        if safe_tool:
            arguments: dict[str, Any] = {}
            for key in (
                "path", "query", "pattern", "start_line",
                "max_lines", "recursive", "regex",
                "case_sensitive", "max_matches", "limit",
            ):
                value = call.arguments.get(key)
                if isinstance(value, (str, int, float, bool)):
                    arguments[key] = (value[:500] if isinstance(value, str) else value)
            if arguments:
                call_data["arguments"] = arguments
        if call.evidence_question is not None:
            call_data["evidence_question"] = {
                "question_id": call.evidence_question.question_id,
                "question": call.evidence_question.question[:500],
                "expected_scope": call.evidence_question.expected_scope[:1000],
            }

        result_data: dict[str, Any] = {
            "execution_id": execution.execution_id,
            "state": execution.state.value,
        }
        result = execution.result
        if result is not None:
            result_data.update({
                "ok": result.ok, "error_code": result.error_code,
                "truncated": result.truncated,
            })
            if safe_tool and isinstance(result.data, Mapping):
                result_data["observation"] = self._safe_read_observation(
                    call.name, result.data
                )
        return {
            "boundary": "prior_untrusted_tool_observation",
            "call": call_data, "result": result_data,
        }

    def _safe_read_observation(
        self, tool_name: str, data: Mapping[str, Any],
    ) -> dict[str, Any]:
        observation: dict[str, Any] = {}
        for key in (
            "requested_path", "path", "resolved_path",
            "resolved_root", "root_kind", "root_alias", "sha256",
            "start_line", "end_line", "total_lines",
            "scanned_files", "skipped_files", "scanned_entries",
            "skipped_entries", "omitted_sensitive",
            "generated_directories_skipped",
        ):
            value = data.get(key)
            if isinstance(value, (str, int, float, bool)):
                observation[key] = (value[:1000] if isinstance(value, str) else value)

        for field in ("entries", "matches", "candidates"):
            raw_items = data.get(field)
            if not isinstance(raw_items, (list, tuple)):
                continue
            items: list[dict[str, Any]] = []
            for raw in raw_items[:self.execution_result_item_limit]:
                if not isinstance(raw, Mapping):
                    continue
                item: dict[str, Any] = {}
                for key in (
                    "path", "resolved_path", "resolved_root",
                    "root_kind", "type", "size", "line",
                ):
                    value = raw.get(key)
                    if isinstance(value, (str, int, float, bool)):
                        item[key] = (
                            value[:1000] if isinstance(value, str) else value
                        )
                text = raw.get("text")
                if isinstance(text, str) and tool_name == "core.search_text":
                    item["text"] = text[:200]
                if item:
                    items.append(item)
            observation[field + "_count"] = len(raw_items)
            observation[field] = items

        content = data.get("content")
        if isinstance(content, str) and tool_name == "core.read_file":
            observation["content_characters"] = len(content)
            if len(content) <= self.small_read_content_characters:
                observation["content"] = content
                observation["content_included"] = True
            else:
                observation["content_included"] = False
        return observation

    @classmethod
    def _legacy_task_summary(
        cls, payload: Mapping[str, Any], task_id: str, turn_id: str,
        messages: list[SessionConversationMessage],
        resources: Mapping[str, SessionResourceReference],
    ) -> SessionTaskSummary | None:
        if not task_id or not turn_id:
            return None
        raw_state = payload.get("working_state")
        state = raw_state if isinstance(raw_state, Mapping) else {}
        goal = cls._optional_text(state.get("goal"))
        if goal is None:
            goal = next((
                message.text for message in reversed(messages)
                if message.task_id == task_id and message.role is MessageRole.USER
            ), None)
        if goal is None:
            return None
        roots = tuple(dict.fromkeys(
            item.resolved_root for item in resources.values()
            if item.source_task_id == task_id and item.resolved_root
        ))[:20]
        return SessionTaskSummary(
            task_id=task_id, turn_id=turn_id, goal=goal,
            recorded_task_state=(
                cls._optional_text(payload.get("task_state")) or "UNKNOWN"
            ),
            remaining_work=cls._text_tuple(state.get("remaining_work")),
            completed_work=cls._text_tuple(state.get("completed_work")),
            workspace_roots=roots,
        )

    @staticmethod
    def _bounded_text(value: str, limit: int) -> str:
        return value if len(value) <= limit else value[:limit] + "…"

    @classmethod
    def _bounded_texts(
        cls, values: Sequence[str], count: int, characters: int,
    ) -> list[str]:
        return [cls._bounded_text(str(item), characters) for item in values[:count]]

    @classmethod
    def _bounded_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str):
                safe[str(key)] = cls._bounded_text(item, 1000)
            elif isinstance(item, (int, float, bool)) or item is None:
                safe[str(key)] = item
        return safe

    @staticmethod
    def _visible_message(
        raw: Any, expected_role: MessageRole, task_id: str, turn_id: str,
        sequence: int,
    ) -> SessionConversationMessage | None:
        if not isinstance(raw, Mapping) or not task_id or not turn_id:
            return None
        try:
            message = Message.from_data(raw)
        except (KeyError, TypeError, ValueError):
            return None
        text = message.text.strip()
        if message.role is not expected_role or not text:
            return None
        return SessionConversationMessage(
            message.message_id, expected_role, text, task_id, turn_id, sequence
        )

    @staticmethod
    def _apply_explicit_state(target: dict[str, Any], payload: Mapping[str, Any]) -> None:
        raw = payload.get("working_state")
        if not isinstance(raw, Mapping):
            return
        for key in (
            "goal", "constraints", "decisions", "open_questions",
            "completed_work", "remaining_work",
        ):
            if key in raw:
                target[key] = raw[key]

    @staticmethod
    def _apply_working_state(target: dict[str, Any], payload: Mapping[str, Any]) -> None:
        raw = payload.get("working_state")
        if not isinstance(raw, Mapping):
            return
        for key in (
            "goal", "constraints", "decisions", "open_questions",
            "completed_work", "remaining_work",
        ):
            if key in raw:
                target[key] = raw[key]

    @staticmethod
    def _apply_catalogs(
        resources: dict[str, SessionResourceReference],
        questions: dict[str, SessionQuestionReference],
        payload: Mapping[str, Any],
    ) -> None:
        raw_resources = payload.get("resource_catalog", [])
        if isinstance(raw_resources, list):
            for raw in raw_resources:
                if not isinstance(raw, Mapping):
                    continue
                reference = SessionResourceReference.from_data(raw)
                resources.pop(reference.catalog_ref, None)
                resources[reference.catalog_ref] = reference
        raw_questions = payload.get("question_catalog", [])
        if isinstance(raw_questions, list):
            for raw in raw_questions:
                if not isinstance(raw, Mapping):
                    continue
                reference = SessionQuestionReference.from_data(raw)
                questions.pop(reference.question_ref, None)
                questions[reference.question_ref] = reference
        while len(resources) > 200:
            resources.pop(next(iter(resources)))
        while len(questions) > 100:
            questions.pop(next(iter(questions)))

    @staticmethod
    def _optional_text(raw: Any) -> str | None:
        text = str(raw).strip() if raw is not None else ""
        return text or None

    @classmethod
    def _text_tuple(cls, raw: Any) -> tuple[str, ...]:
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(
            text for item in raw if (text := cls._optional_text(item)) is not None
        )
