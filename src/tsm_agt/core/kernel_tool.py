"""Tool invocation and batch lifecycle for ``Kernel``.

A mixin module in the facade-plus-siblings shape (``run_agent.py`` +
``agent/session_persistence.py``): the facade ``core/kernel.py`` imports
this at module level and adds ``ToolMixin`` to ``Kernel``'s bases, so
**every method resolves through ``Kernel``'s MRO unchanged** --
``self._dependencies``, ``self._append_events`` and ``Kernel``'s own class
attributes all still resolve. Moved bodies are verbatim: nothing is renamed
and no signature changes.

This module must never bind the facade at module level; late-import it
inside a method instead (the project's module-boundary rule).

Moved from ``core/kernel.py``:

* ``_bind_tool_call_outcome``
* ``_decide_legacy_tool_call_outcome``
* ``_decide_tool_call_outcome``
* ``_execute_authorized_tool``
* ``_find_tool``
* ``_invoke_agent_tool``
* ``_latest_recoverable_tool_batch``
* ``_looks_like_exact_text_tool_call``
* ``_new_tool_batch``
* ``_present_tool_arguments``
* ``_record_tool_result_reused``
* ``_recover_interrupted_tool``
* ``_resume_safe_tool``
* ``_suspend_unknown_tool_outcome``
* ``_synchronize_tool_batch``
* ``_tool_effect_supports_outcome``
* ``_validate_tool_batch_proposal``
* ``invoke_tool``
* ``list_tools``
* ``recover_tool_execution``

Module-level symbols that moved with them, transitively. They had no
user outside the moved set, and this module must not import the facade
to reach them, so their home is here now:

* ``_TaskMemoryControl``
* ``_TaskProcessControl``
* ``_TaskSpecControl``
* ``_TaskWorkingMemoryControl``
* ``_TaskWorkspaceControl``
* ``_render_workspace_patch``
"""

from __future__ import annotations


import asyncio
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tsm_agt.ports import (
    BackgroundProcessStartResult, Message, MessageRole,
    OutcomeBindingMode, RecoverableToolProtocolError, RuntimeEvent,
    RuntimeUnitOfWork, ToolArgumentPresentation, ToolCall, ToolCallBlock,
    ToolEffect, ToolIdempotency, ToolInvocationContext,
    ToolMemoryControl, ToolProcessControl, ToolProviderPort,
    ToolRecoveryKind, ToolResult, ToolResultBlock, ToolRisk,
    ToolSpec, ToolTaskSpecControl,
    ToolWorkingMemoryControl, ToolWorkspaceControl,
)
from uuid import uuid4
from .agent_loop import (
    AgentClarificationSuspended, AgentTurnCheckpoint, ToolBatchSnapshot,
    ToolBatchStatus
)
from .approval import (
    ApprovalKind, ApprovalRequest, ApprovalRequired, utc_now
)
from .clarification import (
    ClarificationChoice, ClarificationKind, ClarificationRequest
)
from .configuration import canonical_hash
from .execution import (
    IdempotencyConflict, ToolCommitState, ToolExecutionInProgress,
    ToolExecutionRecord
)
from .memory import MemoryScope, MemorySourceKind
from .task import TaskNotFound, TaskSnapshot, TaskState
from .task_spec import (
    OutcomeBindingAction, OutcomeBindingDecision, OutcomeBindingReason,
    TaskAcceptanceCriterion, TaskOutcomeEligibilityCalculator,
    TaskOutcomeKind, TaskOutcomeStatus
)
from .tool import (
    DuplicateToolName, InvalidToolArguments, InvalidToolResult,
    ToolNotFound, validate_tool_arguments
)
from .turn import InvalidModelResponse, InvalidTurnState
from .verification_command import is_verification_request
from .workspace import WorkspacePatchConflict, prepare_workspace_text_write


def _render_workspace_patch(
    path: str, before_bytes: bytes | None,
    edits: tuple[Mapping[str, str], ...],
) -> str:
    if before_bytes is None:
        if len(edits) != 1 or edits[0]["old_text"] != "":
            raise ValueError(
                "creating a file requires exactly one edit with empty old_text"
            )
        return edits[0]["new_text"]
    try:
        content = before_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("core.apply_patch supports UTF-8 text files only") from error
    for index, edit in enumerate(edits):
        old_text = edit["old_text"]
        if not old_text:
            # The caller believed the file was absent (or did not read it), but
            # it exists. Re-reading is the fix, not abandonment.
            raise WorkspacePatchConflict(
                path, index, "old_text must not be empty for an existing file"
            )
        matches = content.count(old_text)
        if matches == 0:
            if "\\n" in old_text and "\n" not in old_text:
                literal_newline_candidate = old_text.replace("\\n", "\n")
                if content.count(literal_newline_candidate) == 1:
                    raise WorkspacePatchConflict(
                        path,
                        index,
                        "old_text contains literal \\n; use actual newline characters",
                    )
            raise WorkspacePatchConflict(
                path, index, "old_text was not found in the current file"
            )
        if matches > 1:
            raise WorkspacePatchConflict(
                path, index, f"old_text is ambiguous: found {matches} matches"
            )
        content = content.replace(old_text, edit["new_text"], 1)
    return content


@dataclass(frozen=True, slots=True)
class _TaskProcessControl(ToolProcessControl):
    """Narrow capability: process tools can act only on one current Task."""

    kernel: Kernel
    task_id: str
    turn_id: str
    invocation_id: str

    async def run(
        self, argv: tuple[str, ...], *, cwd: str,
        environment: Mapping[str, str], background: bool,
        timeout_seconds: float, termination_grace_seconds: float,
        max_output_bytes: int, max_lifetime_seconds: float,
        stop_on_task_end: bool,
    ) -> ProcessCommandResult | BackgroundProcessStartResult:
        if background:
            record = await self.kernel.start_background_process(
                self.task_id, self.turn_id, argv, cwd=cwd,
                environment=environment,
                max_lifetime_seconds=max_lifetime_seconds,
                max_output_bytes=max_output_bytes,
                stop_on_task_end=stop_on_task_end,
                invocation_id=self.invocation_id,
            )
            return BackgroundProcessStartResult(
                process_id=record.process_id,
                state=record.state.value,
                started_at=record.handle.started_at,
                deadline_at=record.deadline_at,
                max_lifetime_seconds=record.max_lifetime_seconds,
                stop_on_task_end=record.stop_on_task_end,
            )
        return await self.kernel.run_foreground_process(
            self.task_id, self.turn_id, argv, cwd=cwd,
            environment=environment, timeout_seconds=timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            max_output_bytes=max_output_bytes,
            invocation_id=self.invocation_id,
        )

    async def status(self, process_id: str) -> Mapping[str, Any]:
        record = await self.kernel.get_background_process_status(
            self.task_id, process_id
        )
        return self.kernel._background_record_data(record)

    async def logs(
        self, process_id: str, stdout_cursor: int, stderr_cursor: int
    ) -> Mapping[str, Any]:
        logs = await self.kernel.read_background_process_logs(
            self.task_id, process_id,
            stdout_cursor=stdout_cursor, stderr_cursor=stderr_cursor,
        )
        return {
            "process_id": process_id,
            "stdout": {
                "text": logs.stdout.text, "cursor": logs.stdout.cursor,
                "next_cursor": logs.stdout.next_cursor,
                "total_bytes": logs.stdout.total_bytes,
                "truncated": logs.stdout.truncated,
            },
            "stderr": {
                "text": logs.stderr.text, "cursor": logs.stderr.cursor,
                "next_cursor": logs.stderr.next_cursor,
                "total_bytes": logs.stderr.total_bytes,
                "truncated": logs.stderr.truncated,
            },
        }

    async def stop(
        self, process_id: str, grace_seconds: float
    ) -> Mapping[str, Any]:
        record = await self.kernel.stop_background_process(
            self.task_id, process_id, grace_seconds=grace_seconds
        )
        return self.kernel._background_record_data(record)


@dataclass(frozen=True, slots=True)
class _TaskWorkspaceControl(ToolWorkspaceControl):
    """Narrow capability: one authorized call may mutate its Task workspace."""

    kernel: Kernel
    task_id: str
    invocation_id: str

    async def apply_patch(
        self, path: str, expected_hash: str | None,
        edits: tuple[Mapping[str, str], ...],
    ) -> Mapping[str, Any]:
        await self.kernel.recover_workspace_transactions(self.task_id)
        task = await self.kernel.get_task(self.task_id)
        prepared = prepare_workspace_text_write(
            Path(task.workspace), path, "", expected_hash,
            self.kernel.dependencies.workspace_path,
        )
        content = _render_workspace_patch(path, prepared.before_bytes, edits)

        record = await self.kernel.write_workspace_text(
            self.task_id, self.invocation_id, path, content, expected_hash
        )
        return record.to_data()

    async def apply_patches(
        self, patches: tuple[Mapping[str, Any], ...],
    ) -> tuple[Mapping[str, Any], ...]:
        await self.kernel.recover_workspace_transactions(self.task_id)
        task = await self.kernel.get_task(self.task_id)
        workspace = Path(task.workspace)
        writes: list[tuple[str, str, str | None]] = []
        total_edits = 0
        for patch in patches:
            path = str(patch["path"])
            expected_hash = patch["expected_hash"]
            edits = tuple(patch["edits"])
            total_edits += len(edits)
            prepared = prepare_workspace_text_write(
                workspace, path, "", expected_hash,
                self.kernel.dependencies.workspace_path,
            )
            writes.append((
                path,
                _render_workspace_patch(path, prepared.before_bytes, edits),
                expected_hash,
            ))
        if total_edits > 500:
            raise ValueError("batch patch cannot contain more than 500 edits")
        records = await self.kernel.write_workspace_text_batch(
            self.task_id, self.invocation_id, tuple(writes)
        )
        return tuple(record.to_data() for record in records)

    async def delete_file(
        self, path: str, expected_hash: str
    ) -> Mapping[str, Any]:
        record = await self.kernel.delete_workspace_file(
            self.task_id, self.invocation_id, path, expected_hash
        )
        return record.to_data()

    async def rollback_mutation(
        self, mutation_id: str
    ) -> Mapping[str, Any]:
        record = await self.kernel.rollback_workspace_mutation(
            self.task_id, self.invocation_id, mutation_id
        )
        return record.to_data()

    async def rollback_mutations(
        self, mutation_ids: tuple[str, ...]
    ) -> tuple[Mapping[str, Any], ...]:
        records = await self.kernel.rollback_workspace_mutations(
            self.task_id, self.invocation_id, mutation_ids
        )
        return tuple(record.to_data() for record in records)

    async def rollback_mutation_batch(
        self, mutation_ids: tuple[str, ...]
    ) -> tuple[Mapping[str, Any], ...]:
        records = await self.kernel.rollback_workspace_mutation_batch(
            self.task_id, self.invocation_id, mutation_ids
        )
        return tuple(record.to_data() for record in records)

    async def rollback_mutation_groups(
        self, mutation_groups: tuple[tuple[str, ...], ...]
    ) -> tuple[Mapping[str, Any], ...]:
        records = await self.kernel.rollback_workspace_mutation_groups(
            self.task_id, self.invocation_id, mutation_groups
        )
        return tuple(record.to_data() for record in records)


@dataclass(frozen=True, slots=True)
class _TaskMemoryControl(ToolMemoryControl):
    """Narrow capability: memory tools can affect only one Task identity."""

    kernel: Kernel
    task_id: str
    invocation_id: str
    idempotency_key: str

    def _operation_id(self, supplied: str) -> str:
        if not supplied.strip():
            raise ValueError("memory operation_id must not be empty")
        return canonical_hash({
            "tool_idempotency_key": self.idempotency_key,
            "supplied_operation_id": supplied,
        })

    async def list(self, include_stale: bool) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            view.to_data()
            for view in await self.kernel.list_task_memories(
                self.task_id, include_stale=include_stale
            )
        )

    async def remember(
        self, scope: str, content: str, source_kind: str,
        source_reference: str, source_hash: str | None, operation_id: str,
    ) -> Mapping[str, Any]:
        view = await self.kernel.remember_for_task(
            self.task_id, MemoryScope(scope), content,
            MemorySourceKind(source_kind), source_reference, source_hash,
            self._operation_id(operation_id), self.invocation_id,
        )
        return view.to_data()

    async def verify(
        self, memory_id: str, source_hash: str | None, operation_id: str,
    ) -> Mapping[str, Any]:
        view = await self.kernel.verify_task_memory(
            self.task_id, memory_id, source_hash,
            self._operation_id(operation_id), self.invocation_id,
        )
        return view.to_data()

    async def forget(
        self, memory_id: str, operation_id: str,
    ) -> Mapping[str, Any]:
        return await self.kernel.forget_task_memory(
            self.task_id, memory_id, self._operation_id(operation_id),
            self.invocation_id,
        )


@dataclass(frozen=True, slots=True)
class _TaskWorkingMemoryControl(ToolWorkingMemoryControl):
    """Narrow capability: one tool invocation can update one Task scratchpad."""

    kernel: Kernel
    task_id: str
    invocation_id: str
    idempotency_key: str

    async def read(self) -> Mapping[str, Any]:
        return (
            await self.kernel.get_effective_working_memory(self.task_id)
        ).to_data()

    async def update(
        self, expected_revision: int, state: Mapping[str, Any], operation_id: str,
    ) -> Mapping[str, Any]:
        if not operation_id.strip():
            raise ValueError("working memory operation_id must not be empty")
        scoped_operation = canonical_hash({
            "tool_idempotency_key": self.idempotency_key,
            "supplied_operation_id": operation_id,
        })
        return (await self.kernel.update_working_memory(
            self.task_id, expected_revision, state, scoped_operation,
            self.invocation_id,
        )).to_data()


@dataclass(frozen=True, slots=True)
class _TaskSpecControl(ToolTaskSpecControl):
    kernel: Kernel
    task_id: str
    invocation_id: str
    idempotency_key: str

    async def read(self) -> Mapping[str, Any]:
        return (await self.kernel.get_task_spec(self.task_id)).to_data()

    async def update(
        self, expected_revision: int, scope: tuple[str, ...],
        constraints: tuple[str, ...],
        acceptance_criteria: tuple[Mapping[str, Any], ...],
        operation_id: str,
    ) -> Mapping[str, Any]:
        criteria = tuple(
            TaskAcceptanceCriterion.from_data(item)
            for item in acceptance_criteria
        )
        scoped_operation = canonical_hash({
            "tool_idempotency_key": self.idempotency_key,
            "supplied_operation_id": operation_id,
        })
        return (await self.kernel.revise_task_spec(
            self.task_id, expected_revision, scope=scope,
            constraints=constraints, acceptance_criteria=criteria,
            operation_id=scoped_operation, writer=self.invocation_id,
        )).to_data()

    async def complete_outcome(
        self, outcome_id: str, completion_summary: str,
        evidence_refs: tuple[str, ...], remaining_work: tuple[str, ...],
    ) -> Mapping[str, Any]:
        return await self.kernel.request_task_outcome_completion(
            self.task_id, outcome_id, completion_summary=completion_summary,
            evidence_refs=evidence_refs, remaining_work=remaining_work,
            writer=self.invocation_id,
        )


class ToolMixin:
    @staticmethod
    def _latest_recoverable_tool_batch(
        messages: Sequence[Message],
    ) -> tuple[ToolResult, ...]:
        """Return retryable failures from the newest completed tool batch.

        This derives a generic recovery barrier from durable ToolResult data;
        it deliberately does not inspect a particular tool name or error code.
        """
        generated_prefixes = (
            "project-instructions-context-", "task-spec-context-",
            "project-onboarding-context-", "project-memory-context-",
            "session-context-", "working-memory-context-",
            "completion-readiness-", "continuation-input-", "steering-",
        )
        results: list[ToolResult] = []
        saw_result = False
        for message in reversed(messages):
            if message.message_id.startswith(generated_prefixes):
                continue
            tool_results = [
                block.result for block in message.content
                if isinstance(block, ToolResultBlock)
            ]
            if tool_results:
                results.extend(tool_results)
                saw_result = True
                continue
            if (
                saw_result
                and message.role is MessageRole.ASSISTANT
                and any(isinstance(block, ToolCallBlock) for block in message.content)
            ):
                break
        recoverable = {
            ToolRecoveryKind.RETRY_SAME,
            ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
        }
        return tuple(
            result for result in reversed(results)
            if not result.ok and result.effective_recovery_kind in recoverable
        )

    async def _suspend_unknown_tool_outcome(
        self, checkpoint: AgentTurnCheckpoint, call: ToolCall,
        result: ToolResult,
    ) -> AgentClarificationSuspended:
        """Stop immediately when a started side effect has no known result.

        This is a Runtime safety boundary, not a model decision.  Remaining
        calls in the accepted batch are cancelled before another tool or model
        request can run.  The original execution ledger remains immutable; a
        later answer records a separate reconciliation observation.
        """
        stored = await self._require_stored_task(checkpoint.task_id)
        task = TaskSnapshot.from_data(stored.data)
        execution_id = ToolExecutionRecord.identity(
            checkpoint.turn_id, call.call_id
        )
        execution = task.tool_executions.get(execution_id)
        if (
            execution is None
            or execution.state is not ToolCommitState.UNKNOWN_OUTCOME
        ):
            raise RuntimeError(
                "UNKNOWN_OUTCOME result is missing its execution ledger entry"
            )
        token = secrets.token_urlsafe(32)
        created = datetime.now(timezone.utc)
        choices = (
            ClarificationChoice(
                "CONFIRM_SUCCEEDED",
                "已核对现场：操作确实成功",
            ),
            ClarificationChoice(
                "CONFIRM_NOT_APPLIED",
                "已核对现场：操作没有生效",
            ),
            ClarificationChoice(
                "KEEP_BLOCKED",
                "暂时无法确认，保持阻塞",
            ),
        )
        request = ClarificationRequest(
            request_id=f"clarification-{uuid4().hex}",
            task_id=task.task_id, turn_id=checkpoint.turn_id, call=call,
            question=(
                "该操作已经开始，但 Runtime 没有收到可信的最终结果。"
                "请先核对目标系统中的实际状态。"
            ),
            choices=choices,
            reason=(
                f"{call.name} returned UNKNOWN_OUTCOME; automatic retry and "
                "dependent actions are unsafe"
            ),
            required=True,
            resume_token_hash=ClarificationRequest.hash_resume_token(token),
            created_at=created, expires_at=created + timedelta(hours=24),
            kind=ClarificationKind.OUTCOME_RECONCILIATION,
            execution_id=execution_id,
        )
        pending_user_action = {
            "kind": ClarificationKind.OUTCOME_RECONCILIATION.value,
            "request_id": request.request_id,
            "execution_id": execution_id,
            "tool_name": call.name,
            "outcome_ref": call.outcome_ref,
            "discarded_pending_call_count": len(
                checkpoint.pending_tool_calls
            ),
        }
        cancellation_messages = tuple(
            Message(
                f"msg-tool-cancelled-{uuid4().hex}", MessageRole.TOOL,
                (ToolResultBlock(ToolResult(
                    pending.call_id, False,
                    error_code="CANCELLED_AFTER_UNKNOWN_OUTCOME",
                    message=(
                        "The call was not started because an earlier call in "
                        "the same batch has an unknown external outcome."
                    ),
                    recovery_kind=ToolRecoveryKind.TERMINAL,
                    meta={"batch_cancelled": True},
                )),),
            )
            for pending in checkpoint.pending_tool_calls
        )
        suspended_checkpoint = replace(
            checkpoint, revision=checkpoint.revision + 1,
            messages=checkpoint.messages + cancellation_messages,
            pending_tool_calls=(), pending_user_action=pending_user_action,
            tool_batch=(
                checkpoint.tool_batch.with_pending(
                    (), status=ToolBatchStatus.RECONCILING
                ) if checkpoint.tool_batch is not None else None
            ),
        )
        awaiting = task.await_clarification(request).with_agent_checkpoint(
            suspended_checkpoint.to_data()
        )
        events: tuple[RuntimeEvent, ...] = (
            RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + 1,
                "outcome_reconciliation.requested", {
                    "request_id": request.request_id,
                    "turn_id": checkpoint.turn_id,
                    "execution_id": execution_id,
                    "tool_name": call.name,
                    "outcome_ref": call.outcome_ref,
                    "choice_count": len(choices),
                    "discarded_pending_call_count": len(
                        checkpoint.pending_tool_calls
                    ),
                    "error_code": result.error_code,
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + 2, "clarification.requested", {
                    "request_id": request.request_id,
                    "turn_id": checkpoint.turn_id,
                    "outcome_ref": call.outcome_ref,
                    "question_hash": canonical_hash({
                        "question": request.question
                    }),
                    "choice_count": len(choices),
                    "input_mode": request.input_mode,
                    "required": True,
                    "kind": request.kind.value,
                    "expires_at": request.expires_at.isoformat(),
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + 3, "checkpoint.saved", {
                    "turn_id": checkpoint.turn_id,
                    "revision": suspended_checkpoint.revision,
                    "checkpoint_hash": suspended_checkpoint.checkpoint_hash,
                    "reason": "unknown-tool-outcome-safety-stop",
                    "pending_user_action": pending_user_action,
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + 4, "task.state_changed", {
                    "previous_state": task.state.value,
                    "next_state": awaiting.state.value,
                    "reason": "unknown tool outcome requires reconciliation",
                },
            ),
        )
        if (
            call.outcome_ref is not None
            and call.outcome_binding_mode is OutcomeBindingMode.FULFILLMENT
        ):
            events += (RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id,
                stored.last_event_sequence + len(events) + 1,
                "task_outcome.state_changed", {
                    "outcome_id": call.outcome_ref,
                    "status": TaskOutcomeStatus.BLOCKED.value,
                    "fulfillment_ref": "",
                    "reason": "unknown_tool_execution_outcome",
                    "tool_call_id": call.call_id,
                },
            ),)
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task.task_id, stored.version, awaiting.to_data(), events
        ))
        return AgentClarificationSuspended(
            task.task_id, checkpoint.turn_id, suspended_checkpoint.revision,
            request.request_id, request.question,
            tuple((choice.value, choice.label) for choice in choices),
            request.reason, True, token, request.kind.value,
        )

    @staticmethod
    def _validate_tool_batch_proposal(
        calls: tuple[ToolCall, ...],
        visible_tool_by_name: Mapping[str, ToolSpec],
        seen_call_ids: set[str],
    ) -> None:
        """Validate a proposed batch completely before executing any call."""
        duplicate = [call.call_id for call in calls if call.call_id in seen_call_ids]
        if duplicate:
            raise InvalidModelResponse(
                "tool call_id must be unique within a turn: "
                + ", ".join(duplicate)
            )
        errors: list[str] = []
        for call in calls:
            spec = visible_tool_by_name.get(call.name)
            if spec is None:
                errors.append(
                    f"{call.call_id}: unknown tool {call.name}; available tools: "
                    + ", ".join(sorted(visible_tool_by_name))
                )
                continue
            try:
                validate_tool_arguments(spec, call.arguments)
            except InvalidToolArguments as error:
                properties = spec.parameters.get("properties", {})
                available = sorted(properties) if isinstance(properties, Mapping) else []
                errors.append(
                    f"{call.call_id}: {error}; available arguments: "
                    + ", ".join(available)
                )
        if errors:
            raise RecoverableToolProtocolError(
                "invalid_tool_batch", "; ".join(errors)
            )

    async def _new_tool_batch(
        self, task_id: str, source_message: Message,
        calls: tuple[ToolCall, ...],
    ) -> ToolBatchSnapshot:
        """Freeze accepted calls and their Task SPEC revision."""
        spec = await self.get_task_spec(task_id)
        return ToolBatchSnapshot(
            batch_id=f"batch-{uuid4().hex}",
            source_message_id=source_message.message_id,
            calls=calls,
            pending_call_ids=tuple(call.call_id for call in calls),
            status=ToolBatchStatus.ACCEPTED,
            task_spec_revision=spec.revision,
        )

    @staticmethod
    def _synchronize_tool_batch(
        checkpoint: AgentTurnCheckpoint,
    ) -> AgentTurnCheckpoint:
        """Migrate old checkpoints and keep batch pending state authoritative."""
        pending = checkpoint.pending_tool_calls
        batch = checkpoint.tool_batch
        if batch is None and pending:
            source = next((
                message for message in reversed(checkpoint.messages)
                if message.role is MessageRole.ASSISTANT
                and any(isinstance(block, ToolCallBlock)
                        for block in message.content)
            ), None)
            batch = ToolBatchSnapshot(
                batch_id=(
                    "legacy-batch-" + canonical_hash({
                        "turn_id": checkpoint.turn_id,
                        "calls": [call.to_data() for call in pending],
                    })[:20]
                ),
                source_message_id=(
                    source.message_id if source is not None else "legacy-unknown"
                ),
                calls=pending, pending_call_ids=tuple(
                    call.call_id for call in pending
                ),
                status=ToolBatchStatus.RECONCILING,
                task_spec_revision=checkpoint.task_spec_revision,
            )
        elif batch is not None:
            batch = batch.with_pending(pending)
        return replace(checkpoint, tool_batch=batch)

    @staticmethod
    def _looks_like_exact_text_tool_call(text: str) -> bool:
        """Fail closed when the whole response looks like tool protocol.

        Provider recovery remains deliberately strict because only a valid,
        advertised call may execute.  Final-answer validation is broader: a
        malformed block or a gateway-specific attribute order is still
        protocol, not a user-facing answer.
        """
        return bool(re.fullmatch(
            r'\s*<tool_use(?:\s+[^>]*)?>.*</tool_use>\s*',
            text, flags=re.DOTALL,
        ))

    async def _invoke_agent_tool(
        self,
        task_id: str,
        turn_id: str,
        call: ToolCall,
        timeout_seconds: float,
        agent_checkpoint: Mapping[str, Any] | None = None,
        recover_interrupted: bool = False,
        allow_closed_outcome_ref: bool = False,
    ) -> ToolResult:
        try:
            if call.name == "core.request_input":
                await self._request_agent_clarification(
                    task_id, turn_id, call, agent_checkpoint
                )
            return await self.invoke_tool(
                task_id, turn_id, call, timeout_seconds=timeout_seconds,
                agent_checkpoint=agent_checkpoint,
                recover_interrupted=recover_interrupted,
                allow_closed_outcome_ref=allow_closed_outcome_ref,
            )
        except ToolNotFound as error:
            result = ToolResult(
                call_id=call.call_id,
                ok=False,
                error_code="NOT_FOUND",
                message=str(error),
                hint="Choose a tool from the visible tool list.",
            )
        except InvalidToolArguments as error:
            result = ToolResult(
                call_id=call.call_id,
                ok=False,
                error_code="INVALID_PARAM",
                message=str(error),
                hint="Correct the arguments using the advertised tool schema.",
            )
        await self._append_events(
            task_id,
            (
                (
                    "tool.failed",
                    {
                        "turn_id": turn_id,
                        "invocation_id": None,
                        "outcome_ref": call.outcome_ref,
                        "result": result.to_data(),
                    },
                ),
            ),
        )
        return result

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        tools: list[ToolSpec] = []
        names: set[str] = set()
        for provider in self._dependencies.tools:
            for spec in await provider.list_tools():
                if spec.name in names:
                    raise DuplicateToolName(f"duplicate tool name: {spec.name}")
                names.add(spec.name)
                tools.append(spec)
        return tuple(tools)

    async def _bind_tool_call_outcome(
        self, task_id: str, call: ToolCall, tool: ToolSpec, *,
        allow_closed_ref: bool = False,
    ) -> ToolCall:
        """Discard legacy Outcome binding data for new runtime tool calls."""
        del task_id, tool, allow_closed_ref
        return replace(call, outcome_ref=None,
                       outcome_binding_mode=OutcomeBindingMode.FULFILLMENT)

    async def _decide_tool_call_outcome(
        self, task_id: str, call: ToolCall, tool: ToolSpec, *,
        allow_closed_ref: bool = False,
    ) -> OutcomeBindingDecision:
        """Legacy-only binding reader retained for historical diagnostics."""
        decision = await self._decide_legacy_tool_call_outcome(
            task_id, call, tool, allow_closed_ref=allow_closed_ref
        )
        if (
            call.outcome_ref is not None
            or decision.selected_outcome_ids
            or decision.eligible_outcome_ids
            or decision.compatible_outcome_ids
            or decision.action is not OutcomeBindingAction.ACCEPT
        ):
            await self._append_events(task_id, ((
                "task_outcome.binding_decided", {
                    "tool_call": call.to_data(),
                    "tool_effect": tool.effect.value,
                    **decision.to_data(),
                },
            ),))
        if (
            decision.action is OutcomeBindingAction.ACCEPT
            or (
                decision.action is OutcomeBindingAction.CORRECT
                and decision.bound_outcome_id is not None
            )
        ):
            return (
                replace(
                    call, outcome_ref=decision.bound_outcome_id,
                    outcome_binding_mode=decision.binding_mode,
                )
                if decision.bound_outcome_id is not None
                else replace(call, outcome_ref=None)
                if call.outcome_ref is not None else call
            )
        raise InvalidToolArguments(
            json.dumps(decision.to_data(), sort_keys=True, separators=(",", ":"))
        )

    async def _decide_legacy_tool_call_outcome(
        self, task_id: str, call: ToolCall, tool: ToolSpec, *,
        allow_closed_ref: bool = False,
    ) -> OutcomeBindingDecision:
        """Return a deterministic, fully inspectable legacy binding decision."""
        spec = await self.get_task_spec(task_id)
        if not spec.outcomes or tool.effect is ToolEffect.INTERNAL:
            if call.outcome_ref is not None and not spec.outcomes:
                return OutcomeBindingDecision(
                    OutcomeBindingAction.REJECT,
                    OutcomeBindingReason.OUTCOME_NOT_FOUND,
                    requested_outcome_id=call.outcome_ref,
                )
            return OutcomeBindingDecision(
                OutcomeBindingAction.ACCEPT,
                requested_outcome_id=call.outcome_ref,
                bound_outcome_id=call.outcome_ref,
            )
        eligible = TaskOutcomeEligibilityCalculator.eligible(spec)
        eligible_ids = tuple(item.outcome_id for item in eligible)
        compatible_ids = tuple(
            item.outcome_id for item in eligible
            if self._tool_effect_supports_outcome(tool.effect, item)
        )
        task = await self.get_task(task_id)

        def result(
            action: OutcomeBindingAction, reason: OutcomeBindingReason,
            bound: str | None = None,
            mode: OutcomeBindingMode = OutcomeBindingMode.FULFILLMENT,
        ) -> OutcomeBindingDecision:
            return OutcomeBindingDecision(
                action=action, reason=reason,
                requested_outcome_id=call.outcome_ref,
                bound_outcome_id=bound,
                eligible_outcome_ids=eligible_ids,
                compatible_outcome_ids=compatible_ids,
                binding_mode=mode,
            )

        if call.outcome_ref is not None:
            target = next((
                item for item in spec.outcomes
                if item.outcome_id == call.outcome_ref
            ), None)
            if target is None:
                return result(
                    OutcomeBindingAction.REJECT,
                    OutcomeBindingReason.OUTCOME_NOT_FOUND,
                )
            if target.status.is_closed:
                if not allow_closed_ref and len(compatible_ids) == 1:
                    return result(
                        OutcomeBindingAction.CORRECT,
                        OutcomeBindingReason.UNIQUE_COMPATIBLE_OPEN_OUTCOME,
                        compatible_ids[0],
                    )
                return result(
                    OutcomeBindingAction.ACCEPT if allow_closed_ref
                    else OutcomeBindingAction.REJECT,
                    OutcomeBindingReason.NONE if allow_closed_ref
                    else OutcomeBindingReason.OUTCOME_ALREADY_CLOSED,
                    target.outcome_id if allow_closed_ref else None,
                )
            if target.outcome_id not in eligible_ids:
                if (
                    tool.effect is ToolEffect.OBSERVE
                    and target.status is not TaskOutcomeStatus.BLOCKED
                    and self._supporting_observation_is_available(
                        target, eligible_ids
                    )
                ):
                    return result(
                        OutcomeBindingAction.ACCEPT,
                        OutcomeBindingReason.NONE, target.outcome_id,
                        OutcomeBindingMode.SUPPORTING,
                    )
                return result(
                    OutcomeBindingAction.CORRECT,
                    OutcomeBindingReason.OUTCOME_DEPENDENCY_UNSATISFIED,
                )
            if not self._tool_effect_supports_outcome(tool.effect, target):
                # A recognized test/build command after a workspace mutation is
                # task-level checking, not fulfillment of the implementation
                # Outcome. Models often copy the preceding mutation's reference;
                # normalize that harmless bookkeeping error instead of rejecting
                # the command before it can produce verification evidence.
                if (
                    call.name == "core.run_command"
                    and task.mutation_journal
                    and is_verification_request(call.arguments)
                ):
                    return result(
                        OutcomeBindingAction.ACCEPT,
                        OutcomeBindingReason.NO_COMPATIBLE_OUTCOME,
                    )
                return result(
                    OutcomeBindingAction.CORRECT,
                    OutcomeBindingReason.OUTCOME_EFFECT_MISMATCH,
                )
            # A valid explicit outcome_ref carries the model's semantic
            # choice. Historical execution focus must not participate in
            # new-runtime Outcome binding.
            return result(
                OutcomeBindingAction.ACCEPT, OutcomeBindingReason.NONE,
                target.outcome_id,
            )

        if compatible_ids:
            if len(compatible_ids) == 1:
                return result(
                    OutcomeBindingAction.CORRECT,
                    OutcomeBindingReason.UNIQUE_COMPATIBLE_OPEN_OUTCOME,
                    compatible_ids[0],
                )
            return result(
                OutcomeBindingAction.REQUIRE_SELECTION,
                OutcomeBindingReason.OUTCOME_SELECTION_AMBIGUOUS,
            )
        return result(
            OutcomeBindingAction.ACCEPT,
            OutcomeBindingReason.NO_COMPATIBLE_OUTCOME,
        )

    @staticmethod
    def _tool_effect_supports_outcome(
        effect: ToolEffect, outcome: TaskOutcomeSnapshot,
    ) -> bool:
        """Return whether an effect may be linked to an Outcome.

        Observe is authority-free supporting evidence for a conversational
        answer.  It deliberately does not authorize mutate, execute, control,
        network, or interaction effects that the Planner omitted.
        """
        return (
            effect in outcome.required_effects
            or (
                outcome.kind is TaskOutcomeKind.ANSWER
                and effect is ToolEffect.OBSERVE
            )
        )

    async def invoke_tool(
        self,
        task_id: str,
        turn_id: str,
        call: ToolCall,
        timeout_seconds: float = 30.0,
        agent_checkpoint: Mapping[str, Any] | None = None,
        recover_interrupted: bool = False,
        allow_closed_outcome_ref: bool = False,
    ) -> ToolResult:
        if not turn_id.strip():
            raise ValueError("turn_id must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        task = TaskSnapshot.from_data(stored.data)
        if task.state is not TaskState.EXECUTING:
            raise InvalidTurnState(
                f"task {task_id} must be EXECUTING, got {task.state.value}"
            )
        if call.name == "core.run_command":
            current_trust = await self.get_project_trust(Path(task.workspace))
            if (
                current_trust.level is not task.project_trust
                or current_trust.fingerprint != task.project_fingerprint
                or current_trust.subject != task.trust_subject
            ):
                task = task.with_project_trust(
                    current_trust.level, current_trust.fingerprint,
                    current_trust.subject,
                )
                trust_event = RuntimeEvent(
                    event_id=f"evt-{uuid4().hex}", task_id=task_id,
                    sequence=stored.last_event_sequence + 1,
                    event_type="project.trust_refreshed",
                    payload={
                        "level": current_trust.level.value,
                        "fingerprint": current_trust.fingerprint,
                        "subject": current_trust.subject,
                    },
                )
                await self._dependencies.store.commit(RuntimeUnitOfWork(
                    task_id, stored.version, task.to_data(), (trust_event,)
                ))
                stored = await self._require_stored_task(task_id)

        provider, selected_spec = await self._find_tool(call.name)
        existing = task.tool_executions.get(
            ToolExecutionRecord.identity(turn_id, call.call_id)
        )
        validate_tool_arguments(selected_spec, call.arguments)
        call = await self._bind_tool_call_outcome(
            task_id, call, selected_spec,
            allow_closed_ref=(
                existing is not None or allow_closed_outcome_ref
            ),
        )
        # Binding emits its own audit event. Refresh the optimistic-lock base
        # before policy/execution commits so observability cannot create a
        # false version conflict.
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        invocation_id = f"inv-{uuid4().hex}"
        document_failure = await self._document_fetch_failure(task_id)
        if (
            document_failure is not None
            and call.name in {
                "core.search_files", "core.find_files", "core.search_text",
                "core.grep_search",
            }
        ):
            blocked = self._dependencies.tool_result_governor.govern(
                selected_spec,
                call,
                ToolResult(
                    call_id=call.call_id,
                    ok=False,
                    error_code="DOCUMENT_FETCH_UNAVAILABLE",
                    message=(
                        "The Task has an unresolved explicit document URL and "
                        "web.fetch_markdown is unavailable; local workspace search "
                        "was not executed as a substitute for reading that document."
                    ),
                    hint=document_failure.hint,
                    retryable=False,
                    data=document_failure.data,
                    meta={
                        **dict(document_failure.meta),
                        "runtime_guard": "EXPLICIT_DOCUMENT_URL_LOCAL_SEARCH_BLOCKED",
                    },
                ),
            )
            await self._dependencies.store.commit(RuntimeUnitOfWork(
                task_id, stored.version, task.to_data(), (RuntimeEvent(
                    f"evt-{uuid4().hex}", task_id,
                    stored.last_event_sequence + 1, "tool.failed", {
                        "turn_id": turn_id, "invocation_id": invocation_id,
                        "outcome_ref": call.outcome_ref,
                        "result": blocked.to_data(),
                    },
                ),)
            ))
            return blocked
        if existing is not None:
            expected_hash = self._tool_policy.payload_hash(
                selected_spec, call, task.project_trust
            )
            if existing.payload_hash != expected_hash:
                raise IdempotencyConflict(
                    "tool call_id was reused with different tool arguments or safety metadata"
                )
            if existing.result is not None and existing.state in {
                ToolCommitState.COMMITTED,
                ToolCommitState.FAILED,
                ToolCommitState.CANCELLED,
                ToolCommitState.UNKNOWN_OUTCOME,
            }:
                await self._record_tool_result_reused(task_id, turn_id, existing)
                return existing.result
            if not recover_interrupted:
                raise ToolExecutionInProgress(
                    f"tool execution is already RUNNING: {existing.execution_id}"
                )
            return await self._recover_interrupted_tool(
                task, existing, provider, selected_spec, timeout_seconds
            )

        invocation_id = f"inv-{uuid4().hex}"
        decision = self._tool_policy.evaluate(
            selected_spec, call, task.project_trust
        )
        policy_event = RuntimeEvent(
            event_id=f"evt-{uuid4().hex}",
            task_id=task_id,
            sequence=stored.last_event_sequence + 1,
            event_type="policy.evaluated",
            payload={
                "turn_id": turn_id,
                "invocation_id": invocation_id,
                "tool_name": call.name,
                "outcome_ref": call.outcome_ref,
                "decision": decision.to_data(),
            },
        )
        external_read_root = self._external_read_root_requiring_approval(
            task, call
        )
        if external_read_root is not None:
            request = ApprovalRequest(
                request_id=f"approval-{uuid4().hex}",
                task_id=task_id, turn_id=turn_id, invocation_id=invocation_id,
                policy_decision_id=decision.decision_id,
                payload_hash=decision.payload_hash,
                risk=ToolRisk.R0, call=call,
                action=(
                    f"Allow {call.name} to read one directory outside the "
                    "current workspace"
                ),
                target=str(external_read_root),
                preview=(
                    "Permission: read-only; scope: current Task; "
                    f"requested by {call.name}. Sensitive files remain blocked."
                ),
                network_access="not required",
                data_transmission="none",
                rollback="expires automatically when the current Task ends",
                created_at=utc_now(),
                agent_checkpoint=(
                    dict(agent_checkpoint) if agent_checkpoint is not None else None
                ),
                kind=ApprovalKind.WORKSPACE_READ,
                workspace_access_root=str(external_read_root),
            )
            await self._persist_approval_request(
                stored, task, request, policy_event, agent_checkpoint
            )
            raise ApprovalRequired(request)
        if decision.requires_approval:
            request = ApprovalRequest(
                request_id=f"approval-{uuid4().hex}",
                task_id=task_id,
                turn_id=turn_id,
                invocation_id=invocation_id,
                policy_decision_id=decision.decision_id,
                payload_hash=decision.payload_hash,
                risk=decision.effective_risk,
                call=call,
                action=selected_spec.description,
                target=self._approval_target(call),
                preview=self._approval_preview(call),
                network_access=(
                    "required" if decision.requires_network else "not required"
                ),
                data_transmission=(
                    "command-defined data may be transmitted to remote endpoints"
                    if decision.requires_network
                    else selected_spec.data_transmission
                ),
                rollback=selected_spec.rollback,
                created_at=utc_now(),
                agent_checkpoint=(
                    dict(agent_checkpoint) if agent_checkpoint is not None else None
                ),
            )
            awaiting = task.await_approval(request)
            approval_event = RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=stored.last_event_sequence + 2,
                event_type="approval.requested",
                payload=request.to_data(),
            )
            approval_events: tuple[RuntimeEvent, ...]
            state_sequence = stored.last_event_sequence + 3
            if agent_checkpoint is not None:
                checkpoint_event = RuntimeEvent(
                    event_id=f"evt-{uuid4().hex}",
                    task_id=task_id,
                    sequence=state_sequence,
                    event_type="checkpoint.saved",
                    payload={
                        "turn_id": turn_id,
                        "request_id": request.request_id,
                        "revision": agent_checkpoint.get("revision"),
                    },
                )
                approval_events = (policy_event, approval_event, checkpoint_event)
                state_sequence += 1
            else:
                approval_events = (policy_event, approval_event)
            state_event = RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=state_sequence,
                event_type="task.state_changed",
                payload={
                    "previous_state": task.state.value,
                    "next_state": awaiting.state.value,
                    "reason": f"approval required: {request.request_id}",
                },
            )
            await self._dependencies.store.commit(
                RuntimeUnitOfWork(
                    task_id=task_id,
                    expected_version=stored.version,
                    next_state=awaiting.to_data(),
                    events=approval_events + (state_event,),
                )
            )
            raise ApprovalRequired(request)

        if not decision.allowed:
            denied = self._dependencies.tool_result_governor.govern(
                selected_spec,
                call,
                ToolResult(
                    call_id=call.call_id,
                    ok=False,
                    error_code="PERMISSION_DENIED",
                    message=decision.reason,
                    hint="Use an R0 read-only tool or request an approved capability.",
                    meta={
                        "policy_decision_id": decision.decision_id,
                        "payload_hash": decision.payload_hash,
                    },
                ),
            )
            failed_event = RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=stored.last_event_sequence + 2,
                event_type="tool.failed",
                payload={
                    "turn_id": turn_id,
                    "invocation_id": invocation_id,
                    "outcome_ref": call.outcome_ref,
                    "result": denied.to_data(),
                },
            )
            await self._dependencies.store.commit(
                RuntimeUnitOfWork(
                    task_id=task_id,
                    expected_version=stored.version,
                    next_state=task.to_data(),
                    events=(policy_event, failed_event),
                )
            )
            return denied

        return await self._execute_authorized_tool(
            task_id=task_id,
            turn_id=turn_id,
            call=call,
            provider=provider,
            selected_spec=selected_spec,
            invocation_id=invocation_id,
            policy_decision_id=decision.decision_id,
            effective_risk=decision.effective_risk,
            payload_hash=decision.payload_hash,
            timeout_seconds=timeout_seconds,
            initial_events=(policy_event,),
        )

    async def _execute_authorized_tool(
        self,
        *,
        task_id: str,
        turn_id: str,
        call: ToolCall,
        provider: ToolProviderPort,
        selected_spec: ToolSpec,
        invocation_id: str,
        policy_decision_id: str,
        effective_risk: ToolRisk,
        payload_hash: str,
        timeout_seconds: float,
        approval_request_id: str | None = None,
        initial_events: tuple[RuntimeEvent, ...] = (),
        next_state: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        effective_timeout_seconds = timeout_seconds
        if call.name == "core.run_command":
            declared = call.arguments.get("timeout_seconds")
            grace = call.arguments.get("termination_grace_seconds", 2.0)
            if (
                isinstance(declared, (int, float))
                and not isinstance(declared, bool)
                and isinstance(grace, (int, float))
                and not isinstance(grace, bool)
            ):
                # The process supervisor owns timeout termination and can return
                # a definitive TIMED_OUT result. Give it enough time to finish
                # TERM/KILL and output draining before the outer Tool deadline.
                effective_timeout_seconds = max(
                    timeout_seconds, float(declared) + float(grace) + 2.0
                )
        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        task = TaskSnapshot.from_data(stored.data)
        execution = ToolExecutionRecord.start(
            task_id=task_id,
            turn_id=turn_id,
            invocation_id=invocation_id,
            call=call,
            payload_hash=payload_hash,
            policy_decision_id=policy_decision_id,
            effective_risk=effective_risk,
            approval_request_id=approval_request_id,
            idempotency=selected_spec.idempotency,
            effect=selected_spec.effect,
            result_authority=self._tool_policy.execution_result_authority(
                selected_spec, call
            ),
        )
        prepared_task = task.with_tool_execution(execution)
        if next_state is not None:
            resumed_task = TaskSnapshot.from_data(next_state)
            prepared_task = resumed_task.with_tool_execution(execution)
        prepared_sequence = stored.last_event_sequence + len(initial_events) + 1
        prepared = RuntimeEvent(
            event_id=f"evt-{uuid4().hex}",
            task_id=task_id,
            sequence=prepared_sequence,
            event_type="tool.prepared",
            payload={
                "turn_id": turn_id,
                "invocation_id": invocation_id,
                "call": call.to_data(),
                "tool": selected_spec.to_data(),
                "policy_decision_id": policy_decision_id,
                "payload_hash": payload_hash,
                "approval_request_id": approval_request_id,
                "execution_id": execution.execution_id,
                "commit_state": execution.state.value,
                "idempotency": execution.idempotency.value,
                "idempotency_key": execution.idempotency_key,
            },
        )
        await self._dependencies.store.commit(
            RuntimeUnitOfWork(
                task_id=task_id,
                expected_version=stored.version,
                next_state=prepared_task.to_data(),
                events=initial_events + (prepared,),
            )
        )
        after_prepare = await self._dependencies.store.load_task(task_id)
        if after_prepare is None:
            raise TaskNotFound(f"task disappeared before tool start: {task_id}")
        current_task = TaskSnapshot.from_data(after_prepare.data)
        running_execution = execution.mark_running()
        running_task = current_task.with_tool_execution(running_execution)
        started = RuntimeEvent(
            event_id=f"evt-{uuid4().hex}",
            task_id=task_id,
            sequence=after_prepare.last_event_sequence + 1,
            event_type="tool.started",
            payload={
                "turn_id": turn_id,
                "invocation_id": invocation_id,
                "call": call.to_data(),
                "tool": selected_spec.to_data(),
                "policy_decision_id": policy_decision_id,
                "payload_hash": payload_hash,
                "approval_request_id": approval_request_id,
                "execution_id": execution.execution_id,
                "commit_state": running_execution.state.value,
                "idempotency": execution.idempotency.value,
                "idempotency_key": execution.idempotency_key,
            },
        )
        started_events: tuple[RuntimeEvent, ...] = (started,)
        if (
            call.outcome_ref is not None
            and call.outcome_binding_mode is OutcomeBindingMode.FULFILLMENT
        ):
            started_events += (RuntimeEvent(
                event_id=f"evt-{uuid4().hex}", task_id=task_id,
                sequence=after_prepare.last_event_sequence + 2,
                event_type="task_outcome.state_changed", payload={
                    "outcome_id": call.outcome_ref,
                    "status": TaskOutcomeStatus.IN_PROGRESS.value,
                    "fulfillment_ref": "",
                    "reason": "authorized_tool_started",
                    "tool_call_id": call.call_id,
                },
            ),)
        await self._dependencies.store.commit(
            RuntimeUnitOfWork(
                task_id=task_id,
                expected_version=after_prepare.version,
                next_state=running_task.to_data(),
                events=started_events,
            )
        )

        context = ToolInvocationContext(
            invocation_id=invocation_id,
            task_id=task_id,
            turn_id=turn_id,
            workspace=Path(task.workspace),
            deadline=(datetime.now(timezone.utc)
                      + timedelta(seconds=effective_timeout_seconds)),
            policy_decision_id=policy_decision_id,
            authorized_risk=effective_risk,
            payload_hash=payload_hash,
            approval_request_id=approval_request_id,
            idempotency_key=execution.idempotency_key,
            process_control=_TaskProcessControl(
                self, task_id, turn_id, invocation_id
            ),
            workspace_control=_TaskWorkspaceControl(
                self, task_id, invocation_id
            ),
            memory_control=_TaskMemoryControl(
                self, task_id, invocation_id,
                execution.idempotency_key or payload_hash,
            ),
            working_memory_control=_TaskWorkingMemoryControl(
                self, task_id, invocation_id,
                execution.idempotency_key or payload_hash,
            ),
            task_spec_control=_TaskSpecControl(
                self, task_id, invocation_id,
                execution.idempotency_key or payload_hash,
            ),
            workspace_path=self._dependencies.workspace_path,
            additional_read_roots=self._additional_read_roots(
                running_task, call.name
            ),
            resource_paths=self._resource_paths(running_task),
            resource_candidates=self._resource_candidates(running_task),
        )
        try:
            async with asyncio.timeout(effective_timeout_seconds):
                result = await provider.invoke(call, context)
            if result.call_id != call.call_id:
                raise InvalidToolResult(
                    f"tool result call_id {result.call_id!r} does not match {call.call_id!r}"
                )
        except TimeoutError as error:
            result = ToolResult(
                call_id=call.call_id,
                ok=False,
                error_code="TIMEOUT",
                message=(
                    f"tool exceeded {effective_timeout_seconds:g} second deadline"
                ),
                retryable=selected_spec.idempotency.value != "non_idempotent",
                recovery_kind=(
                    ToolRecoveryKind.RETRY_SAME
                    if selected_spec.idempotency.value != "non_idempotent"
                    else ToolRecoveryKind.UNKNOWN_OUTCOME
                ),
                meta={"error_type": type(error).__name__},
            )
        except Exception as error:
            result = ToolResult(
                call_id=call.call_id,
                ok=False,
                error_code="TOOL_FAILED",
                message=str(error),
                retryable=False,
                meta={"error_type": type(error).__name__},
            )

        after_tool = await self._dependencies.store.load_task(task_id)
        if after_tool is None:
            raise TaskNotFound(f"task disappeared during tool invocation: {task_id}")
        current_task = TaskSnapshot.from_data(after_tool.data)
        if (
            not result.ok
            and result.error_code == "TIMEOUT"
            and selected_spec.idempotency is ToolIdempotency.NON_IDEMPOTENT
        ):
            commit_state = ToolCommitState.UNKNOWN_OUTCOME
            result = ToolResult(
                call_id=result.call_id,
                ok=False,
                error_code="UNKNOWN_OUTCOME",
                message=(
                    "non-idempotent tool timed out after starting; the outcome "
                    "cannot be safely inferred"
                ),
                hint="Inspect the target system before deciding whether to retry.",
                retryable=False,
                recovery_kind=ToolRecoveryKind.UNKNOWN_OUTCOME,
                meta=result.meta,
            )
        else:
            commit_state = (
                ToolCommitState.COMMITTED if result.ok else ToolCommitState.FAILED
            )
        result = self._dependencies.tool_result_governor.govern(
            selected_spec, call, result
        )
        finished_execution = running_execution.finish(commit_state, result)
        finished_task = current_task.with_tool_execution(finished_execution)
        completed = RuntimeEvent(
            event_id=f"evt-{uuid4().hex}",
            task_id=task_id,
            sequence=after_tool.last_event_sequence + 1,
            event_type="tool.completed" if result.ok else "tool.failed",
            payload={
                "turn_id": turn_id,
                "invocation_id": invocation_id,
                "result": result.to_data(),
                "fact_descriptor": (
                    result.fact_descriptor.to_data()
                    if result.fact_descriptor is not None else None
                ),
                "execution_id": execution.execution_id,
                "outcome_ref": call.outcome_ref,
                "commit_state": commit_state.value,
            },
        )
        completed_events: tuple[RuntimeEvent, ...] = (completed,)
        if (
            result.ok and call.outcome_ref is not None
            and call.outcome_binding_mode is OutcomeBindingMode.FULFILLMENT
            and selected_spec.effect is not ToolEffect.INTERNAL
        ):
            live_spec = await self.get_task_spec(task_id)
            outcome = next(
                item for item in live_spec.outcomes
                if item.outcome_id == call.outcome_ref
            )
            reference = (
                f"tool:{execution.execution_id}:{selected_spec.effect.value}"
            )
            refs = outcome.fulfillment_refs + (reference,)
            next_status = (
                TaskOutcomeStatus.DELIVERED
                if self._atomic_action_completed(outcome, call, result)
                else TaskOutcomeStatus.IN_PROGRESS
            )
            completed_events += (RuntimeEvent(
                event_id=f"evt-{uuid4().hex}", task_id=task_id,
                sequence=after_tool.last_event_sequence + 2,
                event_type="task_outcome.state_changed", payload={
                    "outcome_id": call.outcome_ref,
                    "status": next_status.value,
                    "fulfillment_ref": reference,
                    "reason": "committed_tool_result",
                    "tool_call_id": call.call_id,
                    "tool_effect": selected_spec.effect.value,
                },
            ),)
        elif (
            result.ok and call.outcome_ref is not None
            and call.outcome_binding_mode is OutcomeBindingMode.SUPPORTING
        ):
            completed_events += (RuntimeEvent(
                event_id=f"evt-{uuid4().hex}", task_id=task_id,
                sequence=after_tool.last_event_sequence + 2,
                event_type="task_outcome.support_observed", payload={
                    "outcome_id": call.outcome_ref,
                    "tool_call_id": call.call_id,
                    "tool_effect": selected_spec.effect.value,
                    "binding_mode": call.outcome_binding_mode.value,
                },
            ),)
        await self._dependencies.store.commit(
            RuntimeUnitOfWork(
                task_id=task_id,
                expected_version=after_tool.version,
                next_state=finished_task.to_data(),
                events=completed_events,
            )
        )
        return result

    async def recover_tool_execution(
        self, task_id: str, turn_id: str, call_id: str, timeout_seconds: float = 30.0
    ) -> ToolResult:
        await self.recover_workspace_transactions(task_id)
        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        task = TaskSnapshot.from_data(stored.data)
        execution_id = ToolExecutionRecord.identity(turn_id, call_id)
        execution = task.tool_executions.get(execution_id)
        if execution is None:
            raise ToolNotFound(f"tool execution not found: {execution_id}")
        provider, spec = await self._find_tool(execution.call.name)
        if execution.result is not None:
            await self._record_tool_result_reused(task_id, turn_id, execution)
            return execution.result
        return await self._recover_interrupted_tool(
            task, execution, provider, spec, timeout_seconds
        )

    async def _recover_interrupted_tool(
        self,
        task: TaskSnapshot,
        execution: ToolExecutionRecord,
        provider: ToolProviderPort,
        spec: ToolSpec,
        timeout_seconds: float,
    ) -> ToolResult:
        if execution.state is ToolCommitState.PREPARED:
            return await self._resume_safe_tool(
                task, execution, provider, spec, timeout_seconds
            )
        if execution.state is not ToolCommitState.RUNNING:
            raise ToolExecutionInProgress(
                f"tool execution has no recoverable result: {execution.execution_id}"
            )
        if execution.idempotency is ToolIdempotency.NON_IDEMPOTENT:
            result = self._dependencies.tool_result_governor.govern(
                spec,
                execution.call,
                ToolResult(
                    call_id=execution.call.call_id,
                    ok=False,
                    error_code="UNKNOWN_OUTCOME",
                    message=(
                        "non-idempotent tool started before interruption; its external "
                        "outcome must be checked before any retry"
                    ),
                    hint="Inspect the target system or ask the user how to proceed.",
                    retryable=False,
                    meta={"execution_id": execution.execution_id},
                ),
            )
            unknown = execution.finish(ToolCommitState.UNKNOWN_OUTCOME, result)
            await self._commit_recovery_result(task, unknown, result, "tool.unknown_outcome")
            return result
        return await self._resume_safe_tool(
            task, execution, provider, spec, timeout_seconds
        )

    async def _resume_safe_tool(
        self,
        task: TaskSnapshot,
        execution: ToolExecutionRecord,
        provider: ToolProviderPort,
        spec: ToolSpec,
        timeout_seconds: float,
    ) -> ToolResult:
        stored = await self._dependencies.store.load_task(task.task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task.task_id}")
        current = TaskSnapshot.from_data(stored.data)
        running_execution = (
            execution.mark_running()
            if execution.state is ToolCommitState.PREPARED
            else execution
        )
        running_task = current.with_tool_execution(running_execution)
        recovery_started = RuntimeEvent(
            event_id=f"evt-{uuid4().hex}",
            task_id=task.task_id,
            sequence=stored.last_event_sequence + 1,
            event_type="tool.recovery_started",
            payload={
                "turn_id": execution.turn_id,
                "execution_id": execution.execution_id,
                "previous_commit_state": execution.state.value,
                "commit_state": ToolCommitState.RUNNING.value,
                "idempotency": execution.idempotency.value,
                "idempotency_key": execution.idempotency_key,
            },
        )
        await self._dependencies.store.commit(
            RuntimeUnitOfWork(
                task.task_id, stored.version, running_task.to_data(),
                (recovery_started,),
            )
        )
        context = ToolInvocationContext(
            invocation_id=execution.invocation_id,
            task_id=task.task_id,
            turn_id=execution.turn_id,
            workspace=Path(task.workspace),
            deadline=datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds),
            policy_decision_id=execution.policy_decision_id,
            authorized_risk=execution.effective_risk,
            payload_hash=execution.payload_hash,
            approval_request_id=execution.approval_request_id,
            idempotency_key=execution.idempotency_key,
            process_control=_TaskProcessControl(
                self, task.task_id, execution.turn_id,
                execution.invocation_id,
            ),
            workspace_control=_TaskWorkspaceControl(
                self, task.task_id, execution.invocation_id
            ),
            memory_control=_TaskMemoryControl(
                self, task.task_id, execution.invocation_id,
                execution.idempotency_key or execution.payload_hash,
            ),
            working_memory_control=_TaskWorkingMemoryControl(
                self, task.task_id, execution.invocation_id,
                execution.idempotency_key or execution.payload_hash,
            ),
            task_spec_control=_TaskSpecControl(
                self, task.task_id, execution.invocation_id,
                execution.idempotency_key or execution.payload_hash,
            ),
            workspace_path=self._dependencies.workspace_path,
            additional_read_roots=self._additional_read_roots(
                running_task, execution.call.name
            ),
            resource_paths=self._resource_paths(running_task),
            resource_candidates=self._resource_candidates(running_task),
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await provider.invoke(execution.call, context)
            if result.call_id != execution.call.call_id:
                raise InvalidToolResult(
                    "recovered tool result call_id does not match the original call"
                )
        except TimeoutError:
            result = ToolResult(
                execution.call.call_id, False, error_code="TIMEOUT",
                message="recovered tool execution timed out", retryable=True,
            )
        except Exception as error:
            result = ToolResult(
                execution.call.call_id, False, error_code="TOOL_FAILED",
                message=str(error), retryable=False,
                meta={"error_type": type(error).__name__},
            )
        result = self._dependencies.tool_result_governor.govern(
            spec,
            execution.call,
            result,
        )
        state = ToolCommitState.COMMITTED if result.ok else ToolCommitState.FAILED
        finished = running_execution.finish(state, result)
        await self._commit_recovery_result(task, finished, result, "tool.recovered")
        return result

    async def _record_tool_result_reused(
        self, task_id: str, turn_id: str, execution: ToolExecutionRecord
    ) -> None:
        await self._append_events(
            task_id,
            ((
                "tool.result_reused",
                {
                    "turn_id": turn_id,
                    "execution_id": execution.execution_id,
                    "commit_state": execution.state.value,
                },
            ),),
        )

    async def _find_tool(
        self, name: str
    ) -> tuple[ToolProviderPort, ToolSpec]:
        provider: ToolProviderPort | None = None
        selected_spec: ToolSpec | None = None
        for candidate in self._dependencies.tools:
            for spec in await candidate.list_tools():
                if spec.name != name:
                    continue
                if provider is not None:
                    raise DuplicateToolName(f"duplicate tool name: {name}")
                provider = candidate
                selected_spec = spec
        if provider is None or selected_spec is None:
            raise ToolNotFound(f"tool not found: {name}")
        return provider, selected_spec

    def _present_tool_arguments(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> ToolArgumentPresentation:
        presenter = self._dependencies.tool_argument_presenter
        if presenter is not None:
            return presenter.present(tool_name, arguments)
        for key in ("diff", "patch", "command", "argv"):
            value = arguments.get(key)
            if value is not None:
                rendered = str(value)
                text = rendered[:2000] + ("…" if len(rendered) > 2000 else "")
                return ToolArgumentPresentation(text, visible_arguments=arguments)
        safe_arguments = {
            key: value
            for key, value in arguments.items()
            if not any(
                secret in key.lower()
                for secret in ("secret", "token", "password", "api_key", "apikey")
            )
        }
        try:
            rendered = json.dumps(
                safe_arguments, ensure_ascii=False, sort_keys=True
            )
        except (TypeError, ValueError):
            rendered = "arguments are bound by payload hash but cannot be previewed"
        text = rendered[:2000] + ("…" if len(rendered) > 2000 else "")
        return ToolArgumentPresentation(text, visible_arguments=safe_arguments)
