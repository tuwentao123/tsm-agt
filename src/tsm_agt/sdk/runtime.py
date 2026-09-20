"""One Runtime command/event facade shared by SDK and local transports."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
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
    SessionInputDecision, SessionRouteDisposition, SessionTaskRelation,
    TaskSnapshot, TaskState, canonical_hash, SteeringKind, RuntimeInputIntent,
)
from tsm_agt.ports import (
    ImageBlock, RuntimeCommandRecord, RuntimeStorePort, TextBlock,
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
    ) -> TaskSnapshot:
        return await self.application.kernel.create_task(
            goal, self.workspace, session_id=session_id, command_id=command_id,
            source_task_id=source_task_id, task_relation=task_relation,
        )

    async def submit_task(
        self, goal: str, *, command_id: str, session_id: str | None = None,
        source_task_id: str | None = None,
        task_relation: SessionTaskRelation = SessionTaskRelation.INDEPENDENT,
        images: tuple[ImageBlock, ...] = (),
    ) -> RuntimeTaskResult:
        async with self._submit_lock:
            task = await self.create_task(
                goal, command_id=command_id, session_id=session_id,
                source_task_id=source_task_id, task_relation=task_relation,
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
        images: tuple[Mapping[str, str], ...] = (),
    ) -> RuntimeCommandResult:
        """Route one ordinary message and execute its validated Session action.

        ``command_id`` is the caller's stable per-message request id. It is
        also passed to ``create_task`` when a Task is created, preserving the
        Kernel's stricter Task command receipt rather than bypassing it.

        ``images`` carries inline data-URL attachments. They travel as
        structured ``ImageBlock`` content, never inside the Task goal: a goal is
        bounded text and base64 payloads would exceed the Task SPEC limits.
        """
        normalized = text.strip()
        if not normalized:
            raise ValueError("session text must not be empty")
        image_blocks = tuple(
            ImageBlock(image_url=str(image["image_url"]))
            for image in images
            if str(image.get("image_url", "")).startswith("data:image/")
        )

        async def execute() -> SessionTextResult:
            decision = await self.application.kernel.dispatch_input_event(
                SessionTextInput(session_id, normalized, command_id, str(self.workspace))
            )
            if not isinstance(decision, SessionInputDecision):
                raise RuntimeError("session text dispatch returned an invalid result")
            decision_data = _session_decision_data(decision)
            if decision.disposition is SessionRouteDisposition.ANSWER:
                answer = await self.application.kernel.answer_session_message(
                    session_id, normalized
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
                assert decision.source_task_id is not None
                task = await self.run_task(
                    decision.source_task_id, normalized, images=image_blocks
                )
                return SessionTextResult(command_id, "task", decision_data, task=task)
            assert decision.disposition is SessionRouteDisposition.CREATE_TASK
            task = await self.submit_task(
                decision.resolved_goal or normalized,
                command_id=command_id, session_id=session_id,
                source_task_id=decision.source_task_id,
                task_relation=decision.relation,
                images=image_blocks,
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

    async def get_task_result(self, task_id: str) -> RuntimeTaskResult:
        task = await self.application.kernel.get_task(task_id)
        stored = await self._store().load_task(task_id)
        assert stored is not None
        cached = self._latest_results.get(task_id)
        return RuntimeTaskResult(
            task_id, task.state.value, task.state.phase1_state.value,
            _status_for_state(task.state), stored.last_event_sequence,
            assistant_text=cached.assistant_text if cached else None,
            approval=_approval_data(task),
            clarification=(cached.clarification if cached else _clarification_data(task)),
            verification=cached.verification if cached else None,
            evidence_level=cached.evidence_level if cached else None,
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
            task_result = RuntimeTaskResult(
                base.task_id, base.state, base.phase1_state, "awaiting_user",
                base.cursor,
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
            task_result = RuntimeTaskResult(
                base.task_id, base.state, base.phase1_state, "awaiting_user",
                base.cursor,
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
            base = await self.get_task_result(result.task_id)
            task_result = RuntimeTaskResult(
                base.task_id, base.state, base.phase1_state,
                _status_for_state(TaskState(base.state)),
                base.cursor, assistant_text=result.assistant_message.text,
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
