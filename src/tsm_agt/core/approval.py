"""Approval records owned by the microkernel, not by tool adapters."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from tsm_agt.ports import ToolCall, ToolRisk


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    DENY = "deny"


class ApprovalKind(StrEnum):
    TOOL_ACTION = "tool_action"
    WORKSPACE_READ = "workspace_read"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request_id: str
    task_id: str
    turn_id: str
    invocation_id: str
    policy_decision_id: str
    payload_hash: str
    risk: ToolRisk
    call: ToolCall
    action: str
    target: str
    preview: str
    network_access: str
    data_transmission: str
    rollback: str
    created_at: datetime
    agent_checkpoint: Mapping[str, Any] | None = None
    kind: ApprovalKind = ApprovalKind.TOOL_ACTION
    workspace_access_root: str = ""

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ApprovalRequest:
        call_data = data["call"]
        if not isinstance(call_data, Mapping):
            raise ValueError("stored approval call must be an object")
        arguments = call_data.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise ValueError("stored approval arguments must be an object")
        return cls(
            request_id=str(data["request_id"]),
            task_id=str(data["task_id"]),
            turn_id=str(data["turn_id"]),
            invocation_id=str(data["invocation_id"]),
            policy_decision_id=str(data["policy_decision_id"]),
            payload_hash=str(data["payload_hash"]),
            risk=ToolRisk(str(data["risk"])),
            call=ToolCall.from_data(call_data),
            action=str(data["action"]),
            target=str(data["target"]),
            preview=str(
                data.get(
                    "preview",
                    "legacy approval: inspect the stored call arguments before resolving",
                )
            ),
            network_access=str(data["network_access"]),
            data_transmission=str(data.get("data_transmission", "not declared")),
            rollback=str(data["rollback"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            agent_checkpoint=(
                dict(data["agent_checkpoint"])
                if isinstance(data.get("agent_checkpoint"), Mapping)
                else None
            ),
            kind=ApprovalKind(str(data.get("kind", ApprovalKind.TOOL_ACTION.value))),
            workspace_access_root=str(data.get("workspace_access_root", "")),
        )

    def matches(self, request_id: str, payload_hash: str) -> bool:
        return hmac.compare_digest(self.request_id, request_id) and hmac.compare_digest(
            self.payload_hash, payload_hash
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "invocation_id": self.invocation_id,
            "policy_decision_id": self.policy_decision_id,
            "payload_hash": self.payload_hash,
            "risk": self.risk.value,
            "call": self.call.to_data(),
            "action": self.action,
            "target": self.target,
            "preview": self.preview,
            "network_access": self.network_access,
            "data_transmission": self.data_transmission,
            "rollback": self.rollback,
            "created_at": self.created_at.isoformat(),
            "agent_checkpoint": (
                dict(self.agent_checkpoint) if self.agent_checkpoint is not None else None
            ),
            "kind": self.kind.value,
            "workspace_access_root": self.workspace_access_root,
        }


class ApprovalRequired(RuntimeError):
    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__(
            f"approval {request.request_id} is required for {request.call.name}"
        )
        self.request = request


class ApprovalNotPending(LookupError):
    pass


class ApprovalPayloadMismatch(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
