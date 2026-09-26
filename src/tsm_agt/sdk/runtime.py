"""One Runtime command/event facade shared by SDK and local transports."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from tsm_agt.bootstrap import (
    Application, compose_openai_compatible_engineering_application_from_env,
)
from tsm_agt.core import (
    AgentClarificationSuspended, AgentProgress, AgentTurnResult,
    AgentTurnSuspended,
    AgentContinuationSuspended,
    ApprovalDecision, ApprovalResolutionInput, ClarificationReplyInput,
    InterruptTaskInput, RuntimeTextInput, SessionTextInput,
    SessionAnswerRequiresTask,
    SessionInputDecision, SessionRouteDisposition, SessionTaskRelation,
    SessionSnapshot,
    TaskRuntimeProjection, TaskRuntimeProjector,
    TaskSnapshot, TaskState, build_session_follow_up_goal,
    canonical_hash, SteeringKind, RuntimeInputIntent,
)
from tsm_agt.ports import (
    CompletionReadinessMode, ImageBlock, RuntimeCommandRecord,
    RuntimeStorePort, SessionInputRelation, SessionInputRelationJudgement,
    SessionInputRelationPort, TextBlock,
)


class CommandInProgress(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeEventEnvelope:
    """Redacted event metadata: payloads never cross the public event boundary."""

    event_id: str
    task_id: str
    cursor: int
    event_type: str
    occurred_at: str
    schema_version: int

    def to_data(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "task_id": self.task_id,
            "cursor": self.cursor, "type": self.event_type,
            "occurred_at": self.occurred_at,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class RuntimeProgressEnvelope:
    """Ephemeral, concrete progress for an authenticated local UI.

    Unlike ``RuntimeEventEnvelope``, this object is not persisted and is not
    redacted.  It can include the user's goal, evidence question, paths,
    searches, and tool arguments so a local Web UI can explain the live run.
    """

    task_id: str
    sequence: int
    progress: AgentProgress

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "sequence": self.sequence,
            "progress": self.progress.to_data(),
        }


@dataclass(frozen=True, slots=True)
class RuntimeTaskResult:
    task_id: str
    state: str
    phase1_state: str
    status: str
    cursor: int
    assistant_text: str | None = None
    approval: Mapping[str, Any] | None = None
    clarification: Mapping[str, Any] | None = None
    verification: Mapping[str, Any] | None = None
    evidence_level: Mapping[str, Any] | None = None
    projection: Mapping[str, Any] | None = None
    latest_answer_event_ref: str | None = None
    conclusion_claims: tuple[Mapping[str, Any], ...] = ()
    conclusion_validation: Mapping[str, Any] | None = None
    completion_diagnostics: Mapping[str, Any] | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "state": self.state,
            "phase1_state": self.phase1_state,
            "status": self.status, "cursor": self.cursor,
            "assistant_text": self.assistant_text,
            "approval": dict(self.approval) if self.approval else None,
            "clarification": (
                dict(self.clarification) if self.clarification else None
            ),
            "verification": dict(self.verification) if self.verification else None,
            "evidence_level": (
                dict(self.evidence_level) if self.evidence_level else None
            ),
            "projection": dict(self.projection) if self.projection else None,
            "latest_answer_event_ref": self.latest_answer_event_ref,
            "conclusion_claims": [dict(item) for item in self.conclusion_claims],
            "conclusion_validation": (
                dict(self.conclusion_validation)
                if self.conclusion_validation is not None else None
            ),
            "completion_diagnostics": (
                dict(self.completion_diagnostics)
                if self.completion_diagnostics is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class RuntimeCommandResult:
    command_id: str
    replayed: bool
    result: Mapping[str, Any]

    def to_data(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id, "replayed": self.replayed,
            "result": dict(self.result),
        }


@dataclass(frozen=True, slots=True)
class SessionTextResult:
    """Result of a normal Session message routed by the Runtime."""

    command_id: str
    kind: str
    decision: Mapping[str, Any]
    task: RuntimeTaskResult | None = None
    answer: str | None = None
    clarification: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "kind": self.kind,
            "decision": dict(self.decision),
            "task": self.task.to_data() if self.task else None,
            "answer": self.answer,
            "clarification": self.clarification,
        }


@dataclass(frozen=True, slots=True)
class UserInputResult:
    """Outcome of one unified user input, independent of the calling client."""

    command_id: str
    kind: str
    relation: str
    reason_code: str
    decision: Mapping[str, Any] | None = None
    task: RuntimeTaskResult | None = None
    answer: str | None = None
    clarification: str | None = None
    routed_task_id: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "kind": self.kind,
            "relation": self.relation,
            "reason_code": self.reason_code,
            "decision": dict(self.decision) if self.decision else None,
            "task": self.task.to_data() if self.task else None,
            "answer": self.answer,
            "clarification": self.clarification,
            "routed_task_id": self.routed_task_id,
        }


class EngineeringAgentClient:
    """Async SDK facade; it owns Adapter lifecycle, never reimplements the loop."""
    def __init__(
        self, workspace: Path, *,
        application_factory: Callable[[], Application] | None = None,
        poll_interval: float = 0.1,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        self._application_factory = application_factory or (lambda: (
            compose_openai_compatible_engineering_application_from_env(
                env_file=self.workspace / ".env",
                database_path=self.workspace / ".agent" / "runtime.db",
            )
        ))
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._poll_interval = poll_interval
        self._application: Application | None = None
        self._run_tasks: dict[str, asyncio.Task[RuntimeTaskResult]] = {}
        self._latest_results: dict[str, RuntimeTaskResult] = {}
        self._progress_history: dict[str, list[RuntimeProgressEnvelope]] = {}
        self._progress_sequences: dict[str, int] = {}
        self._progress_changed = asyncio.Condition()
        self._submit_lock = asyncio.Lock()
        # One foreground Task per Session: serialize inputs per Session id.
        self._session_input_locks: dict[str, asyncio.Lock] = {}

    @property
    def application(self) -> Application:
        if self._application is None:
            raise RuntimeError("EngineeringAgentClient is not started")
        return self._application

    async def start(self) -> EngineeringAgentClient:
        if self._application is None:
            self._application = self._application_factory()
            await self._application.registry.start_all()
        return self

    async def close(self) -> None:
        for task_id, runner in tuple(self._run_tasks.items()):
            if runner.done():
                continue
            try:
                current = await self.application.kernel.get_task(task_id)
                if current.state in {
                    TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW
                }:
                    await self.application.kernel.interrupt_agent_turn(
                        task_id, "SDK client is closing"
                    )
            except (LookupError, RuntimeError, ValueError):
                pass
            runner.cancel()
        if self._run_tasks:
            await asyncio.gather(*self._run_tasks.values(), return_exceptions=True)
        self._run_tasks.clear()
        if self._application is not None:
            await self._application.registry.stop_all()
            self._application = None

    async def __aenter__(self) -> EngineeringAgentClient:
        return await self.start()

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def create_task(
        self, goal: str, *, command_id: str, session_id: str | None = None,
        source_task_id: str | None = None,
        task_relation: SessionTaskRelation = SessionTaskRelation.INDEPENDENT,
        workspace: Path | None = None,
        original_user_text: str | None = None,
    ) -> TaskSnapshot:
        """Create one Task, optionally in a workspace other than the client's.

        One client process may serve several workspaces, so the caller must be
        able to name the workspace a Task actually runs in. Omitting it keeps
        the client's own workspace, which is the single-workspace default.

        ``original_user_text`` preserves the message the user typed, which a
        derived goal deliberately rewrites.
        """
        return await self.application.kernel.create_task(
            goal, workspace or self.workspace, session_id=session_id,
            command_id=command_id, source_task_id=source_task_id,
            task_relation=task_relation,
            original_user_text=original_user_text,
        )

    async def submit_task(
        self, goal: str, *, command_id: str, session_id: str | None = None,
        source_task_id: str | None = None,
        task_relation: SessionTaskRelation = SessionTaskRelation.INDEPENDENT,
        images: tuple[ImageBlock, ...] = (),
        workspace: Path | None = None,
        original_user_text: str | None = None,
    ) -> RuntimeTaskResult:
        async with self._submit_lock:
            task = await self.create_task(
                goal, command_id=command_id, session_id=session_id,
                source_task_id=source_task_id, task_relation=task_relation,
                workspace=workspace, original_user_text=original_user_text,
            )
            current = await self.application.kernel.get_task(task.task_id)
            if (
                current.state is TaskState.CREATED
                and task.task_id not in self._run_tasks
            ):
                runner = asyncio.create_task(
                    self._run_submitted_task(task.task_id, goal, images=images),
                    name=f"tsm-agt-sdk-{task.task_id}",
                )
                self._run_tasks[task.task_id] = runner
                runner.add_done_callback(
                    lambda _done, task_id=task.task_id: self._run_tasks.pop(
                        task_id, None
                    )
                )
        return await self.get_task_result(task.task_id)

    async def submit_session_text(
        self, session_id: str, text: str, *, command_id: str,
        images: tuple[ImageBlock, ...] = (),
        workspace: Path | None = None,
    ) -> RuntimeCommandResult:
        """Route one ordinary message and execute its validated Session action.

        ``command_id`` is the caller's stable per-message request id. It is
        also passed to ``create_task`` when a Task is created, preserving the
        Kernel's stricter Task command receipt rather than bypassing it.

        ``images`` matches ``submit_task``: already-validated ``ImageBlock``
        values, whose accepted sources are that type's invariant. Attachments
        travel as structured content, never inside the Task goal, because a goal
        is bounded text and base64 payloads would exceed the Task SPEC limits.

        Ordinary semantic continuation never resumes a source checkpoint in
        place. A validated RESUME proposal is canonicalized into a fresh
        FOLLOW_UP Task with a bounded authority-free handoff. Typed approval,
        clarification and explicit recovery APIs retain their original-Task
        semantics.
        """
        normalized = text.strip()
        if not normalized:
            raise ValueError("session text must not be empty")
        image_blocks = tuple(images)
        # The router reasons about the workspace the message belongs to, so a
        # multi-workspace caller must be able to name it here too.
        resolved_workspace = workspace or self.workspace

        async def execute() -> SessionTextResult:
            decision = await self.application.kernel.dispatch_input_event(
                SessionTextInput(
                    session_id, normalized, command_id, str(resolved_workspace)
                )
            )
            if not isinstance(decision, SessionInputDecision):
                raise RuntimeError("session text dispatch returned an invalid result")
            decision_data = _session_decision_data(decision)
            if decision.disposition is SessionRouteDisposition.ANSWER:
                try:
                    answer = await self.application.kernel.answer_session_message(
                        session_id, normalized, attachments=image_blocks,
                    )
                except SessionAnswerRequiresTask as error:
                    # Routing read one message; the answering model saw the whole
                    # request with no tools in hand and reported the route cannot
                    # serve it. Re-route the same message rather than returning an
                    # apology. CONTEXTUAL carries Session history without
                    # inheriting any prior Task's authority.
                    await self.application.kernel.record_session_route_self_correction(
                        session_id, normalized, error.reason
                    )
                    task = await self.submit_task(
                        normalized, command_id=command_id, session_id=session_id,
                        task_relation=SessionTaskRelation.CONTEXTUAL,
                        images=image_blocks, workspace=resolved_workspace,
                        original_user_text=normalized,
                    )
                    corrected = SessionInputDecision(
                        SessionRouteDisposition.CREATE_TASK,
                        SessionTaskRelation.CONTEXTUAL, None, normalized,
                        decision.confidence,
                        "answer_route_self_corrected_to_task",
                        decision.input_grounding, None,
                        decision.resolver_version, decision.candidates,
                        decision.task_catalog, decision.candidate_task_ids,
                    )
                    return SessionTextResult(
                        command_id, "task", _session_decision_data(corrected),
                        task=task,
                    )
                return SessionTextResult(
                    command_id, "answer", decision_data, answer=answer
                )
            if decision.disposition is SessionRouteDisposition.CLARIFY:
                return SessionTextResult(
                    command_id, "clarify", decision_data,
                    clarification=decision.clarification,
                )
            if decision.disposition is SessionRouteDisposition.RESUME_TASK:
                # Kernel normally canonicalizes ordinary RESUME proposals into
                # CREATE_TASK+FOLLOW_UP. Keep this defensive branch for custom
                # Kernel implementations without ever replaying a source
                # checkpoint from ordinary Session text.
                assert decision.source_task_id is not None
                source = next(
                    item for item in decision.task_catalog
                    if item.task_id == decision.source_task_id
                )
                # The goal is built here rather than taken from the decision. A
                # derived goal must state the user's own request and reference
                # the source Task only as bounded context, and that is a
                # deterministic construction, not something a router may author.
                goal = build_session_follow_up_goal(normalized, source)
                task = await self.submit_task(
                    goal, command_id=command_id, session_id=session_id,
                    source_task_id=decision.source_task_id,
                    task_relation=SessionTaskRelation.FOLLOW_UP,
                    images=image_blocks, workspace=resolved_workspace,
                    original_user_text=normalized,
                )
                derived = SessionInputDecision(
                    SessionRouteDisposition.CREATE_TASK,
                    SessionTaskRelation.FOLLOW_UP,
                    decision.source_task_id, goal, decision.confidence,
                    "ordinary_resume_derived_follow_up",
                    decision.input_grounding, decision.clarification,
                    decision.resolver_version, decision.candidates,
                    decision.task_catalog, decision.candidate_task_ids,
                )
                return SessionTextResult(
                    command_id, "task", _session_decision_data(derived),
                    task=task,
                )
            assert decision.disposition is SessionRouteDisposition.CREATE_TASK
            assert decision.resolved_goal is not None
            task = await self.submit_task(
                decision.resolved_goal,
                command_id=command_id, session_id=session_id,
                source_task_id=decision.source_task_id,
                task_relation=decision.relation,
                images=image_blocks, workspace=resolved_workspace,
                original_user_text=normalized,
            )
            return SessionTextResult(command_id, "task", decision_data, task=task)

        return await self._command(
            command_id, "session_text", {
                "session_id": session_id,
                "text_hash": canonical_hash(normalized),
                "workspace": str(self.workspace),
                "image_count": len(image_blocks),
            }, execute, lambda result: result.to_data(),
        )

    # A relation must be at least this confident to override the safe default.
    _RELATION_CONFIDENCE_FLOOR = 0.85
    _RUNNING_TASK_STATES = frozenset({
        TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW,
    })
    _TRANSITIONAL_TASK_STATES = frozenset({
        TaskState.CREATED, TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.PLANNING,
        TaskState.VERIFYING, TaskState.FINALIZING, TaskState.INTERRUPTING,
        TaskState.RESUMING,
    })
    _INTERRUPTIBLE_TASK_STATES = frozenset({
        TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW,
        TaskState.AWAITING_APPROVAL, TaskState.AWAITING_USER,
    })

    async def submit_user_input(
        self, session_id: str, text: str, *, input_id: str,
        explicit_intent: RuntimeInputIntent | None = None,
        target_task_id: str | None = None,
        images: tuple[ImageBlock, ...] = (),
        workspace: Path | None = None,
    ) -> RuntimeCommandResult:
        """One entry point for every client (Web / CLI / SDK).

        The client only names the Session and, optionally, an explicit intent.
        Core owns the routing decision; clients never pick a router.
        """
        normalized = text.strip()
        if not normalized and explicit_intent is not RuntimeInputIntent.INTERRUPT:
            raise ValueError("user input text must not be empty")
        if not input_id.strip():
            raise ValueError("input_id must not be empty")
        image_blocks = tuple(images)
        resolved_workspace = workspace or self.workspace
        lock = self._session_input_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            return await self._route_user_input(
                session_id, normalized, input_id,
                explicit_intent=explicit_intent,
                target_task_id=target_task_id,
                image_blocks=image_blocks, workspace=resolved_workspace,
            )

    async def _route_user_input(
        self, session_id: str, text: str, input_id: str, *,
        explicit_intent: RuntimeInputIntent | None,
        target_task_id: str | None,
        image_blocks: tuple[ImageBlock, ...],
        workspace: Path,
    ) -> RuntimeCommandResult:
        session = await self._ensure_session(session_id, text)
        active = await self._active_task(target_task_id or session.active_task_id)

        # (0) The foreground Task is between stable states: never start a second
        #     one, and never steer a Task that cannot absorb steering yet.
        if active is not None and active.state in self._TRANSITIONAL_TASK_STATES:
            return RuntimeCommandResult(input_id, False, UserInputResult(
                input_id, "busy", SessionInputRelation.UNKNOWN.value,
                "task_processing_retry",
                clarification=(
                    "当前任务正在处理中，请稍后重试，或使用显式命令。"
                ),
                routed_task_id=active.task_id,
            ).to_data())

        # (1) An explicit client intent always wins and never uses the judge.
        if explicit_intent is not None:
            return await self._apply_explicit_intent(
                session_id, text, input_id, explicit_intent, active,
                image_blocks, workspace,
            )
        # (2) Attachments always start a fresh Task: steering is text-only.
        if image_blocks:
            return await self._delegate_session_text(
                session_id, text, input_id, image_blocks, workspace, None,
            )
        # (3) Waiting for the user: never reinterpret plain text as a new topic.
        if active is not None and active.state in {
            TaskState.AWAITING_APPROVAL, TaskState.AWAITING_USER,
        }:
            return RuntimeCommandResult(input_id, False, UserInputResult(
                input_id, "clarify", SessionInputRelation.UNKNOWN.value,
                "task_awaits_user_action",
                clarification=(
                    "当前任务正在等待审批或回答，请通过对应通道回复，"
                    "或使用显式命令（/new、/stop）。"
                ),
                routed_task_id=active.task_id,
            ).to_data())
        # (4) Running Task: one general judge decides the relation.
        if active is not None and active.state in self._RUNNING_TASK_STATES:
            judgement = await self._judge_relation(text, session_id, active)
            relation = judgement.relation
            reason = judgement.reason_code
            if (
                relation is not SessionInputRelation.SUPPLEMENT
                and judgement.confidence < self._RELATION_CONFIDENCE_FLOOR
            ):
                relation = SessionInputRelation.SUPPLEMENT
                reason = "below_confidence_floor_supplement"
            if relation is SessionInputRelation.SUPPLEMENT:
                return await self._steer_active(
                    text, input_id, active, relation, reason,
                    RuntimeInputIntent.STEER,
                )
            if relation is SessionInputRelation.REPLACE:
                return await self._steer_active(
                    text, input_id, active, relation, reason,
                    RuntimeInputIntent.REPLACE,
                )
            if relation is SessionInputRelation.UNRELATED:
                await self._stop_active_task(active.task_id, input_id)
            return await self._delegate_session_text(
                session_id, text, input_id, image_blocks, workspace, relation,
            )
        # (5) No running Task: existing Session routing creates/answers/clarifies.
        return await self._delegate_session_text(
            session_id, text, input_id, image_blocks, workspace, None,
        )

    async def _ensure_session(self, session_id: str, text: str) -> SessionSnapshot:
        kernel = self.application.kernel
        try:
            return await kernel.get_session(session_id)
        except LookupError:
            return await kernel.create_session(text[:120], session_id=session_id)

    async def _active_task(self, task_id: str | None) -> TaskSnapshot | None:
        if not task_id:
            return None
        try:
            return await self.application.kernel.get_task(task_id)
        except LookupError:
            return None

    async def _judge_relation(
        self, text: str, session_id: str, active: TaskSnapshot,
    ) -> SessionInputRelationJudgement:
        judges = self.application.registry.all(SessionInputRelationPort)
        if not judges:
            return SessionInputRelationJudgement(
                SessionInputRelation.SUPPLEMENT, 0.0,
                "no_relation_judge_registered_supplement",
            )
        try:
            conversation = await self.application.kernel.get_session_conversation(
                session_id
            )
            recent = [
                {
                    "role": message.role.value, "text": message.text,
                    "task_id": message.task_id,
                }
                for message in conversation.messages[-12:]
            ]
        except (LookupError, RuntimeError, ValueError):
            recent = []
        context = {
            "session_id": session_id,
            "recent_messages": recent,
            "active_task": {
                "task_id": active.task_id, "state": active.state.value,
                "goal": active.goal,
            },
        }
        try:
            return await judges[0].judge_input_relation(text, context)
        except Exception:
            # A judge failure must not change the Task: fall back to supplement.
            return SessionInputRelationJudgement(
                SessionInputRelation.SUPPLEMENT, 0.0,
                "relation_judge_failed_supplement",
            )

    async def _steer_active(
        self, text: str, input_id: str, active: TaskSnapshot,
        relation: SessionInputRelation, reason: str,
        explicit_intent: RuntimeInputIntent,
    ) -> RuntimeCommandResult:
        # Delegate to the Kernel's live-input router so steering, approval
        # supersession and idempotency are identical across Web, CLI and SDK.
        route = await self.application.kernel.dispatch_input_event(
            RuntimeTextInput(active.task_id, text, input_id, explicit_intent)
        )
        task = await self.get_task_result(active.task_id)
        return RuntimeCommandResult(input_id, False, {
            "kind": (
                "replaced" if relation is SessionInputRelation.REPLACE
                else "steered"
            ),
            "relation": relation.value,
            "reason_code": getattr(route, "reason_code", reason),
            "applied": getattr(route, "applied", True),
            "routed_task_id": active.task_id,
            "task": task.to_data(),
        })

    async def _stop_active_task(self, task_id: str, input_id: str) -> None:
        task = await self._active_task(task_id)
        if task is None or task.state not in self._INTERRUPTIBLE_TASK_STATES:
            return
        try:
            await self.interrupt(
                task_id, command_id=f"{input_id}:stop",
                reason="superseded by new user input",
            )
        except (RuntimeError, ValueError):
            # Best effort: failing to stop the old Task must not block the new.
            pass

    async def _delegate_session_text(
        self, session_id: str, text: str, input_id: str,
        image_blocks: tuple[ImageBlock, ...], workspace: Path,
        relation: SessionInputRelation | None,
    ) -> RuntimeCommandResult:
        result = await self.submit_session_text(
            session_id, text, command_id=input_id, images=image_blocks,
            workspace=workspace,
        )
        if relation is None:
            return result
        data = dict(result.result)
        data["relation"] = relation.value
        return RuntimeCommandResult(result.command_id, result.replayed, data)

    async def _apply_explicit_intent(
        self, session_id: str, text: str, input_id: str,
        intent: RuntimeInputIntent, active: TaskSnapshot | None,
        image_blocks: tuple[ImageBlock, ...], workspace: Path,
    ) -> RuntimeCommandResult:
        if intent is RuntimeInputIntent.INTERRUPT:
            if active is None:
                raise ValueError("interrupt requires an active Task")
            inner = await self.interrupt(
                active.task_id, command_id=input_id,
                reason="interrupted by explicit user command",
            )
            data = dict(inner.result)
            data.update({
                "kind": "interrupted", "relation": "EXPLICIT",
                "reason_code": "explicit_interrupt",
                "routed_task_id": active.task_id,
            })
            return RuntimeCommandResult(inner.command_id, inner.replayed, data)
        if intent is RuntimeInputIntent.NEW_TASK:
            if active is not None:
                await self._stop_active_task(active.task_id, input_id)
            return await self._delegate_session_text(
                session_id, text, input_id, image_blocks, workspace, None,
            )
        if active is not None and active.state in self._RUNNING_TASK_STATES:
            if intent is RuntimeInputIntent.STEER:
                return await self._steer_active(
                    text, input_id, active,
                    SessionInputRelation.SUPPLEMENT, "explicit_steer",
                    RuntimeInputIntent.STEER,
                )
            if intent is RuntimeInputIntent.REPLACE:
                return await self._steer_active(
                    text, input_id, active,
                    SessionInputRelation.REPLACE, "explicit_replace",
                    RuntimeInputIntent.REPLACE,
                )
        return await self._delegate_session_text(
            session_id, text, input_id, image_blocks, workspace, None,
        )

    async def _run_submitted_task(
        self, task_id: str, goal: str, *, images: tuple[ImageBlock, ...] = (),
    ) -> RuntimeTaskResult:
        try:
            return await self.run_task(task_id, goal, images=images)
        except asyncio.CancelledError:
            raise
        except Exception:
            current = await self.application.kernel.get_task(task_id)
            if not current.state.is_terminal and current.state not in {
                TaskState.AWAITING_APPROVAL, TaskState.AWAITING_USER,
            }:
                try:
                    await self.application.kernel.transition_task(
                        task_id, TaskState.FAILED,
                        "SDK background execution failed",
                    )
                except (RuntimeError, ValueError):
                    pass
            result = await self.get_task_result(task_id)
            self._latest_results[task_id] = result
            return result

    async def wait_task(
        self, task_id: str, timeout: float | None = None,
    ) -> RuntimeTaskResult:
        runner = self._run_tasks.get(task_id)
        if runner is not None:
            return await asyncio.wait_for(asyncio.shield(runner), timeout)
        return await self.get_task_result(task_id)

    async def run_task(
        self, task_id: str, user_text: str, *,
        images: tuple[ImageBlock, ...] = (),
    ) -> RuntimeTaskResult:
        kernel = self.application.kernel
        task = await kernel.get_task(task_id)
        if task.state is TaskState.CREATED:
            for target in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await kernel.transition_task(
                    task.task_id, target, f"SDK {target.value.lower()}"
                )
            result = await kernel.run_agent_turn(
                task_id, user_text,
                user_blocks=(
                    (TextBlock(user_text), *images) if images else None
                ),
                on_progress=lambda item: self._record_progress(task_id, item),
            )
        elif task.state in {TaskState.INTERRUPTED, TaskState.CONFLICT}:
            result = await kernel.resume_checkpointed_agent_turn(
                task_id,
                on_progress=lambda item: self._record_progress(task_id, item),
            )
        elif (
            task.state is TaskState.AWAITING_USER
            and task.pending_clarification is None
            and isinstance(task.active_agent_checkpoint, Mapping)
            and isinstance(
                task.active_agent_checkpoint.get("pending_user_action"),
                Mapping,
            )
            and task.active_agent_checkpoint["pending_user_action"].get(
                "kind"
            ) == "CONTINUATION"
        ):
            result = await kernel.resume_agent_continuation(
                task_id, user_text, input_id=f"sdk-{uuid4().hex}",
                on_progress=lambda item: self._record_progress(task_id, item),
            )
        else:
            return await self.get_task_result(task_id)
        return await self._finish_agent_result(result)

    async def _persisted_assistant_text(
        self, task: TaskSnapshot,
    ) -> str | None:
        """Read only a durable *user-facing* Task result after a restart.

        Every ``llm.completed`` event is not a response for the user: an Agent
        can emit a provisional summary, fail readiness, then continue with more
        tools. The Session result event is the durable boundary that says a
        message was actually handed to the user, so it is the only safe fallback
        once the live ``_latest_results`` cache is gone.
        """
        session_events = await self._store().read_session_events(task.session_id)
        for event in reversed(session_events):
            if (
                event.event_type != "session.task_result_recorded"
                or str(event.payload.get("task_id") or "") != task.task_id
            ):
                continue
            raw_message = event.payload.get("assistant_message")
            if not isinstance(raw_message, Mapping):
                continue
            raw_content = raw_message.get("content")
            if not isinstance(raw_content, list):
                continue
            text = "\n".join(
                str(block.get("text", ""))
                for block in raw_content
                if isinstance(block, Mapping) and block.get("type") == "text"
            ).strip()
            if text:
                return text
        return None

    async def get_task_result(self, task_id: str) -> RuntimeTaskResult:
        task = await self.application.kernel.get_task(task_id)
        stored = await self._store().load_task(task_id)
        assert stored is not None
        events = await self._store().read_events(task_id)
        projected = TaskRuntimeProjector.project(task, events)
        projection = projected.to_data()
        cached = self._latest_results.get(task_id)
        assistant_text = (
            cached.assistant_text
            if cached is not None and cached.assistant_text is not None
            else await self._persisted_assistant_text(task)
        )
        return RuntimeTaskResult(
            task_id, task.state.value, task.state.phase1_state.value,
            _status_for_state(task.state), stored.last_event_sequence,
            assistant_text=assistant_text,
            approval=_approval_data(task),
            clarification=(cached.clarification if cached else _clarification_data(task)),
            verification=cached.verification if cached else None,
            evidence_level=cached.evidence_level if cached else None,
            projection=projection,
            latest_answer_event_ref=projected.latest_answer_event_ref,
            conclusion_claims=projected.conclusion_claims,
            conclusion_validation=projected.conclusion_validation,
            completion_diagnostics=_completion_diagnostics(events),
        )

    async def read_events(
        self, task_id: str, *, after: int = 0, limit: int = 200,
    ) -> tuple[RuntimeEventEnvelope, ...]:
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("after must be non-negative and limit must be 1..1000")
        await self.application.kernel.get_task(task_id)
        events = (await self._store().read_events(task_id, after))[:limit]
        return tuple(RuntimeEventEnvelope(
            event.event_id, event.task_id, event.sequence, event.event_type,
            event.occurred_at.isoformat(), event.schema_version,
        ) for event in events)

    async def get_investigation_status(
        self, task_id: str,
    ) -> Mapping[str, Any]:
        """Read one redacted status projection without executing Agent work."""
        return (
            await self.application.kernel.get_investigation_status(task_id)
        ).to_data()

    async def subscribe_events(
        self, task_id: str, *, after: int = 0, timeout: float | None = None,
    ) -> AsyncIterator[RuntimeEventEnvelope]:
        if after < 0 or (timeout is not None and timeout <= 0):
            raise ValueError("invalid event cursor or timeout")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None
        cursor = after
        while True:
            events = await self.read_events(task_id, after=cursor, limit=1000)
            for event in events:
                cursor = event.cursor
                yield event
            task = await self.application.kernel.get_task(task_id)
            if task.state.is_terminal and not events:
                return
            if deadline is not None and loop.time() >= deadline:
                return
            await asyncio.sleep(self._poll_interval)

    def latest_progress_sequence(self, task_id: str) -> int:
        """Return the cursor for the ephemeral progress stream namespace."""
        return self._progress_sequences.get(task_id, 0)

    def read_progress(
        self, task_id: str, *, after: int = 0, limit: int = 200,
    ) -> tuple[RuntimeProgressEnvelope, ...]:
        """Read in-memory live progress; concrete values are not persisted."""
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("after must be non-negative and limit must be 1..1000")
        return tuple(
            item for item in self._progress_history.get(task_id, ())
            if item.sequence > after
        )[:limit]

    async def subscribe_progress(
        self, task_id: str, *, after: int = 0, timeout: float | None = None,
    ) -> AsyncIterator[RuntimeProgressEnvelope]:
        """Stream concrete, ephemeral Kernel progress to a local Web UI."""
        if after < 0 or (timeout is not None and timeout <= 0):
            raise ValueError("invalid progress cursor or timeout")
        await self.application.kernel.get_task(task_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None
        cursor = after
        while True:
            items = self.read_progress(task_id, after=cursor, limit=1000)
            for item in items:
                cursor = item.sequence
                yield item
            task = await self.application.kernel.get_task(task_id)
            runner = self._run_tasks.get(task_id)
            running = runner is not None and not runner.done()
            if task.state.is_terminal and not running and not items:
                return
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return
            wait_for = self._poll_interval if remaining is None else min(
                self._poll_interval, remaining
            )
            async with self._progress_changed:
                try:
                    await asyncio.wait_for(
                        self._progress_changed.wait(), timeout=wait_for
                    )
                except TimeoutError:
                    pass

    def _record_progress(self, task_id: str, progress: AgentProgress) -> None:
        history = self._progress_history.setdefault(task_id, [])
        sequence = self._progress_sequences.get(task_id, 0) + 1
        self._progress_sequences[task_id] = sequence
        history.append(RuntimeProgressEnvelope(task_id, sequence, progress))
        if len(history) > 2000:
            del history[:-2000]
        # Wake subscribers without making progress rendering affect execution.
        async def notify() -> None:
            async with self._progress_changed:
                self._progress_changed.notify_all()
        asyncio.create_task(notify())

    async def interrupt(
        self, task_id: str, *, command_id: str, reason: str,
    ) -> RuntimeCommandResult:
        async def execute():
            task = await self.application.kernel.dispatch_input_event(
                InterruptTaskInput(task_id, reason)
            )
            runner = self._run_tasks.get(task_id)
            if runner is not None and not runner.done():
                runner.cancel()
            return task
        return await self._command(
            command_id, "interrupt", {"task_id": task_id, "reason": reason},
            execute,
            lambda task: {"task_id": task.task_id, "state": task.state.value},
        )

    async def steer(
        self, task_id: str, text: str, *, command_id: str,
    ) -> RuntimeCommandResult:
        return await self._queue_steering(
            task_id, SteeringKind.STEER, text, command_id
        )

    async def replace(
        self, task_id: str, text: str, *, command_id: str,
    ) -> RuntimeCommandResult:
        return await self._queue_steering(
            task_id, SteeringKind.REPLACE, text, command_id
        )

    async def get_task_spec(self, task_id: str) -> Mapping[str, Any]:
        return (await self.application.kernel.get_task_spec(task_id)).to_data()

    async def revise_task_spec(
        self, task_id: str, *, expected_revision: int,
        scope: tuple[str, ...], constraints: tuple[str, ...],
        acceptance_criteria: tuple[Mapping[str, Any], ...],
        command_id: str,
    ) -> RuntimeCommandResult:
        from tsm_agt.core import TaskAcceptanceCriterion
        parsed = tuple(
            TaskAcceptanceCriterion.from_data(item)
            for item in acceptance_criteria
        )

        async def execute():
            return await self.application.kernel.revise_task_spec(
                task_id, expected_revision, scope=scope, constraints=constraints,
                acceptance_criteria=parsed, operation_id=command_id, writer="sdk",
            )

        return await self._command(
            command_id, "task_spec_update", {
                "task_id": task_id, "expected_revision": expected_revision,
                "scope": list(scope), "constraints": list(constraints),
                "criteria_hash": canonical_hash([item.to_data() for item in parsed]),
            }, execute, lambda spec: spec.to_data(),
        )

    async def route_input(
        self, task_id: str, text: str, *, command_id: str,
        intent: str | None = None,
    ) -> RuntimeCommandResult:
        """Apply input to an active Task using a protocol intent.

        ``intent`` may explicitly select steer, replace, or
        new_task_after_current. Omitting it deterministically means STEER;
        Runtime never infers control intent from the user's wording.
        """
        explicit = None
        if intent is not None:
            try:
                explicit = RuntimeInputIntent(intent.strip().upper())
            except ValueError as error:
                raise ValueError(
                    "intent must be steer, replace, or new_task_after_current"
                ) from error

        async def execute():
            return await self.application.kernel.dispatch_input_event(
                RuntimeTextInput(
                    task_id, text, command_id, explicit,
                    RuntimeInputIntent.STEER if explicit is None else None,
                )
            )

        return await self._command(
            command_id, "runtime_input", {
                "task_id": task_id,
                "text_hash": canonical_hash(text.strip()),
                "intent": explicit.value if explicit else None,
            }, execute, lambda route: {
                "task_id": task_id, **route.to_data(),
            },
        )

    async def _queue_steering(
        self, task_id: str, kind: SteeringKind, text: str, command_id: str,
    ) -> RuntimeCommandResult:
        async def execute():
            return await self.application.kernel.queue_steering(
                task_id, kind, text, command_id
            )
        return await self._command(
            command_id, kind.value, {
                "task_id": task_id, "text_hash": canonical_hash(text.strip()),
            }, execute, lambda projection: {
                "task_id": projection.task_id,
                "steering_revision": projection.revision,
                "inbound_sequence": projection.latest_inbound_sequence,
                "pending_count": len(projection.pending),
                "kind": kind.value,
            },
        )

    async def resolve_approval(
        self, request_id: str, decision: ApprovalDecision, reason: str, *,
        command_id: str,
    ) -> RuntimeCommandResult:
        async def execute():
            stored = await self._store().find_task_by_pending_approval(request_id)
            task_id = (
                TaskSnapshot.from_data(stored.data).task_id if stored is not None
                else ""
            )
            result = await self.application.kernel.dispatch_input_event(
                ApprovalResolutionInput(request_id, decision, reason),
                on_progress=(
                    (lambda item: self._record_progress(task_id, item))
                    if task_id else None
                ),
            )
            return await self._finish_agent_result(result)
        return await self._command(
            command_id, "approval", {
                "request_id": request_id, "decision": decision.value,
                "reason": reason,
            }, execute, lambda result: result.to_data(),
        )

    async def answer_clarification(
        self, request_id: str, resume_token: str, answer: str | None = None, *,
        selected_choice: str | None = None,
        command_id: str,
    ) -> RuntimeCommandResult:
        async def execute():
            stored = await self._store().find_task_by_pending_clarification(
                request_id
            )
            task_id = (
                TaskSnapshot.from_data(stored.data).task_id if stored is not None
                else ""
            )
            result = await self.application.kernel.dispatch_input_event(
                ClarificationReplyInput(
                    request_id, resume_token, answer, selected_choice
                ),
                on_progress=(
                    (lambda item: self._record_progress(task_id, item))
                    if task_id else None
                ),
            )
            return await self._finish_agent_result(result)
        return await self._command(
            command_id, "clarification", {
                "request_id": request_id, "resume_token_hash": canonical_hash(
                    resume_token
                ),
                "answer_hash": (
                    canonical_hash(answer) if answer is not None else None
                ),
                "selected_choice": selected_choice,
            }, execute, lambda result: result.to_data(),
        )

    async def _finish_agent_result(self, result: object) -> RuntimeTaskResult:
        if isinstance(result, AgentTurnSuspended):
            task_result = await self.get_task_result(result.task_id)
        elif isinstance(result, AgentClarificationSuspended):
            base = await self.get_task_result(result.task_id)
            task_result = replace(
                base,
                status="awaiting_user",
                clarification={
                    "request_id": result.request_id,
                    "question": result.question, "reason": result.reason,
                    "kind": result.kind,
                    "required": result.required,
                    "input_mode": result.input_mode,
                    "choices": [
                        {"value": value, "label": label}
                        for value, label in result.choices
                    ],
                    "resume_token": result.resume_token,
                },
            )
        elif isinstance(result, AgentContinuationSuspended):
            base = await self.get_task_result(result.task_id)
            task_result = replace(
                base,
                status="awaiting_user",
                assistant_text=result.assistant_message.text,
                clarification={
                    "kind": "CONTINUATION",
                    "completed_outcome_ids": list(
                        result.completed_outcome_ids
                    ),
                    "remaining_outcome_ids": list(
                        result.remaining_outcome_ids
                    ),
                },
            )
        elif isinstance(result, AgentTurnResult):
            kernel = self.application.kernel
            if kernel.completion_readiness_mode is CompletionReadinessMode.LEGACY_GATE:
                await kernel.transition_task(
                    result.task_id, TaskState.VERIFYING, "SDK verifier started"
                )
                verification = await kernel.verify_task_acceptance(result.task_id)
                if verification.passed:
                    await kernel.transition_task(
                        result.task_id, TaskState.FINALIZING, "SDK finalizing"
                    )
                    await kernel.transition_task(
                        result.task_id, TaskState.SUCCEEDED, "SDK succeeded"
                    )
                else:
                    await kernel.transition_task(
                        result.task_id, TaskState.FAILED,
                        f"SDK verifier {verification.status.value}",
                    )
            else:
                # Diagnostics remain observable, but only an explicit caller
                # decision may advance or fail the Task outside legacy gating.
                verification = await kernel.verify_task_acceptance(result.task_id)
            base = await self.get_task_result(result.task_id)
            task_result = replace(
                base,
                status=_status_for_state(TaskState(base.state)),
                assistant_text=result.assistant_message.text,
                verification={
                    "status": verification.status.value,
                    "passed": verification.passed,
                    "criteria_count": len(verification.criteria),
                    "criteria": [
                        {
                            "criterion_id": item.criterion_id,
                            "status": item.status.value,
                        }
                        for item in verification.criteria
                    ],
                },
                evidence_level=(
                    verification.evidence_level.to_data()
                    if verification.evidence_level else None
                ),
            )
        else:
            raise TypeError(f"unsupported Agent result: {type(result).__name__}")
        self._latest_results[task_result.task_id] = task_result
        return task_result

    async def _command(
        self, command_id: str, command_type: str, request: Mapping[str, Any],
        execute: Callable[[], Any], serialize: Callable[[Any], Mapping[str, Any]],
    ) -> RuntimeCommandResult:
        normalized_id = command_id.strip()
        if not normalized_id:
            raise ValueError("command_id must not be empty")
        request_hash = canonical_hash({
            "type": command_type, "request": dict(request)
        })
        now = datetime.now(timezone.utc)
        claim_token = uuid4().hex
        claimed = await self._store().claim_runtime_command(RuntimeCommandRecord(
            normalized_id, command_type, request_hash, "in_progress",
            {"claim_token": claim_token}, now, now,
        ))

        if claimed.status == "completed":
            return RuntimeCommandResult(normalized_id, True, claimed.result)
        if claimed.result.get("claim_token") != claim_token:
            raise CommandInProgress(
                f"Runtime command is already in progress: {normalized_id}"
            )
        value = execute()
        if asyncio.iscoroutine(value):
            value = await value
        encoded = dict(serialize(value))
        persisted = _redact_command_result(encoded)
        completed = await self._store().complete_runtime_command(
            normalized_id, request_hash, persisted
        )
        return RuntimeCommandResult(normalized_id, False, encoded)

    def _store(self) -> RuntimeStorePort:
        return self.application.registry.require(RuntimeStorePort)


def _session_decision_data(decision: SessionInputDecision) -> dict[str, Any]:
    return {
        "disposition": decision.disposition.value,
        "relation": decision.relation.value,
        "source_task_id": decision.source_task_id,
        "resolved_goal": decision.resolved_goal,
        "confidence": decision.confidence,
        "reason_code": decision.reason_code,
        "clarification": decision.clarification,
        "candidate_task_ids": list(decision.candidate_task_ids),
    }


def _completion_diagnostics(
    events: tuple[Any, ...] | list[Any],
) -> Mapping[str, Any] | None:
    for event in reversed(events):
        if event.event_type == "completion.readiness_evaluated":
            return dict(event.payload)
    return None


def _status_for_state(state: TaskState) -> str:
    if state is TaskState.AWAITING_APPROVAL:
        return "awaiting_approval"
    if state is TaskState.AWAITING_USER:
        return "awaiting_user"
    if state is TaskState.INTERRUPTED:
        return "interrupted"
    if state.is_terminal:
        return "completed" if state is TaskState.SUCCEEDED else "failed"
    return "running"


def _approval_data(task: TaskSnapshot) -> Mapping[str, Any] | None:
    request = task.pending_approval
    if request is None:
        return None
    return {
        "request_id": request.request_id, "risk": request.risk.value,
        "action": request.action, "target": request.target,
        "preview": request.preview, "network_access": request.network_access,
        "data_transmission": request.data_transmission,
        "rollback": request.rollback,
        "approval_kind": request.kind.value,
    }


def _clarification_data(task: TaskSnapshot) -> Mapping[str, Any] | None:
    request = task.pending_clarification
    if request is None:
        return None
    return {
        "request_id": request.request_id, "question": request.question,
        "reason": request.reason, "required": request.required,
        "kind": request.kind.value,
        "input_mode": request.input_mode,
        "choices": [choice.to_data() for choice in request.choices],
        "expires_at": request.expires_at.isoformat(),
        "resume_token": None,
    }


def _redact_command_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Never persist one-time resume credentials in command receipts."""

    def scrub(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {
                str(key): (None if str(key) == "resume_token" else scrub(child))
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [scrub(child) for child in item]
        return item

    result = scrub(value)
    assert isinstance(result, dict)
    return result
