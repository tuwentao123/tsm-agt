"""Event-projected Goal/Contract lifecycle owned by the existing Task aggregate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any
from uuid import uuid4

from tsm_agt.ports import RuntimeEvent, RuntimeUnitOfWork
from tsm_agt.ports.evidence import ContractAuthority, ContractPhase, TaskContract

from .configuration import canonical_hash
from .task import InvalidTaskTransition, TaskNotFound, TaskSnapshot


_CONTRACT_EVENTS = frozenset({
    "contract.drafted",
    "contract.created_by_user",
    "contract.accepted_by_user",
    "contract.updated_by_user",
    "contract.cleared",
    "contract.draft_discarded",
})


class TaskContractProjector:
    """Replays contract snapshots without adding a second Task lifecycle."""

    @staticmethod
    def project(events: Sequence[RuntimeEvent]) -> TaskContract | None:
        contract: TaskContract | None = None
        for event in events:
            if event.event_type == "task.created":
                raw = event.payload.get("evidence_contract")
            elif event.event_type in _CONTRACT_EVENTS:
                raw = event.payload.get("snapshot")
            else:
                continue
            if not isinstance(raw, Mapping):
                continue
            contract = TaskContract.from_data(raw)
        return contract


class ContractMixin:
    """Versioned Contract commands sharing Task storage and optimistic commits."""

    async def get_task_contract(self, task_id: str) -> TaskContract | None:
        await self.get_task(task_id)
        return TaskContractProjector.project(
            await self._dependencies.store.read_events(task_id)
        )

    async def draft_task_contract(
        self,
        task_id: str,
        contract: TaskContract,
        *,
        expected_revision: int,
        command_id: str,
    ) -> TaskContract:
        return await self._write_contract(
            task_id,
            contract,
            expected_revision=expected_revision,
            command_id=command_id,
            event_type="contract.drafted",
            authority=ContractAuthority.MODEL_DRAFT,
            phase=ContractPhase.DRAFT,
        )

    async def accept_task_contract(
        self,
        task_id: str,
        contract_id: str,
        *,
        expected_revision: int,
        command_id: str,
    ) -> TaskContract:
        current = await self.get_task_contract(task_id)
        if current is None or current.phase is not ContractPhase.DRAFT:
            raise ValueError("only a current draft contract can be accepted")
        if current.contract_id != contract_id:
            raise ValueError("contract_id does not match the current draft")
        return await self._write_contract(
            task_id,
            replace(
                current,
                authority=ContractAuthority.USER_EXPLICIT,
                phase=ContractPhase.ACTIVE,
            ),
            expected_revision=expected_revision,
            command_id=command_id,
            event_type="contract.accepted_by_user",
            authority=ContractAuthority.USER_EXPLICIT,
            phase=ContractPhase.ACTIVE,
        )

    async def create_or_update_task_contract(
        self,
        task_id: str,
        contract: TaskContract,
        *,
        expected_revision: int,
        command_id: str,
    ) -> TaskContract:
        current = await self.get_task_contract(task_id)
        event_type = (
            "contract.created_by_user" if current is None or current.phase is ContractPhase.CLEARED
            else "contract.updated_by_user"
        )
        if current is not None and current.phase is ContractPhase.ACTIVE:
            if contract.contract_id and contract.contract_id != current.contract_id:
                raise ValueError("contract_id does not match the active contract")
        return await self._write_contract(
            task_id,
            contract,
            expected_revision=expected_revision,
            command_id=command_id,
            event_type=event_type,
            authority=ContractAuthority.USER_EXPLICIT,
            phase=ContractPhase.ACTIVE,
            superseded=current if event_type == "contract.updated_by_user" else None,
        )

    async def clear_task_contract(
        self,
        task_id: str,
        contract_id: str,
        *,
        expected_revision: int,
        command_id: str,
    ) -> TaskContract:
        current = await self.get_task_contract(task_id)
        if current is None or current.contract_id != contract_id:
            raise ValueError("contract_id does not match the current contract")
        return await self._write_contract(
            task_id,
            replace(current, phase=ContractPhase.CLEARED),
            expected_revision=expected_revision,
            command_id=command_id,
            event_type=(
                "contract.draft_discarded"
                if current.phase is ContractPhase.DRAFT
                else "contract.cleared"
            ),
            authority=current.authority,
            phase=ContractPhase.CLEARED,
        )

    async def _write_contract(
        self,
        task_id: str,
        contract: TaskContract,
        *,
        expected_revision: int,
        command_id: str,
        event_type: str,
        authority: ContractAuthority,
        phase: ContractPhase,
        superseded: TaskContract | None = None,
    ) -> TaskContract:
        if expected_revision < 0 or not command_id.strip():
            raise ValueError("expected_revision must be non-negative and command_id must not be empty")
        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        task = TaskSnapshot.from_data(stored.data)
        if task.state.is_terminal:
            raise InvalidTaskTransition(task.state, task.state)
        events = await self._dependencies.store.read_events(task_id)
        current = TaskContractProjector.project(events)
        contract_id = contract.contract_id or (
            current.contract_id if current is not None else f"contract-{uuid4().hex}"
        )
        candidate = replace(
            contract,
            contract_id=contract_id,
            revision=(current.revision if current is not None else 0) + 1,
            authority=authority,
            phase=phase,
        )
        request_hash = canonical_hash({
            "event_type": event_type,
            "expected_revision": expected_revision,
            "contract": candidate.to_data(),
        })
        previous = next((
            event for event in events
            if event.event_type in _CONTRACT_EVENTS
            and event.payload.get("command_id") == command_id
        ), None)
        if previous is not None:
            if previous.payload.get("request_hash") != request_hash:
                raise ValueError("contract command_id was reused with different content")
            snapshot = previous.payload.get("snapshot")
            if not isinstance(snapshot, Mapping):
                raise ValueError("stored contract command is malformed")
            return TaskContract.from_data(snapshot)
        actual_revision = current.revision if current is not None else 0
        if actual_revision != expected_revision:
            raise ValueError(
                f"contract revision conflict: expected {expected_revision}, got {actual_revision}"
            )
        payload: dict[str, Any] = {
            "command_id": command_id,
            "request_hash": request_hash,
            "content_hash": canonical_hash(candidate.to_data()),
            "snapshot": candidate.to_data(),
        }
        if superseded is not None:
            payload["superseded"] = replace(
                superseded, phase=ContractPhase.SUPERSEDED
            ).to_data()
        event = RuntimeEvent(
            f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
            event_type, payload,
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, task.to_data(), (event,)
        ))
        return candidate

    async def suspend_for_contract_gate(
        self,
        task_id: str,
        turn_id: str,
        checkpoint_data: Mapping[str, Any],
        contract: TaskContract,
        decision: Any,
    ) -> tuple[Any, str]:
        """Persist a Contract BLOCKED clarification without inventing a Task state."""
        from datetime import datetime, timedelta, timezone
        import secrets

        from .clarification import ClarificationKind, ClarificationRequest
        from .turn import InvalidTurnState
        from tsm_agt.ports import ToolCall

        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        if task.state.value != "EXECUTING" or task.pending_clarification is not None:
            raise InvalidTurnState("contract gate can only block an executing Task")
        token = secrets.token_urlsafe(32)
        created = datetime.now(timezone.utc)
        payload = {
            "blocked_reason": decision.reason_code,
            "missing_requirement_ids": list(decision.missing_requirement_ids),
            "required_capability_ids": list(decision.required_capability_ids),
            "recovery_actions": [item.to_data() for item in decision.recovery_actions],
            "contract_revision": contract.revision,
            "contract_id": contract.contract_id,
        }
        request = ClarificationRequest(
            request_id=f"clarification-{uuid4().hex}",
            task_id=task_id,
            turn_id=turn_id,
            call=ToolCall(f"contract-gate-{uuid4().hex}", "core.request_input", {}),
            question="合同要求的证据能力当前不可用。请调整合同、提供来源或启用能力后继续。",
            choices=(),
            reason=decision.detail or decision.reason_code,
            required=True,
            resume_token_hash=ClarificationRequest.hash_resume_token(token),
            created_at=created,
            expires_at=created + timedelta(hours=24),
            kind=ClarificationKind.CONTRACT_BLOCKED,
            payload=payload,
        )
        checkpoint = dict(checkpoint_data)
        checkpoint["pending_user_action"] = {
            "kind": "CONTRACT_BLOCKED",
            "request_id": request.request_id,
            "contract_id": contract.contract_id,
            "contract_revision": contract.revision,
        }
        awaiting = task.await_clarification(request).with_agent_checkpoint(checkpoint)
        events = (
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
                "clarification.requested", {
                    "request_id": request.request_id,
                    "turn_id": turn_id,
                    "kind": request.kind.value,
                    "reason": decision.reason_code,
                    "contract_revision": contract.revision,
                    "missing_requirement_ids": list(decision.missing_requirement_ids),
                    "recovery_actions": [item.to_data() for item in decision.recovery_actions],
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 2,
                "checkpoint.saved", {
                    "turn_id": turn_id,
                    "revision": checkpoint.get("revision"),
                    "reason": "contract-gate-blocked",
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 3,
                "task.state_changed", {
                    "previous_state": task.state.value,
                    "next_state": awaiting.state.value,
                    "reason": "contract gate requires user action",
                },
            ),
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, awaiting.to_data(), events
        ))
        return request, token

    async def resume_contract_gate(
        self,
        task_id: str,
        *,
        on_text_delta: Any = None,
        on_progress: Any = None,
    ) -> Any:
        """Release a Contract gate only after an explicit typed Contract action."""
        from .clarification import ClarificationKind, ClarificationNotPending

        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        request = task.pending_clarification
        if request is None or request.kind is not ClarificationKind.CONTRACT_BLOCKED:
            raise ClarificationNotPending("task is not waiting on a contract gate")
        if task.active_agent_checkpoint is None:
            raise ClarificationNotPending("contract gate lost its checkpoint")
        checkpoint = dict(task.active_agent_checkpoint)
        checkpoint["pending_user_action"] = {}
        resumed = task.resolve_clarification().with_agent_checkpoint(checkpoint)
        events = (
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 1,
                "clarification.resolved", {
                    "request_id": request.request_id,
                    "turn_id": request.turn_id,
                    "resolution": "contract_action",
                },
            ),
            RuntimeEvent(
                f"evt-{uuid4().hex}", task_id, stored.last_event_sequence + 2,
                "task.state_changed", {
                    "previous_state": task.state.value,
                    "next_state": resumed.state.value,
                    "reason": "contract action completed",
                },
            ),
        )
        await self._dependencies.store.commit(RuntimeUnitOfWork(
            task_id, stored.version, resumed.to_data(), events
        ))
        return await self.resume_checkpointed_agent_turn(
            task_id, on_text_delta=on_text_delta, on_progress=on_progress
        )
