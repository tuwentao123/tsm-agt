"""Project single-loop orchestration metadata from durable task events."""

from __future__ import annotations

from collections.abc import Sequence

from tsm_agt.ports.runtime_store import RuntimeEvent

from .types import TaskOrchestrationMetadata


class TaskOrchestrationProjector:
    """Read new orchestration metadata while preserving legacy task semantics.

    The projector owns the legacy fallback so later execution code never needs
    to infer a root goal from assistant messages or mutable Session summaries.
    """

    @staticmethod
    def project(
        task_id: str,
        fallback_goal: str,
        events: Sequence[RuntimeEvent],
    ) -> TaskOrchestrationMetadata:
        created = next((
            event for event in events
            if event.task_id == task_id and event.event_type == "task.created"
        ), None)
        if created is None:
            raise ValueError(f"task {task_id} has no task.created event")
        event_goal = str(created.payload.get("goal") or fallback_goal)
        return TaskOrchestrationMetadata.from_task_created_payload(
            created.payload, fallback_goal=event_goal,
        )
