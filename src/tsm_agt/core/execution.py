"""Persistent tool execution ledger owned by the microkernel."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from tsm_agt.ports import ToolCall, ToolIdempotency, ToolResult, ToolRisk


class ToolCommitState(StrEnum):
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    execution_id: str
    turn_id: str
    invocation_id: str
    call: ToolCall
    payload_hash: str
    policy_decision_id: str
    effective_risk: ToolRisk
    approval_request_id: str | None
    idempotency: ToolIdempotency
    idempotency_key: str | None
    state: ToolCommitState
    result: ToolResult | None
    started_at: datetime
    updated_at: datetime
    reconciled_outcome: str | None = None
    reconciliation_ref: str | None = None
    reconciled_at: datetime | None = None

    @classmethod
    def start(
        cls,
        *,
        task_id: str,
        turn_id: str,
        invocation_id: str,
        call: ToolCall,
        payload_hash: str,
        policy_decision_id: str,
        effective_risk: ToolRisk,
        approval_request_id: str | None,
        idempotency: ToolIdempotency,
    ) -> ToolExecutionRecord:
        now = datetime.now(timezone.utc)
        execution_id = cls.identity(turn_id, call.call_id)
        key = (
            cls.make_idempotency_key(task_id, turn_id, call.call_id, payload_hash)
            if idempotency is ToolIdempotency.KEYED
            else None
        )
        return cls(
            execution_id=execution_id,
            turn_id=turn_id,
            invocation_id=invocation_id,
            call=call,
            payload_hash=payload_hash,
            policy_decision_id=policy_decision_id,
            effective_risk=effective_risk,
            approval_request_id=approval_request_id,
            idempotency=idempotency,
            idempotency_key=key,
            state=ToolCommitState.PREPARED,
            result=None,
            started_at=now,
            updated_at=now,
        )

    @staticmethod
    def identity(turn_id: str, call_id: str) -> str:
        return f"{turn_id}:{call_id}"

    @staticmethod
    def make_idempotency_key(
        task_id: str, turn_id: str, call_id: str, payload_hash: str
    ) -> str:
        raw = f"{task_id}\0{turn_id}\0{call_id}\0{payload_hash}".encode()
        return hashlib.sha256(raw).hexdigest()

    def finish(
        self, state: ToolCommitState, result: ToolResult
    ) -> ToolExecutionRecord:
        if state not in {
            ToolCommitState.COMMITTED,
            ToolCommitState.FAILED,
            ToolCommitState.CANCELLED,
            ToolCommitState.UNKNOWN_OUTCOME,
        }:
            raise ValueError(f"invalid terminal tool commit state: {state.value}")
        return replace(
            self,
            state=state,
            result=result,
            updated_at=datetime.now(timezone.utc),
        )

    def mark_running(self) -> ToolExecutionRecord:
        if self.state is not ToolCommitState.PREPARED:
            raise ValueError(
                f"only PREPARED execution can start, got {self.state.value}"
            )
        return replace(
            self,
            state=ToolCommitState.RUNNING,
            updated_at=datetime.now(timezone.utc),
        )

    def reconcile(self, outcome: str, reference: str) -> ToolExecutionRecord:
        """Attach a human-observed fact without rewriting tool history."""
        if self.state is not ToolCommitState.UNKNOWN_OUTCOME:
            raise ValueError("only UNKNOWN_OUTCOME execution can be reconciled")
        normalized = outcome.strip().upper()
        if normalized not in {"SUCCEEDED", "NOT_APPLIED"}:
            raise ValueError("unsupported reconciled tool outcome")
        if not reference.strip():
            raise ValueError("tool reconciliation reference is required")
        now = datetime.now(timezone.utc)
        return replace(
            self, reconciled_outcome=normalized,
            reconciliation_ref=reference.strip(), reconciled_at=now,
            updated_at=now,
        )

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ToolExecutionRecord:
        raw_call = data.get("call")
        raw_result = data.get("result")
        if not isinstance(raw_call, Mapping):
            raise ValueError("stored tool execution call must be an object")
        return cls(
            execution_id=str(data["execution_id"]),
            turn_id=str(data["turn_id"]),
            invocation_id=str(data["invocation_id"]),
            call=ToolCall.from_data(raw_call),
            payload_hash=str(data["payload_hash"]),
            policy_decision_id=str(data["policy_decision_id"]),
            effective_risk=ToolRisk(str(data["effective_risk"])),
            approval_request_id=(
                str(data["approval_request_id"])
                if data.get("approval_request_id") is not None
                else None
            ),
            idempotency=ToolIdempotency(str(data["idempotency"])),
            idempotency_key=(
                str(data["idempotency_key"])
                if data.get("idempotency_key") is not None
                else None
            ),
            state=ToolCommitState(str(data["state"])),
            result=(
                ToolResult.from_data(raw_result)
                if isinstance(raw_result, Mapping)
                else None
            ),
            started_at=datetime.fromisoformat(str(data["started_at"])),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
            reconciled_outcome=(
                str(data["reconciled_outcome"])
                if data.get("reconciled_outcome") is not None else None
            ),
            reconciliation_ref=(
                str(data["reconciliation_ref"])
                if data.get("reconciliation_ref") is not None else None
            ),
            reconciled_at=(
                datetime.fromisoformat(str(data["reconciled_at"]))
                if data.get("reconciled_at") is not None else None
            ),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "turn_id": self.turn_id,
            "invocation_id": self.invocation_id,
            "call": self.call.to_data(),
            "payload_hash": self.payload_hash,
            "policy_decision_id": self.policy_decision_id,
            "effective_risk": self.effective_risk.value,
            "approval_request_id": self.approval_request_id,
            "idempotency": self.idempotency.value,
            "idempotency_key": self.idempotency_key,
            "state": self.state.value,
            "result": self.result.to_data() if self.result is not None else None,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "reconciled_outcome": self.reconciled_outcome,
            "reconciliation_ref": self.reconciliation_ref,
            "reconciled_at": (
                self.reconciled_at.isoformat() if self.reconciled_at else None
            ),
        }


class IdempotencyConflict(ValueError):
    pass


class ToolExecutionInProgress(RuntimeError):
    pass
