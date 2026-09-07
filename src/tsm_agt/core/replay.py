"""Read-only, side-effect-free replay of persisted Events into Flow snapshots."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import math
from typing import Any

from tsm_agt.ports import InvestigationFlowProjectorPort, RuntimeEvent

from .flow import (
    FlowNodeKind, FlowNodeStatus, FlowProjection, FlowProjectionError,
    FlowProjector,
)


class FlowReplayBoundary(StrEnum):
    START = "start"
    END = "end"


class FlowReplaySpeed(StrEnum):
    REALTIME = "1x"
    DOUBLE = "2x"
    MAX = "max"

    @property
    def divisor(self) -> float | None:
        return {
            self.REALTIME: 1.0,
            self.DOUBLE: 2.0,
            self.MAX: None,
        }[self]


@dataclass(frozen=True, slots=True)
class FlowReplayFrame:
    sequence: int
    event_type: str
    occurred_at: datetime

    def to_data(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class FlowReplayIndex:
    task_id: str
    latest_cursor: int
    frames: tuple[FlowReplayFrame, ...]
    terminal: bool

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "latest_cursor": self.latest_cursor,
            "terminal": self.terminal,
            "frames": [frame.to_data() for frame in self.frames],
        }


@dataclass(frozen=True, slots=True)
class FlowReplayTarget:
    node_id: str
    node_kind: FlowNodeKind
    label: str
    boundary: FlowReplayBoundary
    event_sequence: int

    def to_data(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_kind": self.node_kind.value,
            "label": self.label,
            "boundary": self.boundary.value,
            "event_sequence": self.event_sequence,
        }


@dataclass(frozen=True, slots=True)
class FlowReplaySnapshot:
    task_id: str
    cursor: int
    latest_cursor: int
    frame: FlowReplayFrame
    projection: FlowProjection
    target: FlowReplayTarget | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "cursor": self.cursor,
            "latest_cursor": self.latest_cursor,
            "frame": self.frame.to_data(),
            "flow": self.projection.to_data(),
            "read_only": True,
            "target": self.target.to_data() if self.target else None,
        }


@dataclass(frozen=True, slots=True)
class FlowReplayPlaybackFrame:
    frame: FlowReplayFrame
    recorded_gap_ms: int
    wait_ms: int
    gap_capped: bool
    timestamp_regressed: bool

    def to_data(self, snapshot: FlowReplaySnapshot) -> dict[str, Any]:
        return {
            "snapshot": snapshot.to_data(),
            "timing": {
                "recorded_gap_ms": self.recorded_gap_ms,
                "wait_ms": self.wait_ms,
                "gap_capped": self.gap_capped,
                "timestamp_regressed": self.timestamp_regressed,
            },
        }


@dataclass(frozen=True, slots=True)
class FlowReplayPlayback:
    task_id: str
    speed: FlowReplaySpeed
    max_wait_ms: int
    frames: tuple[FlowReplayPlaybackFrame, ...]
    latest_cursor: int
    from_sequence: int
    to_sequence: int
    _prefix_events: tuple[RuntimeEvent, ...]
    _events: tuple[RuntimeEvent, ...]
    _investigation_projector: InvestigationFlowProjectorPort | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "speed": self.speed.value,
            "max_wait_ms": self.max_wait_ms,
            "frame_count": len(self.frames),
            "latest_cursor": self.latest_cursor,
            "from_sequence": self.from_sequence,
            "to_sequence": self.to_sequence,
            "read_only": True,
        }

    def iter_snapshots(
        self,
    ) -> Iterator[tuple[FlowReplayPlaybackFrame, FlowReplaySnapshot]]:
        """Incrementally materialize one current Flow; never retain all snapshots."""

        projector = FlowProjector(self._investigation_projector)
        projection: FlowProjection | None = (
            projector.project(self._prefix_events)
            if self._prefix_events else None
        )
        for playback_frame, event in zip(
            self.frames, self._events, strict=True
        ):
            projection = (
                projector.project((event,))
                if projection is None
                else projector.apply(projection, (event,))
            )
            yield playback_frame, FlowReplaySnapshot(
                task_id=self.task_id, cursor=event.sequence,
                latest_cursor=self.latest_cursor, frame=playback_frame.frame,
                projection=projection,
            )


class FlowReplay:
    """Validate one Event stream before exposing historical Flow frames."""

    _EXPECTED_ROOT_STATUS = {
        "SUCCEEDED": FlowNodeStatus.SUCCEEDED,
        "FAILED": FlowNodeStatus.FAILED,
        "CANCELLED": FlowNodeStatus.CANCELLED,
        "AWAITING_APPROVAL": FlowNodeStatus.WAITING_APPROVAL,
        "AWAITING_USER": FlowNodeStatus.WAITING_USER,
    }
    _TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

    def __init__(
        self, investigation_projector: InvestigationFlowProjectorPort | None = None,
    ) -> None:
        self._investigation_projector = investigation_projector

    def index(
        self, events: tuple[RuntimeEvent, ...], current_task_state: str,
        expected_latest_sequence: int | None = None,
    ) -> FlowReplayIndex:
        projection = self._validate(
            events, current_task_state, expected_latest_sequence
        )
        return FlowReplayIndex(
            task_id=projection.task_id,
            latest_cursor=projection.cursor,
            frames=tuple(
                FlowReplayFrame(
                    event.sequence, event.event_type, event.occurred_at
                )
                for event in events
            ),
            terminal=current_task_state in self._TERMINAL_STATES,
        )

    def snapshot(
        self, events: tuple[RuntimeEvent, ...], current_task_state: str,
        at_sequence: int | None = None,
        expected_latest_sequence: int | None = None,
    ) -> FlowReplaySnapshot:
        latest = self._validate(
            events, current_task_state, expected_latest_sequence
        )
        cursor = latest.cursor if at_sequence is None else at_sequence
        return self._snapshot_at(events, latest, cursor)

    def snapshot_for_node(
        self, events: tuple[RuntimeEvent, ...], current_task_state: str,
        node_id: str, boundary: FlowReplayBoundary = FlowReplayBoundary.END,
        expected_latest_sequence: int | None = None,
    ) -> FlowReplaySnapshot:
        latest = self._validate(
            events, current_task_state, expected_latest_sequence
        )
        normalized = node_id.strip()
        if not normalized:
            raise ValueError("replay node_id must not be empty")
        if not isinstance(boundary, FlowReplayBoundary):
            raise TypeError("replay boundary must be a FlowReplayBoundary")
        try:
            node = next(node for node in latest.nodes if node.node_id == normalized)
        except StopIteration as error:
            raise LookupError(f"replay node not found: {normalized}") from error
        cursor = (
            node.event_seq_start
            if boundary is FlowReplayBoundary.START
            else node.event_seq_end
        )
        if (
            cursor is None
            or (
                boundary is FlowReplayBoundary.END
                and not node.status.is_terminal
            )
        ):
            raise LookupError(
                f"replay node has no {boundary.value} event yet: {normalized}"
            )
        target = FlowReplayTarget(
            node_id=node.node_id, node_kind=node.kind, label=node.label,
            boundary=boundary, event_sequence=cursor,
        )
        return self._snapshot_at(events, latest, cursor, target)

    def snapshot_for_turn(
        self, events: tuple[RuntimeEvent, ...], current_task_state: str,
        turn_id: str, boundary: FlowReplayBoundary = FlowReplayBoundary.END,
        expected_latest_sequence: int | None = None,
    ) -> FlowReplaySnapshot:
        normalized = turn_id.strip()
        if not normalized:
            raise ValueError("replay turn_id must not be empty")
        node_id = normalized if normalized.startswith("turn:") else f"turn:{normalized}"
        snapshot = self.snapshot_for_node(
            events, current_task_state, node_id, boundary,
            expected_latest_sequence,
        )
        assert snapshot.target is not None
        if snapshot.target.node_kind is not FlowNodeKind.TURN:
            raise LookupError(f"replay target is not a Turn node: {node_id}")
        return snapshot

    def playback(
        self, events: tuple[RuntimeEvent, ...], current_task_state: str,
        speed: FlowReplaySpeed = FlowReplaySpeed.REALTIME,
        max_wait_seconds: float = 2.0,
        expected_latest_sequence: int | None = None,
        from_sequence: int | None = None,
        to_sequence: int | None = None,
    ) -> FlowReplayPlayback:
        """Validate once and build a pure playback schedule for all frames."""

        if not isinstance(speed, FlowReplaySpeed):
            raise TypeError("replay speed must be a FlowReplaySpeed")
        if not math.isfinite(max_wait_seconds) or max_wait_seconds <= 0:
            raise ValueError("replay max wait seconds must be positive")
        latest = self._validate(
            events, current_task_state, expected_latest_sequence
        )
        start = 1 if from_sequence is None else from_sequence
        end = latest.cursor if to_sequence is None else to_sequence
        if start < 1 or start > latest.cursor + 1:
            raise ValueError(
                f"replay from sequence must be between 1 and {latest.cursor + 1}, "
                f"got {start}"
            )
        if start == latest.cursor + 1 and end == latest.cursor:
            selected_events = ()
        elif end < start or end > latest.cursor:
            raise ValueError(
                f"replay to sequence must be between {start} and "
                f"{latest.cursor}, got {end}"
            )
        else:
            selected_events = events[start - 1:end]
        max_wait_ms = round(max_wait_seconds * 1000)
        frames: list[FlowReplayPlaybackFrame] = []
        previous_at: datetime | None = None
        for event in selected_events:
            try:
                raw_gap_ms = (
                    0
                    if previous_at is None
                    else round(
                        (event.occurred_at - previous_at).total_seconds() * 1000
                    )
                )
            except TypeError as error:
                raise FlowProjectionError(
                    "replay Event timestamps use incompatible timezone forms"
                ) from error
            regressed = raw_gap_ms < 0
            recorded_gap_ms = max(0, raw_gap_ms)
            divisor = speed.divisor
            scaled_wait_ms = (
                0 if divisor is None else round(recorded_gap_ms / divisor)
            )
            wait_ms = min(scaled_wait_ms, max_wait_ms)
            frames.append(FlowReplayPlaybackFrame(
                frame=FlowReplayFrame(
                    event.sequence, event.event_type, event.occurred_at
                ),
                recorded_gap_ms=recorded_gap_ms,
                wait_ms=wait_ms, gap_capped=scaled_wait_ms > max_wait_ms,
                timestamp_regressed=regressed,
            ))
            previous_at = event.occurred_at
        return FlowReplayPlayback(
            task_id=latest.task_id, speed=speed, max_wait_ms=max_wait_ms,
            frames=tuple(frames), latest_cursor=latest.cursor,
            from_sequence=start, to_sequence=end,
            _prefix_events=events[:start - 1], _events=selected_events,
            _investigation_projector=self._investigation_projector,
        )

    def _snapshot_at(
        self,
        events: tuple[RuntimeEvent, ...], latest: FlowProjection, cursor: int,
        target: FlowReplayTarget | None = None,
    ) -> FlowReplaySnapshot:
        if cursor < 1 or cursor > latest.cursor:
            raise ValueError(
                f"replay sequence must be between 1 and {latest.cursor}, got {cursor}"
            )
        projection = (
            latest if cursor == latest.cursor
            else FlowProjector(self._investigation_projector).project(
                events[:cursor]
            )
        )
        event = events[cursor - 1]
        return FlowReplaySnapshot(
            task_id=latest.task_id, cursor=cursor, latest_cursor=latest.cursor,
            frame=FlowReplayFrame(
                event.sequence, event.event_type, event.occurred_at
            ),
            projection=projection,
            target=target,
        )

    def _validate(
        self, events: tuple[RuntimeEvent, ...], current_task_state: str,
        expected_latest_sequence: int | None,
    ) -> FlowProjection:
        projection = FlowProjector(self._investigation_projector).project(events)
        if (
            expected_latest_sequence is not None
            and projection.cursor != expected_latest_sequence
        ):
            raise FlowProjectionError(
                "stored Task last_event_sequence does not match Event Log tail: "
                f"task={expected_latest_sequence}, events={projection.cursor}"
            )
        root = next(
            (node for node in projection.nodes if node.parent_id is None), None
        )
        if root is None:
            raise FlowProjectionError("replay projection has no Task root")
        expected = self._EXPECTED_ROOT_STATUS.get(
            current_task_state, FlowNodeStatus.RUNNING
        )
        if root.status is not expected:
            raise FlowProjectionError(
                "stored Task state does not match the latest Event projection: "
                f"task={current_task_state}, flow={root.status.value}"
            )
        if current_task_state in self._TERMINAL_STATES:
            final = events[-1]
            if (
                final.event_type != "task.state_changed"
                or str(final.payload.get("next_state", "")) != current_task_state
            ):
                raise FlowProjectionError(
                    "terminal Task is missing a matching final state Event"
                )
        return projection
