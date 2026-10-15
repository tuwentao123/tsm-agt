"""Approval request resolution and persistence for `Kernel`.

A mixin module in the facade-plus-siblings shape (``run_agent.py`` +
``agent/session_persistence.py``): the facade ``core/kernel.py`` imports
this at module level and adds ``ApprovalMixin`` to ``Kernel``'s bases, so
**every method resolves through ``Kernel``'s MRO unchanged** --
``self._dependencies``, ``self._append_events`` and ``Kernel``'s own class
attributes all still resolve. Moved bodies are verbatim: nothing is renamed
and no signature changes.

This module must never bind the facade at module level; late-import it
inside a method instead (the project's module-boundary rule).

Moved from ``core/kernel.py``:

* ``_approval_preview``
* ``_approval_target``
* ``_external_read_root_requiring_approval``
* ``_persist_approval_request``
* ``_resolve_approval``
* ``resolve_agent_approval``
* ``resolve_approval``
"""

from __future__ import annotations


from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from tsm_agt.ports import (
    RuntimeEvent, RuntimeUnitOfWork, ToolResult, is_sensitive_read_path
)
from uuid import uuid4
from .agent_loop import (
    AgentClarificationSuspended, AgentTurnCheckpoint, AgentTurnResult,
    AgentTurnSuspended
)
from .approval import (
    ApprovalDecision, ApprovalKind, ApprovalNotPending,
    ApprovalPayloadMismatch, utc_now
)
from .task import TaskNotFound, TaskSnapshot, TaskState
from .tool import validate_tool_arguments
from .trust import ProjectTrustLevel
from .workspace_access import (
    WorkspaceAccessCapability, WorkspaceAccessGrant
)


class ApprovalMixin:
    async def resolve_agent_approval(
        self, request_id: str, decision: ApprovalDecision, reason: str,
        on_text_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[AgentProgress], None] | None = None,
    ) -> AgentTurnResult | AgentTurnSuspended | AgentClarificationSuspended:
        if not request_id.strip():
            raise ValueError("approval request_id must not be empty")
        stored = await self._dependencies.store.find_task_by_pending_approval(
            request_id
        )
        if stored is None:
            raise ApprovalNotPending(f"approval is not pending: {request_id}")
        task = TaskSnapshot.from_data(stored.data)
        request = task.pending_approval
        if request is None:
            raise ApprovalNotPending(f"approval is not pending: {request_id}")
        return await self.resume_agent_turn(
            task.task_id, request.request_id, request.payload_hash, decision, reason,
            on_text_delta=on_text_delta,
            on_progress=on_progress,
        )

    def _external_read_root_requiring_approval(
        self, task: TaskSnapshot, call: ToolCall,
    ) -> Path | None:
        """Return the narrow external directory that needs Task approval."""
        if call.name not in {
            "core.read_file", "core.list_files", "core.search_files",
            "core.find_files", "core.search_text", "core.grep_search",
        }:
            return None
        raw_path = call.arguments.get("path", ".")
        if not isinstance(raw_path, str):
            return None
        if is_sensitive_read_path(Path(raw_path)):
            return None
        workspace = self._dependencies.workspace_path.normalize_workspace(
            Path(task.workspace)
        )
        approved_roots = tuple(
            Path(grant.canonical_root)
            for grant in task.workspace_access_grants
            if grant.capability is WorkspaceAccessCapability.READ
        )
        try:
            self._dependencies.workspace_path.resolve_read_path(
                workspace, raw_path, approved_roots
            )
            return None
        except PermissionError:
            return None
        except ValueError:
            pass
        return self._dependencies.workspace_path.external_read_approval_root(
            workspace, raw_path
        )

    def _external_write_root_requiring_approval(
        self, task: TaskSnapshot, call: ToolCall,
    ) -> Path | None:
        if call.name not in {
            "core.apply_patch", "core.apply_patch_v4a",
            "core.apply_patches", "core.delete_file",
        }:
            return None
        raw_path = call.arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        workspace = self._dependencies.workspace_path.normalize_workspace(Path(task.workspace))
        try:
            self._dependencies.workspace_path.resolve_mutation_path(workspace, raw_path)
            return None
        except (PermissionError, ValueError):
            return Path(raw_path).expanduser().resolve(strict=False).parent

    async def _persist_approval_request(
        self, stored: Any, task: TaskSnapshot, request: ApprovalRequest,
        policy_event: RuntimeEvent, agent_checkpoint: Mapping[str, Any] | None,
    ) -> None:
        """Persist either tool-action or external-directory approval uniformly."""
        awaiting = task.await_approval(request)
        approval_event = RuntimeEvent(
            f"evt-{uuid4().hex}", task.task_id,
            stored.last_event_sequence + 2, "approval.requested",
            request.to_data(),
        )
        events: tuple[RuntimeEvent, ...] = (policy_event, approval_event)
        sequence = stored.last_event_sequence + 3
        if agent_checkpoint is not None:
            pending_user_action = {
                "kind": "APPROVAL",
                "request_id": request.request_id,
                "task_id": task.task_id, "turn_id": request.turn_id,
                "outcome_ref": request.call.outcome_ref,
            }
            checkpoint_data = replace(
                AgentTurnCheckpoint.from_data(agent_checkpoint),
                pending_user_action=pending_user_action,
            ).to_data()
            awaiting = awaiting.with_agent_checkpoint(checkpoint_data)
            events += (RuntimeEvent(
                f"evt-{uuid4().hex}", task.task_id, sequence,
                "checkpoint.saved", {
                    "turn_id": request.turn_id,
                    "request_id": request.request_id,
                    "revision": agent_checkpoint.get("revision"),
                    "pending_user_action": pending_user_action,
                },
            ),)
            sequence += 1
        events += (RuntimeEvent(
            f"evt-{uuid4().hex}", task.task_id, sequence,
            "task.state_changed", {
                "previous_state": task.state.value,
                "next_state": awaiting.state.value,
                "reason": f"approval required: {request.request_id}",
            },
        ),)
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task.task_id, stored.version, awaiting.to_data(), events
        ))

    async def resolve_approval(
        self,
        task_id: str,
        request_id: str,
        payload_hash: str,
        decision: ApprovalDecision,
        reason: str,
        timeout_seconds: float = 30.0,
    ) -> ToolResult:
        return await self._resolve_approval(
            task_id, request_id, payload_hash, decision, reason,
            timeout_seconds=timeout_seconds,
            allow_agent_checkpoint=False,
            resume_revision=None,
        )

    async def _resolve_approval(
        self,
        task_id: str,
        request_id: str,
        payload_hash: str,
        decision: ApprovalDecision,
        reason: str,
        *,
        timeout_seconds: float,
        allow_agent_checkpoint: bool,
        resume_revision: int | None,
    ) -> ToolResult:
        if not request_id.strip() or not payload_hash.strip():
            raise ValueError("request_id and payload_hash must not be empty")
        if not reason.strip():
            raise ValueError("approval reason must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        task = TaskSnapshot.from_data(stored.data)
        request = task.pending_approval
        if task.state is not TaskState.AWAITING_APPROVAL or request is None:
            raise ApprovalNotPending(f"task {task_id} has no pending approval")
        if request.agent_checkpoint is not None and not allow_agent_checkpoint:
            raise ApprovalNotPending(
                "Agent approval must be resolved through resume_agent_turn"
            )
        if not request.matches(request_id, payload_hash):
            raise ApprovalPayloadMismatch(
                "approval request_id or payload_hash does not match the pending action"
            )
        if request.call.name == "core.run_command":
            current_trust = await self.get_project_trust(Path(task.workspace))
            if (
                current_trust.level is not task.project_trust
                or current_trust.fingerprint != task.project_fingerprint
                or current_trust.subject != task.trust_subject
            ):
                raise ApprovalPayloadMismatch(
                    "project identity or trust changed while approval was pending; "
                    "start a new command request after reviewing the project again"
                )
            if request.execution_identity:
                raw_argv = request.call.arguments.get("argv")
                raw_cwd = request.call.arguments.get("cwd", ".")
                raw_environment = request.call.arguments.get("environment", {})
                if not (
                    isinstance(raw_argv, (list, tuple))
                    and all(isinstance(item, str) for item in raw_argv)
                    and isinstance(raw_cwd, str)
                    and isinstance(raw_environment, Mapping)
                ):
                    raise ApprovalPayloadMismatch(
                        "the approved command no longer has a valid execution identity"
                    )
                try:
                    current_identity = self._prepare_process_execution(
                        task, tuple(raw_argv), raw_cwd, raw_environment,
                    ).approval_identity()
                except (OSError, ValueError) as error:
                    raise ApprovalPayloadMismatch(
                        "the approved command environment changed before execution; "
                        "review a newly prepared command"
                    ) from error
                if dict(request.execution_identity) != current_identity:
                    raise ApprovalPayloadMismatch(
                        "the command launcher, resolved target, or governed environment "
                        "changed after approval; review a newly prepared command"
                    )

        provider, selected_spec = await self._find_tool(request.call.name)
        validate_tool_arguments(selected_spec, request.call.arguments)
        current_decision = self._tool_policy.evaluate(
            selected_spec, request.call, task.project_trust
        )
        policy_mismatch = (
            current_decision.payload_hash != request.payload_hash
            or current_decision.effective_risk is not request.risk
        )
        if request.kind is ApprovalKind.TOOL_ACTION:
            policy_mismatch = policy_mismatch or not current_decision.requires_approval
        elif request.kind in {ApprovalKind.WORKSPACE_READ, ApprovalKind.WORKSPACE_WRITE}:
            try:
                root = self._dependencies.workspace_path.normalize_workspace(
                    Path(request.workspace_access_root)
                )
            except (OSError, ValueError):
                root = None
            is_write = request.kind is ApprovalKind.WORKSPACE_WRITE
            expected_root = (
                self._external_write_root_requiring_approval(task, request.call)
                if is_write else
                self._external_read_root_requiring_approval(task, request.call)
            )
            policy_mismatch = (
                policy_mismatch
                or (not is_write and (
                    not selected_spec.is_read_only
                    or request.call.name not in {
                        "core.read_file", "core.list_files", "core.search_files",
                        "core.find_files", "core.search_text", "core.grep_search",
                    }
                ))
                or (is_write and request.call.name not in {
                    "core.apply_patch", "core.apply_patch_v4a",
                    "core.apply_patches", "core.delete_file",
                })
                or root is None
                or str(root) != request.workspace_access_root
                or expected_root != root
            )
        elif request.kind is ApprovalKind.PROJECT_TRUST:
            # The policy intentionally reports an untrusted command as DENY;
            # this approval is the explicit user-facing escape hatch for that
            # exact gate, not a generic command approval.  The command and
            # workspace must still match the pending request and remain
            # untrusted while it is being reviewed.
            policy_mismatch = (
                policy_mismatch
                or request.call.name != "core.run_command"
                or task.project_trust.allows_project_execution
                or current_decision.action.value != "deny"
                or request.workspace_access_root != str(Path(task.workspace))
            )
        else:
            policy_mismatch = True
        if policy_mismatch:
            raise ApprovalPayloadMismatch(
                "the tool declaration, arguments, or effective risk changed after approval was requested"
            )

        resumed = task.resolve_approval()
        if request.agent_checkpoint is not None and allow_agent_checkpoint:
            resolved_checkpoint = replace(
                AgentTurnCheckpoint.from_data(request.agent_checkpoint),
                revision=(
                    resume_revision
                    if resume_revision is not None else
                    AgentTurnCheckpoint.from_data(
                        request.agent_checkpoint
                    ).revision + 1
                ),
                pending_user_action={},
            )
            resumed = resumed.with_agent_checkpoint(
                resolved_checkpoint.to_data()
            )
        if (
            decision is ApprovalDecision.APPROVE
            and request.kind in {ApprovalKind.WORKSPACE_READ, ApprovalKind.WORKSPACE_WRITE}
        ):
            resumed = resumed.with_workspace_access_grant(WorkspaceAccessGrant(
                grant_id=f"workspace-grant-{uuid4().hex}",
                canonical_root=request.workspace_access_root,
                capability=(
                    WorkspaceAccessCapability.WRITE
                    if request.kind is ApprovalKind.WORKSPACE_WRITE
                    else WorkspaceAccessCapability.READ
                ),
                approval_request_id=request.request_id,
                granted_at=utc_now(),
            ))
        trust_event: RuntimeEvent | None = None
        if (
            decision is ApprovalDecision.APPROVE
            and request.kind is ApprovalKind.PROJECT_TRUST
        ):
            binding = await self.set_project_trust(
                Path(task.workspace), ProjectTrustLevel.TRUSTED_BUILD
            )
            resumed = resumed.with_project_trust(
                binding.level, binding.fingerprint, binding.subject
            )
            trust_event = RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=stored.last_event_sequence + 3,
                event_type="project.trust_granted",
                payload={
                    "workspace": binding.workspace,
                    "level": binding.level.value,
                    "fingerprint": binding.fingerprint,
                    "subject": binding.subject,
                    "approval_request_id": request.request_id,
                },
            )
        resolved_event = RuntimeEvent(
            event_id=f"evt-{uuid4().hex}",
            task_id=task_id,
            sequence=stored.last_event_sequence + 1,
            event_type="approval.resolved",
            payload={
                "request_id": request.request_id,
                "payload_hash": request.payload_hash,
                "outcome_ref": request.call.outcome_ref,
                "decision": decision.value,
                "reason": reason.strip(),
                "pending_user_action": {},
            },
        )
        state_event = RuntimeEvent(
            event_id=f"evt-{uuid4().hex}",
            task_id=task_id,
            sequence=stored.last_event_sequence + 2,
            event_type="task.state_changed",
            payload={
                "previous_state": task.state.value,
                "next_state": resumed.state.value,
                "reason": f"approval {decision.value}: {request.request_id}",
            },
        )
        resolution_events: tuple[RuntimeEvent, ...] = (resolved_event, state_event)
        if trust_event is not None:
            resolution_events += (trust_event,)
        if resume_revision is not None:
            turn_resumed = RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=stored.last_event_sequence + len(resolution_events) + 1,
                event_type="turn.resumed",
                payload={
                    "turn_id": request.turn_id,
                    "previous_revision": resume_revision - 1,
                    "revision": resume_revision,
                    "approval_request_id": request.request_id,
                    "decision": decision.value,
                },
            )
            resolution_events += (turn_resumed,)
        if decision is ApprovalDecision.DENY:
            denied = ToolResult(
                call_id=request.call.call_id,
                ok=False,
                error_code="PERMISSION_DENIED",
                message="user denied the requested tool action",
                hint=reason.strip(),
                meta={
                    "approval_request_id": request.request_id,
                    "payload_hash": request.payload_hash,
                },
            )
            failed_event = RuntimeEvent(
                event_id=f"evt-{uuid4().hex}",
                task_id=task_id,
                sequence=stored.last_event_sequence + len(resolution_events) + 1,
                event_type="tool.failed",
                payload={
                    "turn_id": request.turn_id,
                    "invocation_id": request.invocation_id,
                    "outcome_ref": request.call.outcome_ref,
                    "result": denied.to_data(),
                },
            )
            await self._dependencies.store.commit(
                RuntimeUnitOfWork(
                    task_id=task_id,
                    expected_version=stored.version,
                    next_state=resumed.to_data(),
                    events=resolution_events + (failed_event,),
                )
            )
            return denied

        return await self._execute_authorized_tool(
            task_id=task_id,
            turn_id=request.turn_id,
            call=request.call,
            provider=provider,
            selected_spec=selected_spec,
            invocation_id=request.invocation_id,
            policy_decision_id=request.policy_decision_id,
            effective_risk=request.risk,
            payload_hash=request.payload_hash,
            timeout_seconds=timeout_seconds,
            approval_request_id=request.request_id,
            initial_events=resolution_events,
            next_state=resumed.to_data(),
        )

    @staticmethod
    def _approval_target(call: ToolCall) -> str:
        mutation_groups = call.arguments.get("mutation_groups")
        if isinstance(mutation_groups, (list, tuple)):
            return f"mutation_groups={mutation_groups}"
        patches = call.arguments.get("patches")
        if isinstance(patches, (list, tuple)):
            paths = [
                item.get("path") for item in patches
                if isinstance(item, Mapping) and item.get("path") is not None
            ]
            if paths:
                return f"paths={paths}"
        for key in (
            "path", "mutation_id", "mutation_ids", "target", "command",
            "url", "name"
        ):
            value = call.arguments.get(key)
            if value is not None:
                return f"{key}={value}"
        return "arguments supplied in the approval payload"

    def _approval_preview(self, call: ToolCall) -> str:
        return self._present_tool_arguments(call.name, call.arguments).text
