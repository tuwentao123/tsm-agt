"""Agent checkpoint persistence, compatibility and reconciliation for `Kernel`.

A mixin module in the Hermes facade+siblings shape (``run_agent.py`` +
``agent/session_persistence.py``): the facade ``core/kernel.py`` imports
this at module level and adds ``CheckpointMixin`` to ``Kernel``'s bases, so
**every method resolves through ``Kernel``'s MRO unchanged** --
``self._dependencies``, ``self._append_events`` and ``Kernel``'s own class
attributes all still resolve. Moved bodies are verbatim: nothing is renamed
and no signature changes.

This module must never bind the facade at module level; late-import it
inside a method instead (Hermes ``AGENTS.md:243-244``).

Moved from ``core/kernel.py``:

* ``_agent_checkpoint_conflicts``
* ``_bind_agent_checkpoint``
* ``_checkpoint_has_unknown_side_effect``
* ``_compact_continuation_messages``
* ``_complete_agent_checkpoint``
* ``_evaluate_checkpoint_compatibility``
* ``_last_continuation_pending``
* ``_mark_checkpoint_conflict``
* ``_rebase_agent_checkpoint``
* ``_reconcilable_checkpoint_differences``
* ``_reconcile_agent_checkpoint``
* ``_save_agent_checkpoint``
* ``_validate_agent_checkpoint``
"""

from __future__ import annotations


import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from tsm_agt.ports import (
    CheckpointCompatibilityAction, CheckpointCompatibilityDecision,
    CheckpointCompatibilityProbe, Message, MessageRole, RuntimeEvent,
    RuntimeUnitOfWork, TextBlock, ToolCallBlock, ToolIdempotency,
    ToolResultBlock
)
from typing import Any
from uuid import uuid4
from .agent_loop import AgentCheckpointConflict, AgentTurnCheckpoint
from .configuration import canonical_hash, effective_toolset_hash
from .evidence_question import EvidenceQuestionProjection
from .execution import ToolCommitState, ToolExecutionRecord
from .session import SessionState
from .task import LEGAL_TRANSITIONS, TaskSnapshot, TaskState
from .trust import workspace_fingerprint


class CheckpointMixin:
    @staticmethod
    def _checkpoint_has_unknown_side_effect(task: TaskSnapshot) -> bool:
        return any(
            (
                execution.state is ToolCommitState.UNKNOWN_OUTCOME
                and execution.reconciled_outcome is None
            )
            or (
                execution.state is ToolCommitState.RUNNING
                and execution.idempotency is ToolIdempotency.NON_IDEMPOTENT
            )
            for execution in task.tool_executions.values()
        )

    async def _bind_agent_checkpoint(
        self, task: TaskSnapshot, checkpoint: AgentTurnCheckpoint,
        visible_tools: tuple[ToolSpec, ...],
    ) -> AgentTurnCheckpoint:
        if not task.effective_configurations:
            raise RuntimeError("Agent checkpoint requires Effective Configuration")
        configuration = task.effective_configurations[-1]
        session = await self.get_session(task.session_id)
        working_memory = await self.get_working_memory(task.task_id)
        task_spec = await self.get_task_spec(task.task_id)
        return replace(
            checkpoint, workspace_fingerprint=task.project_fingerprint,
            effective_config_hash=configuration.effective_config_hash,
            toolset_hash=effective_toolset_hash(visible_tools),
            prompt_manifest_hash=(configuration.prompt_manifest_hash or ""),
            session_id=session.session_id,
            session_context_hash=session.context_hash,
            working_memory_hash=working_memory.content_hash,
            task_spec_revision=task_spec.revision,
            task_spec_hash=task_spec.content_hash,
            execution_focus={}, legacy_execution_focus=False,
            active_outcome_ids=(), legacy_active_outcome_ids=False,
        )

    async def _save_agent_checkpoint(
        self, checkpoint: AgentTurnCheckpoint, reason: str,
    ) -> None:
        task_spec = await self.get_task_spec(checkpoint.task_id)
        checkpoint = replace(
            checkpoint, task_spec_revision=task_spec.revision,
            task_spec_hash=task_spec.content_hash,
            execution_focus={}, legacy_execution_focus=False,
            active_outcome_ids=(), legacy_active_outcome_ids=False,
        )
        stored = await self._require_stored_task(checkpoint.task_id)
        task = TaskSnapshot.from_data(stored.data)
        updated = task.with_agent_checkpoint(checkpoint.to_data())
        event = RuntimeEvent(
            f"evt-{uuid4().hex}", checkpoint.task_id,
            stored.last_event_sequence + 1, "checkpoint.saved",
            {
                "turn_id": checkpoint.turn_id,
                "revision": checkpoint.revision,
                "checkpoint_hash": checkpoint.checkpoint_hash,
                "reason": reason,
                "model_calls": checkpoint.model_calls,
                "tool_calls": checkpoint.tool_calls,
                "model_budget_renewal_count": (
                    checkpoint.model_budget_renewal_count
                ),
                "model_budget_total_granted": (
                    checkpoint.model_budget_total_granted
                ),
                "pending_tool_calls": len(checkpoint.pending_tool_calls),
                "tool_batch": (
                    checkpoint.tool_batch.to_data()
                    if checkpoint.tool_batch is not None else None
                ),
                "task_spec_revision": checkpoint.task_spec_revision,
                "task_spec_hash": checkpoint.task_spec_hash,
                "pending_user_action": dict(checkpoint.pending_user_action),
            },
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            checkpoint.task_id, stored.version, updated.to_data(), (event,)
        ))

    async def _complete_agent_checkpoint(
        self, task_id: str, turn_id: str, model_calls: int, tool_calls: int,
    ) -> None:
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        updated = task.with_agent_checkpoint(None)
        event = RuntimeEvent(
            f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
            "turn.completed",
            {"turn_id": turn_id, "model_calls": model_calls,
             "tool_calls": tool_calls},
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, updated.to_data(), (event,)
        ))

    async def _validate_agent_checkpoint(
        self, task: TaskSnapshot, checkpoint: AgentTurnCheckpoint,
        visible_tools: tuple[ToolSpec, ...],
    ) -> None:
        decision = await self._evaluate_checkpoint_compatibility(
            task, checkpoint, visible_tools
        )
        if decision.action is CheckpointCompatibilityAction.EXACT_RESUME:
            return
        await self._mark_checkpoint_conflict(
            task.task_id, checkpoint, decision.conflict_reasons
        )
        raise AgentCheckpointConflict(
            "Agent checkpoint cannot resume because identities changed: "
            + ", ".join(decision.conflict_reasons)
        )

    async def _evaluate_checkpoint_compatibility(
        self, task: TaskSnapshot, checkpoint: AgentTurnCheckpoint,
        visible_tools: tuple[ToolSpec, ...],
    ) -> CheckpointCompatibilityDecision:
        """Collect authoritative facts, then delegate classification.

        Kernel owns identity and execution ledgers. The replaceable policy only
        classifies those facts and can never resume a Tool or mutate a Task.
        """
        differences = await self._agent_checkpoint_conflicts(
            task, checkpoint, visible_tools
        )
        policy = self._dependencies.checkpoint_compatibility_policy
        if policy is None:
            return CheckpointCompatibilityDecision(
                CheckpointCompatibilityAction.EXACT_RESUME
                if not differences else CheckpointCompatibilityAction.BLOCKED,
                "safe_checkpoint_available"
                if not differences else "checkpoint_compatibility_policy_missing",
                conflict_reasons=differences,
            )
        unknown = sum(
            execution.state is ToolCommitState.UNKNOWN_OUTCOME
            and execution.reconciled_outcome is None
            for execution in task.tool_executions.values()
        )
        running_non_idempotent = sum(
            execution.state is ToolCommitState.RUNNING
            and execution.idempotency is ToolIdempotency.NON_IDEMPOTENT
            for execution in task.tool_executions.values()
        )
        return await policy.evaluate(CheckpointCompatibilityProbe(
            differences=differences,
            reconcilable_differences=(
                await self._reconcilable_checkpoint_differences(
                    task, checkpoint, differences
                )
            ),
            committed_pending_tool_count=sum(
                1 for call in checkpoint.pending_tool_calls
                if (execution := task.tool_executions.get(
                    ToolExecutionRecord.identity(checkpoint.turn_id, call.call_id)
                )) is not None
                and execution.call == call
                and execution.result is not None
                and execution.state in {
                    ToolCommitState.COMMITTED, ToolCommitState.FAILED,
                    ToolCommitState.CANCELLED,
                }
            ),
            pending_tool_call_count=len(checkpoint.pending_tool_calls),
            tool_execution_count=len(task.tool_executions),
            unknown_outcome_count=unknown,
            running_non_idempotent_count=running_non_idempotent,
            mutation_count=len(task.mutation_journal),
            background_process_count=len(task.background_processes),
        ))

    async def _reconcilable_checkpoint_differences(
        self, task: TaskSnapshot, checkpoint: AgentTurnCheckpoint,
        differences: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Prove stale projections are a one-way Event Log advance.

        Only Evidence generated by an exact terminal Tool call that is still
        pending in the checkpoint is refreshable. Identity, configuration,
        workspace, and unknown-outcome differences remain conflicts.
        """
        if "evidence_question_state" not in differences:
            return ()
        terminal_calls = {
            call.call_id
            for call in checkpoint.pending_tool_calls
            if (execution := task.tool_executions.get(
                ToolExecutionRecord.identity(checkpoint.turn_id, call.call_id)
            )) is not None
            and execution.call == call
            and execution.result is not None
            and execution.state in {
                ToolCommitState.COMMITTED, ToolCommitState.FAILED,
                ToolCommitState.CANCELLED,
            }
        }
        if not terminal_calls:
            return ()
        try:
            stale = EvidenceQuestionProjection.from_data(
                task.task_id, checkpoint.evidence_question_state
            )
            current = await self.get_evidence_questions(task.task_id)
        except (KeyError, TypeError, ValueError):
            return ()
        stale_by_id = {record.question_id: record for record in stale.records}
        current_by_id = {record.question_id: record for record in current.records}
        if not set(stale_by_id).issubset(current_by_id):
            return ()
        advanced = False
        for question_id, old in stale_by_id.items():
            live = current_by_id[question_id]
            if (
                old.question != live.question
                or old.source_task_id != live.source_task_id
                or old.source_turn_id != live.source_turn_id
                or old.expected_scope != live.expected_scope
                or old.revision > live.revision
                or old.updated_event_sequence > live.updated_event_sequence
                or not set(old.tool_call_ids).issubset(live.tool_call_ids)
                or not set(old.evidence_references).issubset(
                    live.evidence_references
                )
            ):
                return ()
            if old != live:
                if not terminal_calls.intersection(live.tool_call_ids):
                    return ()
                advanced = True
        for question_id, live in current_by_id.items():
            if question_id in stale_by_id:
                continue
            if not terminal_calls.intersection(live.tool_call_ids):
                return ()
            advanced = True
        return ("evidence_question_state",) if advanced else ()

    async def _reconcile_agent_checkpoint(
        self, task: TaskSnapshot, checkpoint: AgentTurnCheckpoint,
        visible_tools: tuple[ToolSpec, ...],
        decision: CheckpointCompatibilityDecision,
    ) -> AgentTurnCheckpoint:
        """Refresh execution state from committed Event and Tool ledgers.

        This does not invoke a provider. Exact terminal results are represented
        to the model once, removed from pending work, and audited as reused.
        """
        if decision.action not in {
            CheckpointCompatibilityAction.RECONCILE_REQUIRED,
            CheckpointCompatibilityAction.EXACT_RESUME,
        }:
            raise AgentCheckpointConflict(
                "checkpoint compatibility policy did not allow reconciliation"
            )
        messages = list(checkpoint.messages)
        represented_call_ids = {
            block.result.call_id
            for message in messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        }
        remaining: list[ToolCall] = []
        reconciled: list[ToolExecutionRecord] = []
        for call in checkpoint.pending_tool_calls:
            execution = task.tool_executions.get(
                ToolExecutionRecord.identity(checkpoint.turn_id, call.call_id)
            )
            if (
                execution is None or execution.call != call
                or execution.result is None
                or execution.state not in {
                    ToolCommitState.COMMITTED, ToolCommitState.FAILED,
                    ToolCommitState.CANCELLED,
                }
            ):
                remaining.append(call)
                continue
            reconciled.append(execution)
            if call.call_id not in represented_call_ids:
                messages.append(Message(
                    f"msg-tool-reconciled-{canonical_hash(call.call_id)[:16]}",
                    MessageRole.TOOL, (ToolResultBlock(execution.result),),
                ))
                represented_call_ids.add(call.call_id)
        if remaining:
            raise AgentCheckpointConflict(
                "checkpoint reconciliation found unresolved pending tool calls"
            )
        safe_runtime_differences = {
            "session_context_hash", "effective_config_hash",
            "prompt_manifest_hash", "model_configuration",
            "provider_capabilities_hash", "policy_hash",
            "adapter_lock_hash", "toolset_hash",
        }
        runtime_rebind = bool(
            safe_runtime_differences.intersection(decision.conflict_reasons)
        )
        current = task
        if runtime_rebind:
            stored = await self._require_stored_task(task.task_id)
            current = TaskSnapshot.from_data(stored.data)
            configuration = await self._build_effective_configuration(
                current, len(current.effective_configurations) + 1
            )
            current = current.with_effective_configuration(configuration)
            await self._dependencies.store.commit(RuntimeUnitOfWork(
                current.task_id, stored.version, current.to_data(),
                (RuntimeEvent(
                    f"evt-{uuid4().hex}", current.task_id,
                    stored.last_event_sequence + 1, "config.reconciled", {
                        "revision": configuration.revision,
                        "effective_config_hash": (
                            configuration.effective_config_hash
                        ),
                        "reason": decision.reason_code,
                        "rebind_reasons": list(decision.conflict_reasons),
                    },
                ),),
            ))
            generated_prefixes = (
                "project-instructions-context-", "task-spec-context-",
                "project-onboarding-context-", "project-memory-context-",
                "session-context-", "document-references-context-", "working-memory-context-",
                "task-contract-context-",
            )
            messages = [
                message for message in messages
                if not message.message_id.startswith(generated_prefixes)
            ]
            messages.extend(await self._project_context_messages(current.task_id))
        question_projection = await self.get_evidence_questions(task.task_id)
        evidence_inventory = await self._rebuild_evidence_inventory(
            task.task_id, checkpoint.turn_id
        )
        terminal_tool_calls = sum(
            execution.turn_id == checkpoint.turn_id
            and execution.result is not None
            and execution.state in {
                ToolCommitState.COMMITTED, ToolCommitState.FAILED,
                ToolCommitState.CANCELLED, ToolCommitState.UNKNOWN_OUTCOME,
            }
            for execution in task.tool_executions.values()
        )
        configuration = current.effective_configurations[-1]
        session = await self.get_session(current.session_id)
        working_memory = await self.get_working_memory(current.task_id)
        refreshed = replace(
            checkpoint, revision=checkpoint.revision + 1,
            messages=tuple(messages), pending_tool_calls=tuple(remaining),
            tool_batch=(
                checkpoint.tool_batch.with_pending(tuple(remaining))
                if checkpoint.tool_batch is not None else None
            ),
            tool_calls=max(checkpoint.tool_calls, terminal_tool_calls),
            evidence_inventory=evidence_inventory,
            evidence_question_state=question_projection.to_data(),
            effective_config_hash=configuration.effective_config_hash,
            prompt_manifest_hash=(configuration.prompt_manifest_hash or ""),
            toolset_hash=effective_toolset_hash(visible_tools),
            session_context_hash=session.context_hash,
            working_memory_hash=working_memory.content_hash,
            action_progress={} if runtime_rebind else checkpoint.action_progress,
            read_hits_state={} if runtime_rebind else checkpoint.read_hits_state,
            artifact_read_state=(
                {} if runtime_rebind else checkpoint.artifact_read_state
            ),
            progressive_scope_state=(
                {} if runtime_rebind else checkpoint.progressive_scope_state
            ),
            exploration_budget_state=(
                {} if runtime_rebind else checkpoint.exploration_budget_state
            ),
            stop_or_pivot_state=(
                {} if runtime_rebind else checkpoint.stop_or_pivot_state
            ),
            evidence_relation_state=(
                {} if runtime_rebind else checkpoint.evidence_relation_state
            ),
            rejection_loop_state=(
                {} if runtime_rebind else checkpoint.rejection_loop_state
            ),
            exploration_outcome_state=(
                {} if runtime_rebind else checkpoint.exploration_outcome_state
            ),
            completion_readiness_state=(
                {} if runtime_rebind else checkpoint.completion_readiness_state
            ),
        )
        await self._save_agent_checkpoint(refreshed, "event-log-reconciled")
        for execution in reconciled:
            await self._record_tool_result_reused(
                task.task_id, checkpoint.turn_id, execution
            )
        await self._append_events(task.task_id, ((
            "checkpoint.reconciled", {
                "turn_id": checkpoint.turn_id,
                "previous_revision": checkpoint.revision,
                "revision": refreshed.revision,
                "reason_code": decision.reason_code,
                "refreshed_projections": list(decision.conflict_reasons),
                "reused_execution_ids": [
                    execution.execution_id for execution in reconciled
                ],
                "tool_calls_replayed": False,
                "runtime_rebound": runtime_rebind,
            },
        ),))
        return refreshed

    async def _agent_checkpoint_conflicts(
        self, task: TaskSnapshot, checkpoint: AgentTurnCheckpoint,
        visible_tools: tuple[ToolSpec, ...],
    ) -> tuple[str, ...]:
        """Inspect checkpoint compatibility without mutating Task state."""
        conflicts: list[str] = []
        if checkpoint.task_id != task.task_id:
            conflicts.append("task_id")
        if task.trust_subject != self._dependencies.local_identity.current_subject():
            conflicts.append("local_subject")
        if checkpoint.session_id != task.session_id:
            conflicts.append("session_id")
        else:
            try:
                session = await self.get_session(task.session_id)
            except (LookupError, PermissionError):
                conflicts.append("session_missing")
            else:
                if session.state is not SessionState.ACTIVE:
                    conflicts.append("session_state")
                if checkpoint.session_context_hash != session.context_hash:
                    conflicts.append("session_context_hash")
        working_memory = await self.get_working_memory(task.task_id)
        if checkpoint.working_memory_hash != working_memory.content_hash:
            conflicts.append("working_memory_hash")
        question_projection = await self.get_evidence_questions(task.task_id)
        if (
            checkpoint.evidence_question_state
            and dict(checkpoint.evidence_question_state)
            != question_projection.to_data()
        ):
            conflicts.append("evidence_question_state")
        steering = await self.get_steering(task.task_id)
        if checkpoint.last_steering_inbound_sequence > steering.latest_inbound_sequence:
            conflicts.append("steering_inbound_sequence")
        current_fingerprint = workspace_fingerprint(
            Path(task.workspace), self._dependencies.workspace_path
        )
        if (
            not checkpoint.workspace_fingerprint
            or checkpoint.workspace_fingerprint != current_fingerprint
        ):
            conflicts.append("workspace_fingerprint")
        if not task.effective_configurations:
            conflicts.append("effective_configuration_missing")
        else:
            configuration = task.effective_configurations[-1]
            if checkpoint.effective_config_hash != configuration.effective_config_hash:
                conflicts.append("effective_config_hash")
            if checkpoint.prompt_manifest_hash != (
                configuration.prompt_manifest_hash or ""
            ):
                conflicts.append("prompt_manifest_hash")
            live = await self._build_effective_configuration(
                task, configuration.revision
            )
            if dict(live.model) != dict(configuration.model):
                conflicts.append("model_configuration")
            if (
                live.provider_capabilities_hash
                != configuration.provider_capabilities_hash
            ):
                conflicts.append("provider_capabilities_hash")
            if live.policy_hash != configuration.policy_hash:
                conflicts.append("policy_hash")
            if live.adapter_lock_hash != configuration.adapter_lock_hash:
                conflicts.append("adapter_lock_hash")
        if checkpoint.toolset_hash != effective_toolset_hash(visible_tools):
            conflicts.append("toolset_hash")
        return tuple(dict.fromkeys(conflicts))

    async def _mark_checkpoint_conflict(
        self, task_id: str, checkpoint: AgentTurnCheckpoint,
        conflicts: tuple[str, ...],
    ) -> None:
        stored = await self._require_stored_task(task_id)
        current = TaskSnapshot.from_data(stored.data)
        if current.state is TaskState.CONFLICT:
            return
        if TaskState.CONFLICT not in LEGAL_TRANSITIONS[current.state]:
            raise AgentCheckpointConflict(
                f"checkpoint conflict cannot transition from {current.state.value}"
            )
        conflicted = current.transition(TaskState.CONFLICT)
        events = (
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
                "checkpoint.conflict",
                {"turn_id": checkpoint.turn_id,
                 "checkpoint_hash": checkpoint.checkpoint_hash,
                 "conflicts": list(conflicts)},
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 2,
                "task.state_changed",
                {"previous_state": current.state.value,
                 "next_state": conflicted.state.value,
                 "reason": "Agent checkpoint identity conflict"},
            ),
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, conflicted.to_data(), events
        ))

    async def _rebase_agent_checkpoint(
        self, task: TaskSnapshot, checkpoint: AgentTurnCheckpoint,
        visible_tools: tuple[ToolSpec, ...],
        decision: CheckpointCompatibilityDecision,
    ) -> AgentTurnCheckpoint:
        """Rebuild dynamic context after a proven zero-side-effect upgrade."""
        if decision.action is not CheckpointCompatibilityAction.REBASE_REQUIRED:
            raise AgentCheckpointConflict(
                "Agent checkpoint compatibility policy did not allow rebase"
            )
        if checkpoint.pending_tool_calls or task.tool_executions:
            raise AgentCheckpointConflict(
                "Agent checkpoint with Tool execution state cannot be rebased"
            )
        stored = await self._require_stored_task(task.task_id)
        current = TaskSnapshot.from_data(stored.data)
        configuration = await self._build_effective_configuration(
            current, len(current.effective_configurations) + 1
        )
        updated = current.with_effective_configuration(configuration)
        event = RuntimeEvent(
            f"evt-{uuid4().hex}", current.task_id,
            stored.last_event_sequence + 1, "config.rebased", {
                "revision": configuration.revision,
                "effective_config_hash": configuration.effective_config_hash,
                "reason": decision.reason_code,
                "rebase_reasons": list(decision.rebase_reasons),
            },
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            current.task_id, stored.version, updated.to_data(), (event,)
        ))
        current = updated
        configuration = current.effective_configurations[-1]
        session = await self.get_session(current.session_id)
        refreshed_project_context = await self._project_context_messages(
            current.task_id
        )
        generated_prefixes = (
            "project-instructions-context-", "task-spec-context-",
            "project-onboarding-context-", "project-memory-context-",
            "session-context-", "working-memory-context-", "task-contract-context-",
        )
        messages = tuple(
            message for message in checkpoint.messages
            if not message.message_id.startswith(generated_prefixes)
        )
        messages += refreshed_project_context
        working_memory = await self.get_working_memory(current.task_id)
        rebased = replace(
            checkpoint, revision=checkpoint.revision + 1, messages=messages,
            pending_tool_calls=(), seen_call_ids=(),
            model_calls=0, tool_calls=0, input_tokens=0, output_tokens=0,
            max_model_calls=self._dependencies.default_max_model_calls,
            max_tool_calls=self._dependencies.default_max_tool_calls,
            effective_config_hash=configuration.effective_config_hash,
            prompt_manifest_hash=(configuration.prompt_manifest_hash or ""),
            toolset_hash=effective_toolset_hash(visible_tools),
            session_context_hash=session.context_hash,
            working_memory_hash=working_memory.content_hash,
            action_progress={}, read_hits_state={}, artifact_read_state={},
            progressive_scope_state={}, exploration_budget_state={},
            stop_or_pivot_state={}, evidence_relation_state={},
            rejection_loop_state={}, exploration_outcome_state={},
            completion_readiness_state={},
        )
        await self._save_agent_checkpoint(rebased, "turn-rebased")
        await self._append_events(current.task_id, (("turn.rebased", {
            "turn_id": checkpoint.turn_id,
            "previous_revision": checkpoint.revision,
            "revision": rebased.revision,
            "conflicts": list(decision.rebase_reasons),
            "reason_code": decision.reason_code,
            "tool_calls_replayed": False,
        }),))
        return rebased

    @staticmethod
    def _compact_continuation_messages(
        messages: tuple[Message, ...], user_text: str, input_id: str,
    ) -> tuple[Message, ...]:
        """Keep execution evidence but discard stale assistant conclusions.

        A continuation resumes one Task safely; it must not make earlier blocker
        prose look like durable evidence. Tool-call messages and ToolResult
        messages remain paired and intact for protocol recovery.

        When the history still holds retryable tool failures, the continuation
        says so and asks for the retry by name. The generic "continue from
        durable ToolResults" wording let a model answer the same way again, so
        the Task parked, the user pressed 继续, and nothing changed
        (``task-a20b6734f28b4870be666e7c14561e76``).
        """
        from .kernel import Kernel

        retained = [
            message for message in messages
            if not message.message_id.startswith("completion-readiness-")
            and not (
                message.role is MessageRole.ASSISTANT
                and not any(
                    isinstance(block, ToolCallBlock)
                    for block in message.content
                )
            )
        ]
        payload: dict[str, Any] = {
            "boundary": "user_continuation",
            "instruction": (
                "Continue the current Task from durable ToolResults and "
                "workspace facts. Prior assistant prose is not evidence and "
                "must not be repeated as a conclusion."
            ),
            "text": user_text,
        }
        recovery_batch = Kernel._latest_recoverable_tool_batch(messages)
        if recovery_batch:
            payload["instruction"] = (
                "Continue the current Task from durable ToolResults and "
                "workspace facts. These tool calls did not complete and are "
                "retryable: call the same tool again with corrected arguments "
                "now, or state concretely why it cannot be retried. Prior "
                "assistant prose is not evidence and must not be repeated as a "
                "conclusion."
            )
            payload["unresolved_tool_failures"] = [
                {
                    "call_id": result.call_id,
                    "error_code": result.error_code,
                    "message": result.message,
                    "recovery_kind": result.effective_recovery_kind.value,
                    "recovery_action": dict(result.recovery_action),
                }
                for result in recovery_batch
            ]
        retained.append(Message(
            f"continuation-input-{input_id}", MessageRole.USER,
            (TextBlock(json.dumps(
                payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )),),
            scaffolding="continuation-input",
        ))
        return tuple(retained)

    async def _last_continuation_pending(
        self, task_id: str,
    ) -> Mapping[str, Any]:
        """Return the most recently persisted continuation boundary, if any."""
        events = await self._dependencies.store.read_events(task_id)
        for event in reversed(events):
            if event.event_type == "continuation.requested":
                return event.payload
        return {}
