"""Read-only runtime projections for `Kernel` (flow, replay, investigation status, effective configuration).

A mixin module in the Hermes facade+siblings shape (``run_agent.py`` +
``agent/session_persistence.py``): the facade ``core/kernel.py`` imports
this at module level and adds ``ProjectionMixin`` to ``Kernel``'s bases, so
**every method resolves through ``Kernel``'s MRO unchanged** --
``self._dependencies``, ``self._append_events`` and ``Kernel``'s own class
attributes all still resolve. Moved bodies are verbatim: nothing is renamed
and no signature changes.

This module must never bind the facade at module level; late-import it
inside a method instead (Hermes ``AGENTS.md:243-244``).

Moved from ``core/kernel.py``:

* ``_build_effective_configuration``
* ``_event_excerpt``
* ``_live_scope_target``
* ``get_effective_configuration``
* ``get_flow_projection``
* ``get_flow_replay_index``
* ``get_flow_replay_node_snapshot``
* ``get_flow_replay_playback``
* ``get_flow_replay_snapshot``
* ``get_flow_replay_turn_snapshot``
* ``get_investigation_status``
"""

from __future__ import annotations


import json
from collections.abc import Mapping
from datetime import datetime, timezone
from tsm_agt.ports import (
    EVIDENCE_CATEGORIES, EvidenceInventory, ExplorationBudgetState,
    HealthState, HealthStatus, InvestigationStatusProjection,
    InvestigationStatusSignals, StopOrPivotState
)
from .agent_loop import AgentTurnCheckpoint
from .configuration import EffectiveConfigurationSnapshot, canonical_hash
from .flow import FlowProjection, FlowProjector
from .replay import (
    FlowReplay, FlowReplayIndex, FlowReplayPlayback, FlowReplaySnapshot
)
from .task import TaskNotFound, TaskSnapshot


class ProjectionMixin:
    async def get_flow_projection(
        self, task_id: str, previous: FlowProjection | None = None,
    ) -> FlowProjection:
        """Build or incrementally update the read-only Event Log flow view."""
        stored = await self._dependencies.store.load_task(task_id)
        if stored is None:
            raise TaskNotFound(f"task not found: {task_id}")
        if previous is not None and previous.task_id != task_id:
            raise ValueError("flow projection belongs to a different task")
        events = await self._dependencies.store.read_events(
            task_id, previous.cursor if previous is not None else 0
        )
        projector = FlowProjector(
            self._dependencies.investigation_flow_projector
        )
        if previous is None:
            return projector.project(events)
        if not events:
            return previous
        return projector.apply(previous, events)

    async def get_flow_replay_index(self, task_id: str) -> FlowReplayIndex:
        """Validate and list payload-free frames for one persisted Task."""
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        events = await self._dependencies.store.read_events(task_id)
        return FlowReplay(self._dependencies.investigation_flow_projector).index(
            events, task.state.value, stored.last_event_sequence
        )

    async def get_flow_replay_snapshot(
        self, task_id: str, at_sequence: int | None = None,
    ) -> FlowReplaySnapshot:
        """Rebuild a historical Flow frame without executing any work."""
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        events = await self._dependencies.store.read_events(task_id)
        return FlowReplay(self._dependencies.investigation_flow_projector).snapshot(
            events, task.state.value, at_sequence, stored.last_event_sequence
        )

    async def get_flow_replay_node_snapshot(
        self, task_id: str, node_id: str, boundary: FlowReplayBoundary,
    ) -> FlowReplaySnapshot:
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        events = await self._dependencies.store.read_events(task_id)
        return FlowReplay(
            self._dependencies.investigation_flow_projector
        ).snapshot_for_node(
            events, task.state.value, node_id, boundary,
            stored.last_event_sequence,
        )

    async def get_flow_replay_turn_snapshot(
        self, task_id: str, turn_id: str, boundary: FlowReplayBoundary,
    ) -> FlowReplaySnapshot:
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        events = await self._dependencies.store.read_events(task_id)
        return FlowReplay(
            self._dependencies.investigation_flow_projector
        ).snapshot_for_turn(
            events, task.state.value, turn_id, boundary,
            stored.last_event_sequence,
        )

    async def get_flow_replay_playback(
        self, task_id: str, speed: FlowReplaySpeed,
        max_wait_seconds: float = 2.0,
        from_sequence: int | None = None,
        to_sequence: int | None = None,
    ) -> FlowReplayPlayback:
        stored = await self._require_stored_task(task_id)
        task = TaskSnapshot.from_data(stored.data)
        events = await self._dependencies.store.read_events(task_id)
        return FlowReplay(
            self._dependencies.investigation_flow_projector
        ).playback(
            events, task.state.value, speed, max_wait_seconds,
            stored.last_event_sequence, from_sequence, to_sequence,
        )

    async def get_investigation_status(
        self, task_id: str,
    ) -> InvestigationStatusProjection:
        """Rebuild redacted status without model, Tool, or state mutation."""
        projector = self._dependencies.investigation_status_projector
        if projector is None:
            raise LookupError("investigation status projector is not configured")
        task = await self.get_task(task_id)
        events = await self._dependencies.store.read_events(task_id)
        checkpoint = (
            AgentTurnCheckpoint.from_data(task.active_agent_checkpoint)
            if task.active_agent_checkpoint is not None else None
        )
        counts = {category: 0 for category in EVIDENCE_CATEGORIES}
        consecutive_zero_delta = 0
        scored_actions = 0
        cumulative_tool_milliseconds = 0
        low_value_streak = 0
        model_calls = 0
        tool_calls = 0
        if checkpoint is not None:
            inventory = EvidenceInventory.from_data(checkpoint.evidence_inventory)
            counts.update({
                category: len(inventory.fingerprints.get(category, ()))
                for category in EVIDENCE_CATEGORIES
            })
            consecutive_zero_delta = inventory.consecutive_zero_delta
            budget = ExplorationBudgetState.from_data(
                checkpoint.exploration_budget_state
            )
            scored_actions = budget.scored_actions
            cumulative_tool_milliseconds = budget.cumulative_tool_milliseconds
            low_value_streak = budget.low_value_streak
            model_calls = checkpoint.model_calls
            tool_calls = checkpoint.tool_calls
        else:
            for event in events:
                if event.event_type != "evidence.delta_evaluated":
                    continue
                raw_counts = event.payload.get("counts")
                if isinstance(raw_counts, Mapping):
                    for category in EVIDENCE_CATEGORIES:
                        value = raw_counts.get(category, 0)
                        if isinstance(value, int) and value > 0:
                            counts[category] += value
                consecutive_zero_delta = max(
                    0, int(event.payload.get("consecutive_zero_delta", 0))
                )
            scored = [
                event for event in events
                if event.event_type == "exploration_budget.action_scored"
            ]
            scored_actions = len(scored)
            if scored:
                cumulative_tool_milliseconds = max(
                    0, int(scored[-1].payload.get(
                        "cumulative_tool_milliseconds", 0
                    ))
                )
                low_value_streak = max(
                    0, int(scored[-1].payload.get("low_value_streak", 0))
                )
            completed = [
                event for event in events if event.event_type == "turn.completed"
            ]
            if completed:
                model_calls = max(
                    0, int(completed[-1].payload.get("model_calls", 0))
                )
                tool_calls = max(
                    0, int(completed[-1].payload.get("tool_calls", 0))
                )
        progress = [
            event for event in events
            if event.event_type == "agent_progress.projected"
        ]
        question_ref = (
            str(progress[-1].payload.get("question_ref", ""))
            if progress else ""
        )
        if not question_ref:
            deltas = [
                event for event in events
                if event.event_type == "evidence.delta_evaluated"
            ]
            if deltas and deltas[-1].payload.get("question_id"):
                question_ref = canonical_hash(
                    str(deltas[-1].payload["question_id"])
                )[:12]
        scores = [
            event for event in events
            if event.event_type == "exploration_budget.action_scored"
        ]
        budget_score = (
            int(scores[-1].payload["score"])
            if scores and isinstance(scores[-1].payload.get("score"), int)
            else None
        )
        value_band = (
            str(scores[-1].payload.get("value_band", ""))
            if scores else ""
        )
        decisions = [
            event for event in events
            if event.event_type == "stop_or_pivot.decision_made"
        ]
        latest_decision = (
            str(decisions[-1].payload.get("action", ""))
            if decisions else (
                str(StopOrPivotState.from_data(
                    checkpoint.stop_or_pivot_state
                ).last_decision) if checkpoint is not None else ""
            )
        )
        return await projector.project(InvestigationStatusSignals(
            question_ref=question_ref, evidence_counts=counts,
            consecutive_zero_delta=consecutive_zero_delta,
            scored_actions=scored_actions, budget_score=budget_score,
            value_band=value_band, low_value_streak=low_value_streak,
            cumulative_tool_milliseconds=cumulative_tool_milliseconds,
            latest_decision=latest_decision, model_calls=model_calls,
            tool_calls=tool_calls,
        ))

    async def get_effective_configuration(
        self, task_id: str, revision: int | None = None,
    ) -> EffectiveConfigurationSnapshot:
        """Read an immutable stored snapshot; never sample live configuration."""
        task = await self.get_task(task_id)
        if not task.effective_configurations:
            raise LookupError(
                f"task has no effective configuration snapshot: {task_id}"
            )
        if revision is None:
            return task.effective_configurations[-1]
        for configuration in task.effective_configurations:
            if configuration.revision == revision:
                return configuration
        raise LookupError(
            f"effective configuration revision {revision} not found for {task_id}"
        )

    async def _build_effective_configuration(
        self, task: TaskSnapshot, revision: int,
    ) -> EffectiveConfigurationSnapshot:
        adapter_snapshots = []
        for adapter in self._dependencies.runtime_adapters:
            try:
                health = await adapter.health()
            except Exception as error:
                health = HealthStatus(
                    HealthState.UNHEALTHY,
                    f"health probe failed: {type(error).__name__}",
                )
            adapter_snapshots.append((adapter.descriptor, health))
        adapters = tuple(adapter_snapshots)
        metadata = self._dependencies.configuration_metadata or {}
        raw_model = metadata.get("model", {})
        raw_sources = metadata.get("sources", {})
        model = dict(raw_model) if isinstance(raw_model, Mapping) else {}
        sources = dict(raw_sources) if isinstance(raw_sources, Mapping) else {}
        policy = self._tool_policy.snapshot_data()
        raw_exploration_budget = metadata.get("exploration_budget")
        if isinstance(raw_exploration_budget, Mapping):
            policy = dict(policy)
            policy["exploration_budget"] = dict(raw_exploration_budget)
        return EffectiveConfigurationSnapshot.build(
            captured_at=datetime.now(timezone.utc),
            model=model,
            capabilities=self._dependencies.model.capabilities,
            tools=await self.list_tools(),
            adapters=adapters,
            policy=policy,
            project={
                "trust": task.project_trust.value,
                "workspace_fingerprint": task.project_fingerprint,
                "sandbox_adapter_id": self._dependencies.sandbox.descriptor.adapter_id,
            },
            sources=sources,
            revision=revision,
            prompt_manifest=self._dependencies.prompt_template.manifest().to_data(),
            context=self._dependencies.context_manager.snapshot_data(),
        )

    @staticmethod
    def _event_excerpt(event: RuntimeEvent) -> str:
        """Return the human-readable body of one persisted event."""
        if event.event_type == "llm.completed":
            message = event.payload.get("message")
            if isinstance(message, Mapping):
                texts = [
                    str(block.get("text"))
                    for block in message.get("content", [])
                    if isinstance(block, Mapping) and block.get("text")
                ]
                if texts:
                    return "\n".join(texts)
        return json.dumps(
            event.payload, ensure_ascii=False, sort_keys=True, default=str,
        )

    @staticmethod
    def _live_scope_target(call: ToolCall) -> str:
        """Describe the concrete live operation scope without audit redaction."""
        arguments = call.arguments
        for name in ("path", "cwd", "symbol", "query", "pattern"):
            value = arguments.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "workspace"
