"""Ordered, event-sourced user steering for an active Agent turn."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tsm_agt.ports import RuntimeEvent

from .configuration import canonical_hash


class SteeringKind(StrEnum):
    STEER = "steer"
    REPLACE = "replace"


@dataclass(frozen=True, slots=True)
class SteeringInput:
    steering_id: str
    task_id: str
    inbound_sequence: int
    kind: SteeringKind
    text: str
    text_hash: str
    source_event_sequence: int

    def __post_init__(self) -> None:
        if not self.steering_id.strip() or not self.task_id.strip():
            raise ValueError("steering identity must not be empty")
        if self.inbound_sequence < 1 or self.source_event_sequence < 1:
            raise ValueError("steering sequences must be positive")
        if not self.text.strip():
            raise ValueError("steering text must not be empty")
        if self.text_hash != canonical_hash(self.text):
            raise ValueError("steering text hash does not match")

    def to_data(self) -> dict[str, Any]:
        return {
            "steering_id": self.steering_id, "task_id": self.task_id,
            "inbound_sequence": self.inbound_sequence,
            "kind": self.kind.value, "text": self.text,
            "text_hash": self.text_hash,
            "source_event_sequence": self.source_event_sequence,
        }


@dataclass(frozen=True, slots=True)
class SteeringProjection:
    task_id: str
    revision: int
    pending: tuple[SteeringInput, ...]
    applied_ids: tuple[str, ...]
    latest_inbound_sequence: int
    latest_goal_revision: int
    content_hash: str


class SteeringProjector:
    @staticmethod
    def project(task_id: str, events: Sequence[RuntimeEvent]) -> SteeringProjection:
        queued: dict[str, SteeringInput] = {}
        applied: set[str] = set()
        latest_inbound = 0
        goal_revision = 1
        revision = 0
        for event in sorted(events, key=lambda item: item.sequence):
            if event.task_id != task_id:
                raise ValueError("steering event belongs to another Task")
            if event.event_type == "steering.queued":
                steering_id = str(event.payload.get("steering_id") or "")
                text = str(event.payload.get("text") or "").strip()
                inbound = int(event.payload.get("inbound_sequence") or 0)
                item = SteeringInput(
                    steering_id, task_id, inbound,
                    SteeringKind(str(event.payload.get("kind"))), text,
                    str(event.payload.get("text_hash") or ""), event.sequence,
                )
                queued[steering_id] = item
                latest_inbound = max(latest_inbound, inbound)
                revision += 1
            elif event.event_type == "steering.applied":
                identifiers = event.payload.get("steering_ids", [])
                if isinstance(identifiers, list):
                    applied.update(str(item) for item in identifiers)
                goal_revision = max(
                    goal_revision, int(event.payload.get("goal_revision") or 1)
                )
                revision += 1
        pending = tuple(sorted(
            (item for key, item in queued.items() if key not in applied),
            key=lambda item: item.inbound_sequence,
        ))
        source = {
            "task_id": task_id, "revision": revision,
            "pending": [item.to_data() for item in pending],
            "applied_ids": sorted(applied),
            "latest_inbound_sequence": latest_inbound,
            "latest_goal_revision": goal_revision,
        }
        return SteeringProjection(
            task_id, revision, pending, tuple(sorted(applied)), latest_inbound,
            goal_revision, canonical_hash(source),
        )
