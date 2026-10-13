"""Task lifecycle and Task SPEC planning for `Kernel`.

A mixin module in the facade-plus-siblings shape (``run_agent.py`` +
``agent/session_persistence.py``): the facade ``core/kernel.py`` imports
this at module level and adds ``TaskMixin`` to ``Kernel``'s bases, so
**every method resolves through ``Kernel``'s MRO unchanged** --
``self._dependencies``, ``self._append_events`` and ``Kernel``'s own class
attributes all still resolve. Moved bodies are verbatim: nothing is renamed
and no signature changes.

This module must never bind the facade at module level; late-import it
inside a method instead (the project's module-boundary rule).

Moved from ``core/kernel.py``:

* ``_finalize_needs_review``
* ``_task_spec_context_message``
* ``_task_spec_planning_context``
* ``create_task``
* ``get_task``
* ``get_task_execution_focus``
* ``get_task_spec``
* ``plan_task_spec``
* ``reconcile_orphaned_running_tasks``
* ``resolve_needs_review``
* ``revise_task_spec``
* ``settle_orphaned_task``
* ``transition_task``

Module-level symbols that moved with them, transitively. They had no
user outside the moved set, and this module must not import the facade
to reach them, so their home is here now:

* ``TERMINAL_AUTHORITY``
* ``_answer_alignment_criterion``
* ``_required_effect_criterion``
* ``_runtime_authored_criteria``
"""

from __future__ import annotations


import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from tsm_agt.ports import (
    CompletionGap, Message, MessageRole, RuntimeEvent, RuntimeUnitOfWork, SessionEvent,
    SessionTaskUnitOfWork, SessionUnitOfWork, TextBlock, ToolResult,
    ModelAttemptFailed, ModelFailureCategory, ModelRecoveryAction,
    ModelRecoveryExhausted, ModelRetrySafety,
)
from typing import Any
from uuid import uuid4
from .agent_loop import AgentTurnCheckpoint
from .configuration import canonical_hash
from .document_references import extract_document_references
from .runtime_input import SessionTaskRelation
from .session import SessionSnapshot, standalone_session_id
from .session_context import (
    ContextAuthority, ContextScope, SessionConversationMessage,
)
from .session_handoff import clip_text
from .conversation_orchestration import (
    OrchestrationMode,
    TaskBindingKind,
    TaskOrchestrationMetadata,
    TaskOrchestrationProjector,
)
from .steering import SteeringKind, SteeringProjector
from .task import (
    SUPERSEDED_CONVERSATION_STATE, TaskKind, TaskNotFound, TaskSnapshot,
    TaskState,
)
from .task_spec import (
    MAX_AUTHORED_ACCEPTANCE_CRITERIA, REQUIRED_EFFECT_CRITERION_ID,
    RUNTIME_AUTHORED_CRITERION_KINDS, TaskAcceptanceCriterion,
    TaskCriterionKind, TaskExecutionFocus, TaskExecutionFocusProjector,
    TaskOutcomeKind, TaskSpecProjector, TaskSpecProposal,
    TaskSpecSnapshot, classify_planner_delivery_intent,
    validate_authored_criterion, validate_planner_delivery_intent,
)
from .turn import InvalidTurnState
from .verification import AcceptanceStatus
from .workspace import capture_workspace_baseline


TERMINAL_AUTHORITY: Mapping[TaskState, frozenset[str]] = {
    TaskState.SUCCEEDED: frozenset({"runtime"}),
    TaskState.FAILED: frozenset({"runtime"}),
    TaskState.CANCELLED: frozenset({"user", "runtime"}),
    TaskState.NEEDS_REVIEW: frozenset({"runtime"}),
}


def _required_effect_criterion(
    effects: frozenset[ToolEffect],
) -> TaskAcceptanceCriterion:
    """Build the Runtime-authored criterion asserting declared delivery."""
    named = ", ".join(sorted(effect.value for effect in effects))
    return TaskAcceptanceCriterion(
        criterion_id=REQUIRED_EFFECT_CRITERION_ID,
        description=(
            "Every side effect the Task spec declares as required has a "
            f"durable record of actually being performed: {named} "
            "(a surviving workspace change for mutate, a successful committed "
            "execution for execute)."
        ),
        verification_kind=TaskCriterionKind.REQUIRED_EFFECT,
    )


def _answer_alignment_criterion(goal: str) -> TaskAcceptanceCriterion:
    """INV-11 criterion: an answer must address the goal it was asked."""
    return TaskAcceptanceCriterion(
        criterion_id="answer-alignment",
        description=(
            "The delivered answer directly addresses the user's current "
            "goal: " + clip_text(goal, 400)
        ),
        verification_kind=TaskCriterionKind.GOAL_ALIGNMENT,
    )


def _runtime_authored_criteria(
    outcomes: Sequence[TaskOutcomeProposal | TaskOutcomeSnapshot], goal: str,
) -> tuple[TaskAcceptanceCriterion, ...]:
    """The Runtime's own criteria for a contract; never Planner-authored.

    Deterministic in the contract alone, so both authoring paths (initial
    planning and every later revision) derive exactly the same set.
    """
    from .kernel import _required_effects_from_outcomes
    criteria: list[TaskAcceptanceCriterion] = []
    if any(outcome.kind is TaskOutcomeKind.ANSWER for outcome in outcomes):
        criteria.append(_answer_alignment_criterion(goal))
    effects = _required_effects_from_outcomes(outcomes)
    if effects:
        criteria.append(_required_effect_criterion(effects))
    return tuple(criteria)


class TaskMixin:
    async def get_task_spec(self, task_id: str) -> TaskSpecSnapshot:
        task = await self.get_task(task_id)
        events = await self._dependencies.store.read_events(task_id)
        return TaskSpecProjector.project(task_id, task.goal, events)

    async def get_task_execution_focus(
        self, task_id: str, *, legacy_active_outcome_ids: tuple[str, ...] = (),
    ) -> TaskExecutionFocus:
        """Project durable execution selection independently of obligations."""
        task = await self.get_task(task_id)
        events = await self._dependencies.store.read_events(task_id)
        spec = TaskSpecProjector.project(task_id, task.goal, events)
        return TaskExecutionFocusProjector.project(
            spec, events,
            legacy_active_outcome_ids=legacy_active_outcome_ids,
        )

    async def _task_spec_planning_context(
        self, task: TaskSnapshot,
    ) -> tuple[str, Mapping[str, Any], int, str]:
        session = await self.get_session(task.session_id)
        events = await self._dependencies.store.read_session_events(
            session.session_id
        )
        projection = self._dependencies.session_context_projector.project(
            session, events
        )
        source_task_id: str | None = None
        context_handoff: Mapping[str, Any] | None = None
        # The Task's own creation record is the authoritative provenance fact
        # (INV-8). Deriving lineage from a recent-window summary instead would
        # make the source depend on recency, which is the defect being fixed.
        for event in await self._dependencies.store.read_events(task.task_id):
            if event.event_type != "task.created":
                continue
            candidate = event.payload.get("source_task_id")
            if isinstance(candidate, str) and candidate:
                source_task_id = candidate
            raw_handoff = event.payload.get("context_handoff")
            if isinstance(raw_handoff, Mapping):
                context_handoff = dict(raw_handoff)
            break
        for summary in reversed(projection.task_summaries):
            if source_task_id is not None:
                break
            if summary.task_id == task.task_id:
                for outcome in reversed(summary.outcomes):
                    candidate = outcome.get("source_task_id")
                    if isinstance(candidate, str) and candidate:
                        source_task_id = candidate
                        break
                if source_task_id is not None:
                    break
        current_request = task.goal
        current_request_index: int | None = None
        for index in range(len(projection.messages) - 1, -1, -1):
            message = projection.messages[index]
            if message.task_id == task.task_id and message.role is MessageRole.USER:
                current_request = message.text.strip() or current_request
                current_request_index = index
                break
        delivery_intent = classify_planner_delivery_intent(current_request)
        interpretation_ids = (
            ()
            if delivery_intent["kind"] == "workspace_mutation"
            else self._preceding_exchange_ids(
                projection.messages, current_request_index
            )
        )
        # INV-8: the Planner is scoped to this Task's own lineage and receives
        # no background at all. Another Task's conversation can therefore never
        # become this Task's goal or acceptance criterion, and recency is not an
        # eligibility rule.
        #
        # A request that only reads as a reply -- "原因 直接写文档吧" answering
        # "如果你愿意...我可以...写文档" -- still has to be interpreted by
        # something. Explicit workspace-mutation requests are already
        # self-contained and must not inherit an assistant's prior limitation;
        # only other requests receive the immediately preceding exchange through
        # ContextScope's explicit-reference escape hatch. It stays
        # NON_AUTHORITATIVE and is delivered under its own section, so it can
        # resolve what the request refers to without authoring a goal.
        scope = ContextScope(
            session_id=task.session_id,
            task_id=task.task_id,
            lineage_task_ids=(task.task_id,),
            explicit_message_ids=interpretation_ids,
        )
        prompt_projection = self._dependencies.session_context_projector.for_prompt(
            projection,
            pinned_task_ids=((source_task_id,) if source_task_id else ()),
            scope=scope,
            include_background=False,
        )
        prompt_data = dict(prompt_projection.prompt_data or {})
        related_task: Mapping[str, Any] | None = None
        for item in reversed(prompt_data.get("task_index", [])):
            if item.get("task_id") == task.task_id and source_task_id is None:
                source_task_id = item.get("source_task_id")
                break
        for summary in prompt_data.get("recent_task_summaries", []):
            if summary.get("task_id") == source_task_id:
                related_task = {
                    "task_id": summary.get("task_id"),
                    "state": summary.get("recorded_task_state"),
                    "verification_status": summary.get("verification_status"),
                    "goal": summary.get("goal"),
                    "historical_remaining_work": summary.get("remaining_work", []),
                    "completed_work": summary.get("completed_work", []),
                    "mutations": summary.get("mutations", []),
                    "authority": ContextAuthority.SCOPED_BACKGROUND.value,
                }
                break
        if related_task is None and context_handoff is not None:
            # INV-9: the persisted handoff is the source identity/state block and
            # is explicitly SCOPED_BACKGROUND; it never carries authority.
            related_task = {
                **context_handoff,
                "authority": ContextAuthority.SCOPED_BACKGROUND.value,
            }
        if related_task is None and source_task_id is not None:
            # A Task that has not recorded an execution summary yet still has an
            # identity. Give the Planner the bounded, non-authoritative source
            # reference rather than silently dropping the lineage.
            try:
                source_task = await self.get_task(source_task_id)
            except TaskNotFound:
                source_task = None
            if source_task is not None:
                related_task = {
                    "task_id": source_task.task_id,
                    "state": source_task.state.value,
                    "verification_status": None,
                    "goal": source_task.goal,
                    "historical_remaining_work": [],
                    "completed_work": [],
                    "mutations": [],
                    "authority": ContextAuthority.SCOPED_BACKGROUND.value,
                }
        # The projector marks every scoped message AUTHORITATIVE. The pulled-in
        # exchange belongs to an earlier Task, so it is separated out here and
        # re-tagged: it may resolve what the request refers to, never author the
        # goal or an acceptance criterion.
        scoped_messages = [
            message for message in prompt_data.get("scoped_messages", [])
            if message.get("task_id") == task.task_id
        ]
        interpretation_context = [
            {
                **message,
                "authority": ContextAuthority.NON_AUTHORITATIVE.value,
            }
            for message in prompt_data.get("scoped_messages", [])
            if message.get("task_id") != task.task_id
        ]
        authority = dict(prompt_data.get("authority", {}))
        authority["interpretation_context"] = (
            ContextAuthority.NON_AUTHORITATIVE.value
        )
        onboarding_facts: list[dict[str, Any]] = []
        onboarding_stale = True
        try:
            onboarding, onboarding_stale = await self.get_project_onboarding(
                task.task_id, include_stale=True
            )
            onboarding_facts = [
                {
                    "category": fact.category,
                    "value": fact.value,
                    "sources": [source.to_data() for source in fact.sources],
                }
                for fact in onboarding.facts
                if fact.category in {
                    "entry_candidate", "source_boundary", "technology",
                }
            ][:40]
        except LookupError:
            pass
        planning_context = {
            "workspace": task.workspace,
            "delivery_intent": delivery_intent,
            "project_onboarding": {
                "authority": "source_linked_observation",
                "static_snapshot_stale": onboarding_stale,
                "facts": onboarding_facts,
            },
            "available_tool_effects": sorted({
                tool.effect.value for tool in await self.list_tools()
                if not tool.is_internal_state
            }),
            "authority": authority,
            "session": {
                "revision": prompt_projection.revision,
                "working_state": prompt_data.get("work_state", {}),
                "scoped_messages": scoped_messages,
                "interpretation_context": interpretation_context,
                "background_messages": prompt_data.get("background_messages", []),
                "recent_artifacts": [
                    {
                        "canonical_path": item.get("canonical_path"),
                        "resource_kind": item.get("resource_kind"),
                        "source_task_id": item.get("source_task_id"),
                        "question_ref": item.get("question_ref"),
                    }
                    for item in prompt_data.get("historical_investigation", {}).get("resources", [])
                    if item.get("canonical_path")
                ][:24],
            },
            "related_task": related_task,
            "selection": dict(prompt_data.get("selection", {})),
        }
        context_hash = canonical_hash(planning_context)
        return (
            current_request,
            planning_context,
            prompt_projection.revision,
            context_hash,
        )

    @staticmethod
    def _preceding_exchange_ids(
        messages: Sequence[SessionConversationMessage],
        input_index: int | None,
    ) -> tuple[str, ...]:
        """The exchange immediately before the current input, by message id.

        Bounded to one assistant message plus the user message it answered, and
        only the turn directly before this input. That is enough to resolve a
        reply, a fragment, or a word that only makes sense against what the
        assistant just said -- and too little to quietly re-supply an earlier
        Task's requirements, which is what INV-8 exists to prevent.
        """

        if input_index is None:
            return ()
        preceding = messages[:input_index]
        if not preceding:
            return ()
        picked = [preceding[-1]]
        if picked[0].role is not MessageRole.USER:
            earlier_user = next((
                message for message in reversed(preceding[:-1])
                if message.role is MessageRole.USER
            ), None)
            if earlier_user is not None:
                picked.insert(0, earlier_user)
        return tuple(message.message_id for message in picked)

    async def plan_task_spec(self, task_id: str) -> TaskSpecSnapshot:
        """Ask the semantic Planner for a proposal; Runtime owns persistence."""
        planner = self._dependencies.task_spec_planner
        current = await self.get_task_spec(task_id)
        task = await self.get_task(task_id)
        # A conversation spans unrelated requests, so one acceptance SPEC cannot
        # describe it. Its per-request meaning lives in the turn messages, and
        # the initial advisory spec is kept as-is.
        if task.task_kind is TaskKind.CONVERSATION:
            return current
        if planner is None or current.outcomes:
            return current
        try:
            (
                current_request,
                planning_context,
                planning_session_revision,
                planning_context_hash,
            ) = await self._task_spec_planning_context(task)
            raw = await planner.propose_task_spec(
                current_request,
                planning_context,
            )
            proposal = TaskSpecProposal.from_data(
                raw, require_acceptance_criteria=True
            )
            validate_planner_delivery_intent(
                proposal, planning_context.get("delivery_intent", {}),
            )
            # Any evidence_reference the Runtime cannot resolve right now --
            # malformed like ``turn-...`` or simply not present -- is demoted to
            # rubric (advisory). Demotion, not a hard failure: a Planner that
            # cannot bind a reference must not fail the whole Task, and an
            # unverifiable required criterion is exactly the dead-lock shape of
            # task-2c8f8fbb75714516a94ab442995626b5. The hard format rejection
            # lives on the model-facing tool path (revise_task_spec), where the
            # caller can simply try again.
            criteria, demoted = self._demote_unresolvable_criteria(
                task, await self._dependencies.store.read_events(task_id),
                proposal.acceptance_criteria,
            )
            if demoted:
                proposal = replace(proposal, acceptance_criteria=criteria)
                await self._append_events(task_id, ((
                    "task_spec.criteria_demoted", {
                        "writer": "task-spec-planner", "demoted": demoted,
                    },
                ),))
            # INV-9: authority is a data boundary, so the Runtime checks what the
            # Planner actually wrote against the background it was given instead
            # of trusting the prompt instruction.
            criteria, leaked = self._demote_background_leaks(
                criteria, planning_context, current_request,
            )
            if leaked:
                proposal = replace(proposal, acceptance_criteria=criteria)
                await self._append_events(task_id, ((
                    "task_spec.background_leak_demoted", {
                        "writer": "task-spec-planner", "demoted": leaked,
                    },
                ),))
            # Runtime-authored criteria are derived from the contract and never
            # accepted from the Planner. Strip any the Planner smuggled in -- the
            # model-facing schemas exclude them, but the text-JSON path does not
            # enforce a schema -- so the checks below always see the authored set
            # and the Runtime's own copy is the only one that survives.
            authored = tuple(
                item for item in proposal.acceptance_criteria
                if item.verification_kind not in RUNTIME_AUTHORED_CRITERION_KINDS
            )
            if authored != proposal.acceptance_criteria:
                proposal = replace(proposal, acceptance_criteria=authored)
            runtime_criteria = _runtime_authored_criteria(
                proposal.outcomes, current_request
            )
            if any(
                item.verification_kind is TaskCriterionKind.GOAL_ALIGNMENT
                for item in runtime_criteria
            ):
                await self._append_events(task_id, ((
                    "task_spec.alignment_criterion_added", {
                        "criterion_id": "answer-alignment",
                        "goal_hash": canonical_hash(current_request),
                    },
                ),))
            proposal = replace(
                proposal,
                acceptance_criteria=(
                    proposal.acceptance_criteria + runtime_criteria
                ),
            )
            candidate = TaskSpecSnapshot.from_proposal(
                task_id, current.revision + 1, proposal
            )
            await self._append_events(task_id, (("task_spec.revised", {
                "writer": "task-spec-planner",
                "orchestration_mode": OrchestrationMode.CONVERSATION_LOOP.value,
                "revision": candidate.revision,
                "content_hash": candidate.content_hash,
                "snapshot": candidate.to_data(),
                "planning_session_revision": planning_session_revision,
                "planning_context_hash": planning_context_hash,
                "prompt_projection_hash": planning_context_hash,
                "current_request_hash": canonical_hash(current_request),
                "current_input_ref": (
                    "task-input:" + canonical_hash(current_request)
                ),
            }),))
            return candidate
        except Exception as error:
            model_failure = isinstance(
                error, (ModelAttemptFailed, ModelRecoveryExhausted)
            )
            await self._append_events(task_id, (("task_spec.planning_failed", {
                "recoverable": model_failure,
                "error_type": type(error).__name__,
                "message": str(error)[:1000],
            }),))
            if model_failure:
                # The provider failed before any planning work was committed, so
                # there is no product failure to report: the Task stops at the
                # recoverable interruption the Runtime already defines, with its
                # checkpoint intact. Marking it FAILED turned a gateway hiccup
                # into a dead Task (task-e547187aaaac4f4b83ae9ff1d2c767b5).
                failure = getattr(error, "failure", None)
                decision = getattr(error, "decision", None)
                stored = await self._require_stored_task(task_id)
                checkpoint = (
                    TaskSnapshot.from_data(stored.data).active_agent_checkpoint or {}
                )
                await self._record_recoverable_turn_interruption(
                    task_id, str(checkpoint.get("turn_id") or ""), error,
                    failure_kind="provider",
                    failure_category=getattr(
                        failure, "category", ModelFailureCategory.UNKNOWN,
                    ),
                    retry_safety=getattr(
                        failure, "retry_safety", ModelRetrySafety.NEVER,
                    ),
                    recovery_action=getattr(
                        decision, "action", ModelRecoveryAction.FAIL_TERMINAL,
                    ),
                )
            raise

    async def revise_task_spec(
        self, task_id: str, expected_revision: int, *,
        scope: tuple[str, ...], constraints: tuple[str, ...],
        acceptance_criteria: tuple[TaskAcceptanceCriterion, ...],
        operation_id: str, writer: str, goal: str | None = None,
        allow_goal_change: bool = False,
    ) -> TaskSpecSnapshot:
        """Revise one Task spec; only Replace may change its goal."""
        for item in acceptance_criteria:
            validate_authored_criterion(
                item.verification_kind, item.evidence_reference, item.command,
            )
        if not operation_id.strip() or not writer.strip():
            raise ValueError("Task SPEC operation and writer are required")
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        if task.state.is_terminal:
            raise InvalidTurnState(
                f"Task SPEC cannot change in terminal state {task.state.value}"
            )
        events = await self._dependencies.store.read_events(task_id)
        current = TaskSpecProjector.project(task_id, task.goal, events)
        selected_goal = (goal if goal is not None else current.goal).strip()
        if selected_goal != current.goal and not allow_goal_change:
            raise ValueError("Task SPEC goal can change only through Runtime Replace")
        # The Runtime's own criteria are re-derived from the (immutable) Outcome
        # set on every revision, so a caller can neither drop them nor smuggle in
        # a weaker look-alike.
        authored = tuple(
            item for item in acceptance_criteria
            if item.verification_kind not in RUNTIME_AUTHORED_CRITERION_KINDS
        )
        if len(authored) > MAX_AUTHORED_ACCEPTANCE_CRITERIA:
            raise ValueError(
                f"Task SPEC accepts at most {MAX_AUTHORED_ACCEPTANCE_CRITERIA} "
                "author-supplied acceptance criteria; the Runtime reserves two"
            )
        acceptance_criteria = authored + _runtime_authored_criteria(
            current.outcomes, selected_goal
        )
        request_hash = canonical_hash({
            "expected_revision": expected_revision,
            "goal": selected_goal, "scope": list(scope),
            "constraints": list(constraints),
            "acceptance_criteria": [
                item.to_data() for item in acceptance_criteria
            ],
        })
        existing = next((
            event for event in events
            if event.event_type == "task_spec.revised"
            and event.payload.get("operation_id") == operation_id
        ), None)
        if existing is not None:
            if existing.payload.get("request_hash") != request_hash:
                raise ValueError("Task SPEC operation_id was reused differently")
            return TaskSpecProjector.project(task_id, task.goal, events)
        if current.revision != expected_revision:
            raise ValueError(
                f"Task SPEC revision conflict: expected {expected_revision}, "
                f"got {current.revision}"
            )
        candidate = TaskSpecSnapshot(
            task_id=task_id, revision=current.revision + 1,
            goal=selected_goal, scope=scope, constraints=constraints,
            acceptance_criteria=acceptance_criteria,
            outcomes=current.outcomes,
            continuation_mode=current.continuation_mode,
            schema_version=current.schema_version,
        )
        event = RuntimeEvent(
            f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
            "task_spec.revised", {
                "operation_id": operation_id, "request_hash": request_hash,
                "writer": writer, "revision": candidate.revision,
                "content_hash": candidate.content_hash,
                "snapshot": candidate.to_data(),
            },
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, task.to_data(), (event,)
        ))
        return candidate

    async def _supersede_idle_conversations(
        self, session: SessionSnapshot,
    ) -> None:
        """Close idle conversation tasks before creating a new conversation.

        Enforces "at most one CONVERSATION task per Session" and repairs any
        history that predates the invariant. Each close is its own commit, so a
        failure between closing and creating leaves the old conversation closed
        but still readable -- never two live conversations.
        """

        for task_id in session.task_ids:
            stored = await self._dependencies.store.load_task(task_id)
            if stored is None:
                continue
            task = TaskSnapshot.from_data(stored.data)
            if not (
                task.task_kind is TaskKind.CONVERSATION
                and task.state is TaskState.AWAITING_INPUT
            ):
                continue
            # Replacement is not a verified success: close it directly instead
            # of walking the acceptance-gated SUCCESS_PATH.
            superseded = task.transition(SUPERSEDED_CONVERSATION_STATE)
            event = RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
                "task.state_changed", {
                    "previous_state": task.state.value,
                    "next_state": superseded.state.value,
                    "reason": "superseded by new conversation",
                },
            )
            await self._dependencies.store.commit(RuntimeUnitOfWork(
                task_id, stored.version, superseded.to_data(), (event,)
            ))
            await self._record_session_task_state(superseded)

    async def create_task(
        self, goal: str, workspace: Path, task_id: str | None = None,
        session_id: str | None = None, command_id: str | None = None,
        source_task_id: str | None = None,
        task_relation: SessionTaskRelation = SessionTaskRelation.INDEPENDENT,
        original_user_text: str | None = None,
        context_handoff: Mapping[str, Any] | None = None,
    ) -> TaskSnapshot:
        """Create one Task and attach it to its Session.

        ``original_user_text`` is the message the user actually typed. A goal is
        not that message: a derived Task rewrites it into a bounded handoff, so
        replaying the goal as conversation history would show the user words
        they never wrote. Recording it here keeps it in the same commit as the
        attachment, so history can never contain a Task without its input.
        """
        normalized_goal = goal.strip()
        if not normalized_goal:
            raise ValueError("task goal must not be empty")
        if command_id is not None:
            if not command_id.strip():
                raise ValueError("command_id must not be empty")
            prior = await self._dependencies.store.load_session_task_command(command_id)
            if prior is not None:
                prior_session_id, prior_task_id = prior
                if task_id is not None and prior_task_id != task_id:
                    raise ValueError("command_id was reused with a different task_id")
                if session_id is not None and prior_session_id != session_id:
                    raise ValueError("command_id was reused with a different session_id")
                prior_task = await self.get_task(prior_task_id)
                prior_workspace = self._dependencies.workspace_path.normalize_workspace(
                    Path(prior_task.workspace)
                )
                requested_workspace = self._dependencies.workspace_path.normalize_workspace(
                    workspace
                )
                if (
                    prior_task.goal != normalized_goal
                    or prior_workspace != requested_workspace
                ):
                    raise ValueError("command_id was reused with different Task input")
                return prior_task
        resolved_workspace = self._dependencies.workspace_path.normalize_workspace(
            workspace
        )

        identity = task_id or f"task-{uuid4().hex}"
        session_identity = session_id or standalone_session_id(identity)
        session_stored = await self._dependencies.store.load_session(session_identity)
        if session_stored is None:
            if session_id is not None:
                raise LookupError(f"session not found: {session_identity}")
            session = SessionSnapshot.create(
                session_identity, self._dependencies.local_identity.current_subject(),
                normalized_goal[:120],
            )
            session_version = 0
            session_sequence = 0
            session_events = (SessionEvent(
                f"sevt-{uuid4().hex}", session_identity, 1, "session.created",
                {"title_hash": canonical_hash(session.title),
                 "subject": session.subject, "standalone": True},
            ),)
            attach_sequence = 2
        else:
            session = SessionSnapshot.from_data(session_stored.data)
            self._authorize_session(session)
            session_version = session_stored.version
            session_sequence = session_stored.last_event_sequence
            session_events = ()
            attach_sequence = session_sequence + 1
            # A Session belongs to one workspace. Without this invariant a
            # caller that forgets to pass the workspace silently runs the Task
            # against a different directory, and the Session's own history then
            # describes two unrelated projects.
            await self._require_consistent_session_workspace(
                session, resolved_workspace
            )
        if task_relation in {
            SessionTaskRelation.FOLLOW_UP, SessionTaskRelation.BRANCH
        }:
            if source_task_id is None or source_task_id not in session.task_ids:
                raise ValueError(
                    "derived Task source must belong to the same Session"
                )
        elif source_task_id is not None:
            raise ValueError("non-derived Task cannot have a source Task")
        # A conversation is the durable unit: only an independent Task created
        # under the conversation loop is one. Derived/follow-up work stays a
        # work item so it keeps the one-turn completion path.
        task_kind = (
            TaskKind.CONVERSATION
            if (
                source_task_id is None
                and self._dependencies.orchestration_mode
                is OrchestrationMode.CONVERSATION_LOOP
            )
            else TaskKind.WORK_ITEM
        )
        if task_kind is TaskKind.CONVERSATION:
            await self._supersede_idle_conversations(session)
            if session_stored is not None:
                # Superseding wrote session events, so the version and sequence
                # read above are stale; reload before the atomic create commit.
                reloaded = await self._dependencies.store.load_session(
                    session_identity
                )
                assert reloaded is not None
                session = SessionSnapshot.from_data(reloaded.data)
                session_version = reloaded.version
                session_sequence = reloaded.last_event_sequence
                attach_sequence = session_sequence + 1
        session = session.attach_task(identity)
        attached_payload: dict[str, Any] = {
            "task_id": identity, "active_task_id": identity,
            "source_task_id": source_task_id,
            "task_relation": task_relation.value,
        }
        recorded_input = (original_user_text or "").strip()
        if recorded_input:
            attached_payload["user_text"] = recorded_input
        attached_event = SessionEvent(
            f"sevt-{uuid4().hex}", session_identity, attach_sequence,
            "session.task_attached", attached_payload,
        )
        session_events += (attached_event,)
        if source_task_id is not None:
            session_events += (SessionEvent(
                f"sevt-{uuid4().hex}", session_identity, attach_sequence + 1,
                "session.task_derived", {
                    "task_id": identity,
                    "source_task_id": source_task_id,
                    "task_relation": task_relation.value,
                    "inherited": ["authority_free_session_summary"],
                    "not_inherited": [
                        "approval", "workspace_access_grant", "sandbox_grant",
                        "checkpoint", "tool_batch", "background_process",
                        "unknown_outcome",
                    ],
                },
            ),)

        trust = await self.get_project_trust(resolved_workspace)
        baseline = capture_workspace_baseline(
            resolved_workspace, self._dependencies.workspace_path
        )
        snapshot = TaskSnapshot.create(
            task_id=identity,
            goal=normalized_goal,
            workspace=str(resolved_workspace),
            session_id=session_identity,
            task_kind=task_kind,
        ).with_project_trust(
            trust.level, trust.fingerprint, trust.subject
        ).with_workspace_baseline(baseline)
        task_spec = TaskSpecSnapshot.initial(snapshot.task_id, snapshot.goal)
        document_references = extract_document_references(normalized_goal)
        visible_tool_names = {tool.name for tool in await self.list_tools()}
        document_fetch_failure = (
            ToolResult(
                call_id=f"document-fetch-{canonical_hash(normalized_goal)[:16]}",
                ok=False,
                error_code="DOCUMENT_FETCH_UNAVAILABLE",
                message=(
                    "An explicit document URL was supplied, but this runtime does "
                    "not advertise web.fetch_markdown. The document was not read."
                ),
                hint=(
                    "Configure and advertise web.fetch_markdown before reading the "
                    "URL. Do not use workspace search as a substitute."
                ),
                retryable=False,
                data={"document_references": list(document_references)},
                meta={"runtime_guard": "EXPLICIT_DOCUMENT_REFERENCE"},
            )
            if document_references and "web.fetch_markdown" not in visible_tool_names
            else None
        )
        orchestration_metadata = TaskOrchestrationMetadata(
            schema_version=1,
            orchestration_mode=self._dependencies.orchestration_mode,
            root_goal=normalized_goal,
            root_input_event_ref=f"session-event:{attached_event.event_id}",
            latest_input_event_ref=f"session-event:{attached_event.event_id}",
            binding_kind=TaskBindingKind.NEW,
        )
        event = RuntimeEvent(
            event_id=f"evt-{uuid4().hex}",
            task_id=snapshot.task_id,
            sequence=1,
            event_type="task.created",
            payload={
                "goal": snapshot.goal, "workspace": snapshot.workspace,
                "session_id": session_identity,
                "conversation": orchestration_metadata.to_data(),
                "source_task_id": source_task_id,
                "task_relation": task_relation.value,
                # INV-9: a derived Task's source facts are persisted separately
                # from its goal and are explicitly non-authoritative. The goal
                # itself is only ever the user's current request.
                "context_handoff": (
                    dict(context_handoff)
                    if context_handoff is not None else None
                ),
                "project_trust": snapshot.project_trust.value,
                "project_fingerprint": snapshot.project_fingerprint,
                "trust_subject": snapshot.trust_subject,
                "task_spec": task_spec.to_data(),
                "document_references": list(document_references),
                "document_fetch_failure": (
                    document_fetch_failure.to_data()
                    if document_fetch_failure is not None else None
                ),
                "baseline": {
                    "captured_at": baseline.captured_at.isoformat(),
                    "file_count": len(baseline.files),
                    "git_head": baseline.git_head,
                    "skipped_files": baseline.skipped_files,
                    "truncated": baseline.truncated,
                },
            },
        )
        await self._dependencies.store.commit_session_and_task(SessionTaskUnitOfWork(
            SessionUnitOfWork(
                session_identity, session_version, session.to_data(), session_events
            ),
            RuntimeUnitOfWork(
                snapshot.task_id, 0, snapshot.to_data(),
                (event,),
            ),
            command_id or f"command-create-task-{uuid4().hex}",
        ))
        return snapshot

    async def _task_spec_context_message(self, task_id: str) -> Message:
        snapshot = await self.get_task_spec(task_id)
        task = await self.get_task(task_id)
        workspace = self._dependencies.workspace_path.normalize_workspace(
            Path(task.workspace)
        )
        context_data = {
            "boundary": "inspectable_task_spec",
            "runtime_environment": {
                "primary_workspace": str(workspace),
                "relative_path_base": str(workspace),
                "path_semantics": (
                    "Relative paths including '.' use this base; the base is not "
                    "evidence that a user-named target is this workspace."
                ),
                "external_reads": (
                    "Pass the requested path; Runtime may require Task-scoped approval."
                ),
            },
            "task_spec": (
                # A conversation spans unrelated requests: the initial SPEC is
                # background, never the acceptance criteria for the current turn.
                {
                    "advisory": True,
                    "reason": (
                        "conversation task; the current request is the latest "
                        "user message"
                    ),
                }
                if task.task_kind is TaskKind.CONVERSATION
                else snapshot.to_data()
            ),
        }
        body = json.dumps(
            context_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return Message(
            f"task-spec-context-r{snapshot.revision}-{snapshot.content_hash[:16]}",
            MessageRole.SYSTEM, (TextBlock(body),),
        )

    async def get_task(self, task_id: str) -> TaskSnapshot:
        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        return TaskSnapshot.from_data(stored.data)

    async def get_task_orchestration_metadata(
        self, task_id: str,
    ) -> TaskOrchestrationMetadata:
        """Project the Task's creation-time execution identity from its ledger."""
        task = await self.get_task(task_id)
        events = await self._dependencies.store.read_events(task_id)
        return TaskOrchestrationProjector.project(task_id, task.goal, events)

    async def transition_task(
        self, task_id: str, target: TaskState, reason: str, *,
        authority: str = "runtime", intent: str | None = None,
    ) -> TaskSnapshot:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("transition reason must not be empty")
        allowed_authority = TERMINAL_AUTHORITY.get(target)
        if allowed_authority is not None and authority not in allowed_authority:
            raise InvalidTurnState(
                f"transition to {target.value} requires authority in "
                f"{sorted(allowed_authority)}, got {authority!r}"
            )

        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        current = TaskSnapshot.from_data(stored.data)
        if current.state is TaskState.CANCELLED and target in {
            TaskState.FINALIZING, TaskState.SUCCEEDED,
        }:
            raise InvalidTurnState(
                f"cancelled task {task_id} cannot enter {target.value}"
            )

        if (
            target in {TaskState.FINALIZING, TaskState.SUCCEEDED}
            and self._dependencies.final_acceptance_policy is not None
        ):
            verification_events = [
                event for event in await self._dependencies.store.read_events(task_id)
                if event.event_type == "verify.completed"
            ]
            latest_status = (
                str(verification_events[-1].payload.get("status", ""))
                if verification_events else ""
            )
            if latest_status != AcceptanceStatus.PASSED.value:
                raise InvalidTurnState(
                    f"task {task_id} cannot enter {target.value} without a "
                    "latest passed trusted verification"
                )
        if (
            current.pending_approval is not None
            and target in {TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW}
        ):
            raise InvalidTurnState(
                "pending approval can only be resumed through resolve_approval"
            )
        # Validate the transition before causing cleanup side effects.
        updated = current.transition(target)
        if target.is_terminal:
            await self._cleanup_background_processes_for_task_end(task_id, current)
            stored = await self._require_stored_task(task_id)
            current = TaskSnapshot.from_data(stored.data)
            updated = current.transition(target)
        events: list[RuntimeEvent] = []
        if (
            target in {TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW}
            and not current.effective_configurations
        ):
            configuration = await self._build_effective_configuration(current, 1)
            updated = updated.with_effective_configuration(configuration)
            events.append(RuntimeEvent(
                event_id=f"evt-{uuid4().hex}", task_id=task_id,
                sequence=stored.last_event_sequence + 1,
                event_type="config.snapshot",
                payload={
                    "revision": configuration.revision,
                    "effective_config_hash": configuration.effective_config_hash,
                    "toolset_hash": configuration.toolset_hash,
                    "adapter_lock_hash": configuration.adapter_lock_hash,
                    "policy_hash": configuration.policy_hash,
                    "provider_capabilities_hash": (
                        configuration.provider_capabilities_hash
                    ),
                },
            ))
        event = RuntimeEvent(
            event_id=f"evt-{uuid4().hex}",
            task_id=task_id,
            sequence=stored.last_event_sequence + len(events) + 1,
            event_type="task.state_changed",
            payload={
                "previous_state": current.state.value,
                "next_state": updated.state.value,
                "reason": normalized_reason,
                **(
                    {"intent": intent, "schema_version": 1}
                    if intent is not None else {}
                ),
            },
        )
        events.append(event)
        await self._dependencies.store.commit(
            RuntimeUnitOfWork(
                task_id=task_id,
                expected_version=stored.version,
                next_state=updated.to_data(),
                events=tuple(events),
            )
        )
        if target.is_terminal:
            await self._record_session_task_state(updated)
            await self._close_cancellation_scope(task_id)
            if target is TaskState.CANCELLED:
                self._cancelled_task_ids.add(task_id)
        if target is TaskState.RESOLVING_PROJECT:
            await self.run_project_onboarding(task_id)
            # Inspect the one explicit optional instructions file during project
            # resolution so Turn event boundaries remain stable and auditable.
            await self.get_project_instructions(task_id, audit=True)
            return await self.get_task(task_id)
        return updated

    async def resolve_needs_review(
        self, task_id: str, decision: str, *, reason: str,
    ) -> TaskSnapshot:
        """Resolve a NEEDS_REVIEW Task with an explicit human decision.

        ``decision`` is ``accept`` (SUCCEEDED, recorded as manual verification),
        ``return_for_revision`` (EXECUTING with the unmet criteria queued as
        steering), or ``cancel`` (CANCELLED). Only this explicit channel can
        leave NEEDS_REVIEW; it bypasses machine verification on purpose because
        the human, not the verifier, is accepting the unverified result.
        """
        normalized = decision.strip().lower()
        if normalized not in {"accept", "return_for_revision", "cancel"}:
            raise ValueError(
                "decision must be accept, return_for_revision, or cancel"
            )
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        if task.state is not TaskState.NEEDS_REVIEW:
            raise InvalidTurnState(
                f"task {task_id} is not awaiting human review, "
                f"got {task.state.value}"
            )
        target = {
            "accept": TaskState.SUCCEEDED,
            "return_for_revision": TaskState.EXECUTING,
            "cancel": TaskState.CANCELLED,
        }[normalized]

        if target.is_terminal:
            # Mirror transition_task: a terminal Task must not leave background
            # processes running. Cleanup may commit its own events.
            await self._cleanup_background_processes_for_task_end(task_id, task)
            stored = await self._require_stored_task(task_id)
            task = TaskSnapshot.from_data(stored.data)

        events: list[RuntimeEvent] = []
        sequence = stored.last_event_sequence

        def next_event(event_type: str, payload: Mapping[str, Any]) -> RuntimeEvent:
            nonlocal sequence
            sequence += 1
            return RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, sequence, event_type, dict(payload),
            )

        events.append(next_event("task.review_resolved", {
            "decision": normalized,
            "reason": reason.strip() or f"human review: {normalized}",
            "actor": "user",
        }))
        if normalized == "accept":
            # A human accepted an unverified result. Record it as a manual pass
            # so downstream projections stay consistent and auditable.
            events.append(next_event("verify.completed", {
                "status": AcceptanceStatus.PASSED.value,
                "manual_verified": True,
                "actor": "user",
                "criterion_count": 0,
            }))
        elif normalized == "return_for_revision":
            gaps = ()
            unmet = "; ".join(
                gap.description for gap in gaps if gap.required
            ) or "the remaining required work"
            text = f"按人工复核意见继续完成：{unmet}"
            steering = SteeringProjector.project(
                task_id, await self._dependencies.store.read_events(task_id)
            )
            events.append(next_event("steering.queued", {
                "steering_id": f"review-{uuid4().hex}",
                "inbound_sequence": steering.latest_inbound_sequence + 1,
                "kind": SteeringKind.STEER.value,
                "text": text,
                "text_hash": canonical_hash(text),
                "request_hash": canonical_hash({
                    "kind": SteeringKind.STEER.value, "text": text,
                }),
            }))
        updated = task.transition(target)
        if task.active_agent_checkpoint is not None:
            checkpoint = AgentTurnCheckpoint.from_data(
                task.active_agent_checkpoint
            )
            updated = updated.with_agent_checkpoint(replace(
                checkpoint, pending_user_action={},
            ).to_data())
        events.append(next_event("task.state_changed", {
            "previous_state": task.state.value,
            "next_state": target.value,
            "reason": f"human review: {normalized}",
        }))
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, updated.to_data(), tuple(events)
        ))
        if target.is_terminal:
            await self._record_session_task_state(updated)
            await self._close_cancellation_scope(task_id)
            if target is TaskState.CANCELLED:
                self._cancelled_task_ids.add(task_id)
        return updated

    async def reconcile_orphaned_running_tasks(self) -> tuple[str, ...]:
        """Settle Tasks a previous process left mid-flight.

        A non-terminal Task state claims that Runtime work is happening, but the
        work itself is process-local: after a restart there is none. The Session
        still keeps that Task as its active one, so every later message is
        answered "busy" (or steered into a queue nothing drains) with no error
        and no progress -- the shape behind
        ``task-c2d21bce8ea74f0bbcac07aba4aefedf`` and
        ``task-fb9d5b46923345cfb2ba7653e342f2da``. This covers every such state,
        not only the ones named "running": the second incident was a Task frozen
        in VERIFYING because a restart killed the verifier's judge call.

        Called once after adapters start and before any user input is accepted.
        Idempotent: a settled Task is no longer in a claimed-work state.
        """

        settled: list[str] = []
        for session in await self.list_sessions(include_archived=True):
            for task in await self.list_session_tasks(session.session_id):
                if await self.settle_orphaned_task(task.task_id):
                    settled.append(task.task_id)
        return tuple(settled)

    async def settle_orphaned_task(self, task_id: str) -> bool:
        """Settle one Task whose claimed in-process work no longer exists.

        Returns whether a settlement was applied. The caller is responsible for
        establishing that no live turn drives the Task; at start-up that is
        true by definition, and on the input path the SDK checks its own runner
        registry first.
        """

        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            return False
        task = TaskSnapshot.from_data(stored.data)
        target = self._ORPHANED_SETTLEMENTS.get(task.state)
        if target is None:
            return False
        try:
            if target is TaskState.INTERRUPTED:
                if task.state not in {
                    TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW,
                }:
                    # Interruption is only defined from a running state, so step
                    # back to the state whose checkpoint a resume re-runs.
                    await self.transition_task(
                        task.task_id, TaskState.EXECUTING,
                        "orphaned task returned to execution for settling",
                    )
                await self.interrupt_agent_turn(
                    task.task_id, "orphaned task settled on startup",
                )
            else:
                await self.transition_task(
                    task.task_id, target,
                    f"orphaned {task.state.value} settled on startup",
                )
        except (InvalidTurnState, TaskNotFound):
            return False
        return True
