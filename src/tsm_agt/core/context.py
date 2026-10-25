"""Deterministic context budgeting and protocol-safe compaction."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from tsm_agt.ports import (
    Message, MessageRole, TextBlock, ToolCallBlock, ToolResultBlock, ToolSpec,
)

from .configuration import canonical_hash
from .prompt import PromptTemplate
from .context_messages import is_execution_context
from .read_result_reuse import ReadReuseStats, project_read_results
from .session_history import MANAGED_HISTORY_BUDGETS, compact_history_body, history_turn_groups


class ContextWindowExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ContextBudgetAllocation:
    name: str
    estimated_tokens: int
    protected: bool

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name, "estimated_tokens": self.estimated_tokens,
            "protected": self.protected,
        }


@dataclass(frozen=True, slots=True)
class ContextBudget:
    context_window: int
    max_output_tokens: int
    trigger_tokens: int
    hard_input_tokens: int
    estimated_input_tokens: int
    estimation_method: str = "utf8-bytes-div-4-conservative-v1"
    allocations: tuple[ContextBudgetAllocation, ...] = ()
    history_soft_tokens: int = 0

    @property
    def should_compact(self) -> bool:
        return self.estimated_input_tokens + self.max_output_tokens >= self.trigger_tokens

    def allocation_data(self) -> list[dict[str, Any]]:
        return [item.to_data() for item in self.allocations]

    def event_data(self) -> dict[str, Any]:
        history_tokens = sum(item.estimated_tokens for item in self.allocations if item.name == "session_history")
        return {
            "context_window": self.context_window,
            "trigger_tokens": self.trigger_tokens,
            "hard_input_tokens": self.hard_input_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "reserved_output_tokens": self.max_output_tokens,
            "estimated_total_reserved_tokens": (
                self.estimated_input_tokens + self.max_output_tokens
            ),
            "estimation_method": self.estimation_method,
            "allocations": self.allocation_data(),
            "history_soft_tokens": self.history_soft_tokens,
            "history_budget_algorithm": "turn-token-v2",
            "history_estimated_tokens": history_tokens,
            "history_soft_limit_exceeded": bool(self.history_soft_tokens and history_tokens > self.history_soft_tokens),
        }


@dataclass(frozen=True, slots=True)
class ContextCompactionReceipt:
    algorithm: str
    version: int
    source_message_count: int
    compacted_message_count: int
    preserved_message_count: int
    before_estimated_tokens: int
    after_estimated_tokens: int
    context_window: int
    max_output_tokens: int
    trigger_tokens: int
    source_hash: str
    summary_hash: str
    protected_message_count: int
    protected_sections: tuple[str, ...]
    forgotten_message_ids: tuple[str, ...] = ()
    forgotten_event_sequences: tuple[int, ...] = ()
    pinned_message_ids: tuple[str, ...] = ()

    def event_data(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "version": self.version,
            "source_message_count": self.source_message_count,
            "compacted_message_count": self.compacted_message_count,
            "preserved_message_count": self.preserved_message_count,
            "before_estimated_tokens": self.before_estimated_tokens,
            "after_estimated_tokens": self.after_estimated_tokens,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "trigger_tokens": self.trigger_tokens,
            "source_hash": self.source_hash,
            "summary_hash": self.summary_hash,
            "protected_message_count": self.protected_message_count,
            "protected_sections": list(self.protected_sections),
            "forgotten_message_ids": list(self.forgotten_message_ids),
            "forgotten_event_sequences": list(self.forgotten_event_sequences),
            "pinned_message_ids": list(self.pinned_message_ids),
            "summary_body_persisted": False,
        }


@dataclass(frozen=True, slots=True)
class PreparedContext:
    messages: tuple[Message, ...]
    budget: ContextBudget
    compaction: ContextCompactionReceipt | None = None
    # Canonical retained results stay recoverable; messages is the model view.
    retained_messages: tuple[Message, ...] | None = None
    read_reuse: ReadReuseStats | None = None

    @property
    def canonical_messages(self) -> tuple[Message, ...]:
        return self.messages if self.retained_messages is None else self.retained_messages


@dataclass(frozen=True, slots=True)
class ContextWindowManager:
    trigger_ratio: float = 0.80
    recent_message_floor: int = 8
    latency_soft_input_tokens: int = 0
    #: Ceiling for the union of recency-selected and pinned messages. Pinning
    #: decides eligibility; this only bounds the result.
    pinned_message_ceiling: int = 32
    # One allocation for all authored history, runtime facts and read payloads.
    # Zero disables this soft cap, not the Provider hard-window protection.
    history_soft_tokens: int = 16384

    def __post_init__(self) -> None:
        if not 0 < self.trigger_ratio < 1:
            raise ValueError("context trigger_ratio must be between 0 and 1")
        if self.recent_message_floor < 1:
            raise ValueError("recent_message_floor must be positive")
        if self.latency_soft_input_tokens < 0:
            raise ValueError(
                "latency_soft_input_tokens must be zero or positive"
            )
        if self.pinned_message_ceiling < self.recent_message_floor:
            raise ValueError(
                "pinned_message_ceiling must be at least recent_message_floor"
            )
        if self.history_soft_tokens < 0:
            raise ValueError("history_soft_tokens must be zero or positive")

    def snapshot_data(self) -> dict[str, Any]:
        return {
            "algorithm": "provider-budget-managed-session",
            "version": 2,
            "trigger_ratio": self.trigger_ratio,
            "recent_message_floor": self.recent_message_floor,
            "latency_soft_input_tokens": self.latency_soft_input_tokens,
            "pinned_message_ceiling": self.pinned_message_ceiling,
            "history_soft_tokens": self.history_soft_tokens,
            "estimation_method": "utf8-bytes-div-4-conservative-v1",
            "preserve_first_user_goal": True,
            "preserve_session_and_working_memory": True,
            "preserve_all_session_task_indexes": True,
            "preserve_session_artifact_paths": True,
            "preserve_pinned_session_messages": True,
            "preserve_tool_call_result_groups": True,
        }

    def prepare(
        self, *, conversation: tuple[Message, ...], tools: tuple[ToolSpec, ...],
        prompt_template: PromptTemplate, context_window: int,
        max_output_tokens: int,
        runtime_instruction: str | None = None,
        read_reuse_task_id: str | None = None,
    ) -> PreparedContext:
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if context_window <= 0:
            estimated, allocations = self.estimate_input_budget(
                conversation, tools, prompt_template, runtime_instruction,
                read_reuse_task_id=read_reuse_task_id,
            )
            budget = ContextBudget(
                0, max_output_tokens, 0, 0, estimated,
                "disabled-provider-window-unknown",
                allocations, self.history_soft_tokens,
            )
            compacted, changed = self._compact_turn_history(
                conversation, tools, prompt_template, budget, runtime_instruction,
                read_reuse_task_id=read_reuse_task_id,
            )
            after, allocations = self.estimate_input_budget(compacted, tools, prompt_template, runtime_instruction,
                                                          read_reuse_task_id=read_reuse_task_id)
            receipt = self._compaction_receipt(conversation, compacted, budget, changed, ()) if changed else None
            return self._prepared(compacted, replace(budget, estimated_input_tokens=after, allocations=allocations),
                                  replace(receipt, after_estimated_tokens=after) if receipt else None,
                                  read_reuse_task_id)
        if max_output_tokens >= context_window:
            raise ContextWindowExceeded(
                "max_output_tokens must be smaller than provider context_window"
            )
        before, before_allocations = self.estimate_input_budget(
            conversation, tools, prompt_template, runtime_instruction,
            read_reuse_task_id=read_reuse_task_id,
        )
        budget = ContextBudget(
            context_window=context_window, max_output_tokens=max_output_tokens,
            trigger_tokens=max(1, min(
                [math.floor(context_window * self.trigger_ratio)]
                + (
                    [self.latency_soft_input_tokens]
                    if self.latency_soft_input_tokens > 0 else []
                )
            )),
            hard_input_tokens=context_window - max_output_tokens,
            estimated_input_tokens=before,
            allocations=before_allocations,
            history_soft_tokens=self.history_soft_tokens,
        )
        managed_history = any(
            (body := self._session_body(message)) is not None
            and body.get("history_budget") in MANAGED_HISTORY_BUDGETS
            for message in conversation
        )
        history_pressure = self.history_soft_tokens and managed_history and any(
            item.name == "session_history" and item.estimated_tokens > self.history_soft_tokens
            for item in before_allocations
        )
        if not budget.should_compact and not history_pressure:
            return self._prepared(conversation, budget, None, read_reuse_task_id)

        compacted, receipt = self._compact(
            conversation, tools, prompt_template, budget, runtime_instruction,
            read_reuse_task_id=read_reuse_task_id,
        )
        after, after_allocations = self.estimate_input_budget(
            compacted, tools, prompt_template, runtime_instruction,
            read_reuse_task_id=read_reuse_task_id,
        )
        if receipt is not None:
            receipt = replace(receipt, after_estimated_tokens=after)
        if after > budget.hard_input_tokens:
            allocation_summary = ", ".join(
                f"{item.name}={item.estimated_tokens}"
                for item in sorted(
                    after_allocations, key=lambda item: item.estimated_tokens,
                    reverse=True,
                )
                if item.estimated_tokens
            )
            raise ContextWindowExceeded(
                "context cannot fit after safe compaction: "
                f"estimated_input={after}, hard_input={budget.hard_input_tokens}; "
                "the current request, protected state, recent tool protocol, or "
                f"visible Tool schemas are too large; allocations: {allocation_summary}"
            )
        return self._prepared(
            compacted, ContextBudget(
                context_window, max_output_tokens, budget.trigger_tokens,
                budget.hard_input_tokens, after, budget.estimation_method,
                after_allocations,
                self.history_soft_tokens,
            ), receipt, read_reuse_task_id,
        )

    @staticmethod
    def _prepared(
        retained: tuple[Message, ...], budget: ContextBudget,
        receipt: ContextCompactionReceipt | None, task_id: str | None,
    ) -> PreparedContext:
        projection = project_read_results(retained, task_id) if task_id else None
        return PreparedContext(
            projection.messages if projection else retained, budget, receipt,
            retained, projection.stats if projection else None,
        )

    @staticmethod
    def estimate_input_tokens(
        conversation: tuple[Message, ...], tools: tuple[ToolSpec, ...],
        prompt_template: PromptTemplate,
        runtime_instruction: str | None = None,
        *, read_reuse_task_id: str | None = None,
    ) -> int:
        estimated, _ = ContextWindowManager.estimate_input_budget(
            conversation, tools, prompt_template, runtime_instruction,
            read_reuse_task_id=read_reuse_task_id,
        )
        return estimated

    @staticmethod
    def estimate_input_budget(
        conversation: tuple[Message, ...], tools: tuple[ToolSpec, ...],
        prompt_template: PromptTemplate,
        runtime_instruction: str | None = None,
        *, read_reuse_task_id: str | None = None,
    ) -> tuple[int, tuple[ContextBudgetAllocation, ...]]:
        if read_reuse_task_id:
            conversation = project_read_results(conversation, read_reuse_task_id).messages
        assembly = prompt_template.assemble(
            conversation, tools, runtime_instruction
        )
        buckets: dict[str, list[dict[str, Any]]] = {
            "static_system": [], "runtime_instruction": [],
            "project_context": [], "session_history": [],
            "working_memory": [], "current_turn_and_tool_protocol": [],
        }
        for message in assembly.messages:
            data = {
                "role": message.role.value,
                "content": message.to_data()["content"],
            }
            if message.phase is not None:
                data["phase"] = message.phase.value
            if message.message_id.startswith("prompt-runtime-"):
                bucket = "runtime_instruction"
            elif message.message_id.startswith((
                "task-spec-context-", "conversation-environment-context-",
            )):
                bucket = "project_context"
            elif message.role is MessageRole.SYSTEM:
                bucket = "static_system"
            elif message.message_id.startswith((
                "project-onboarding-context-", "project-memory-context-",
                "task-spec-context-", "conversation-environment-context-",
            )):
                bucket = "project_context"
            elif message.message_id.startswith(("session-context-", "session-history-", "session-facts-")):
                bucket = "session_history"
            elif message.message_id.startswith("working-memory-context-") or is_execution_context(message.message_id):
                bucket = "working_memory"
            else:
                bucket = "current_turn_and_tool_protocol"
            buckets[bucket].append(data)

        allocations: list[ContextBudgetAllocation] = []
        protected = {
            "static_system", "runtime_instruction", "session_history",
            "working_memory", "current_turn_and_tool_protocol",
        }
        for name, items in buckets.items():
            token_count = sum(
                ContextWindowManager._estimate_json_tokens(item) + 4
                for item in items
            )
            allocations.append(ContextBudgetAllocation(
                name, token_count, name in protected
            ))
        tool_data = [{
            "name": tool.name, "description": tool.description,
            "parameters": dict(tool.parameters),
        } for tool in tools]
        allocations.append(ContextBudgetAllocation(
            "tool_schemas",
            sum(ContextWindowManager._estimate_json_tokens(item) + 2
                for item in tool_data),
            True,
        ))
        allocations.append(ContextBudgetAllocation(
            "serialization_overhead", 8, True
        ))
        return max(1, sum(item.estimated_tokens for item in allocations)), tuple(
            allocations
        )

    @staticmethod
    def _estimate_json_tokens(value: Any) -> int:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return max(1, math.ceil(len(encoded) / 4))

    def _compact(
        self, conversation: tuple[Message, ...], tools: tuple[ToolSpec, ...],
        prompt_template: PromptTemplate, budget: ContextBudget,
        runtime_instruction: str | None = None,
        *, read_reuse_task_id: str | None = None,
    ) -> tuple[tuple[Message, ...], ContextCompactionReceipt | None]:
        source_conversation = conversation
        conversation, turn_history_items = self._compact_turn_history(
            conversation, tools, prompt_template, budget, runtime_instruction,
            read_reuse_task_id=read_reuse_task_id,
        )
        conversation, compacted_session_items = self._compact_session_messages(
            conversation
        )
        compacted_session_items += turn_history_items
        target = min(budget.hard_input_tokens, budget.trigger_tokens - budget.max_output_tokens)
        if self.estimate_input_tokens(conversation, tools, prompt_template, runtime_instruction,
                                      read_reuse_task_id=read_reuse_task_id) <= target:
            # History pressure alone must not compact the live tool exchange.
            return conversation, (self._compaction_receipt(
                source_conversation, conversation, budget, compacted_session_items, (),
            ) if compacted_session_items else None)
        groups = self._atomic_groups(conversation)
        if len(groups) <= 2:
            if compacted_session_items == 0:
                return conversation, None
            return conversation, self._compaction_receipt(
                source_conversation, conversation, budget,
                compacted_session_items, (),
            )
        recent_indexes: set[int] = set()
        recent_count = 0
        for index in range(len(groups) - 1, 0, -1):
            if recent_count >= self.recent_message_floor:
                break
            group = groups[index]
            recent_indexes.add(index)
            recent_count += len(group)
        protected_indexes = {
            index for index, group in enumerate(groups)
            if index == 0 or self._is_protected_group(group)
        }
        candidate_indexes = [
            index for index in range(len(groups))
            if index not in protected_indexes and index not in recent_indexes
        ]
        if not candidate_indexes and compacted_session_items == 0:
            return conversation, None
        compacted_messages = tuple(message for index in candidate_indexes
                                   for message in groups[index])
        summary = (
            self._summary_message(compacted_messages)
            if compacted_messages else None
        )
        preserved_messages: list[Message] = []
        summary_inserted = False
        candidate_set = set(candidate_indexes)
        for index, group in enumerate(groups):
            if index in candidate_set:
                if not summary_inserted:
                    assert summary is not None
                    preserved_messages.append(summary)
                    summary_inserted = True
                continue
            preserved_messages.extend(group)
        preserved = tuple(preserved_messages)
        receipt = self._compaction_receipt(
            source_conversation, preserved, budget, compacted_session_items,
            compacted_messages,
        )
        return preserved, receipt

    def _compact_turn_history(
        self, conversation: tuple[Message, ...], tools: tuple[ToolSpec, ...],
        template: PromptTemplate, budget: ContextBudget, instruction: str | None,
        *, read_reuse_task_id: str | None = None,
    ) -> tuple[tuple[Message, ...], int]:
        """One token budget, whole historical Turns, no second count cutoff.

        The immediate prior Turn is kept verbatim. If it and the protected live
        exchange cannot fit, the existing explicit window error is safer than
        silently cutting off the proposal the user just accepted.
        """
        removed = 0
        target = (max(1, min(budget.hard_input_tokens, budget.trigger_tokens - budget.max_output_tokens))
                  if budget.context_window else math.inf)

        def fits(candidate_messages: tuple[Message, ...]) -> bool:
            total, allocations = self.estimate_input_budget(candidate_messages, tools, template, instruction,
                                                            read_reuse_task_id=read_reuse_task_id)
            history = sum(item.estimated_tokens for item in allocations if item.name == "session_history")
            return total <= target and (not self.history_soft_tokens or history <= self.history_soft_tokens)

        for index, message in enumerate(conversation):
            body = self._session_body(message)
            if body is None or body.get("history_budget") not in MANAGED_HISTORY_BUDGETS:
                continue
            groups = history_turn_groups(body.get("recent_messages", []))
            if not groups:
                continue
            if fits(conversation):
                continue

            def candidate(keep: int, previews: bool, omit_reads: bool = True) -> tuple[Message, ...]:
                data = compact_history_body(body, keep, include_previews=previews, omit_read_bodies=omit_reads)
                replacement = Message(
                    message.message_id.removesuffix("-compacted") + "-compacted", message.role,
                    (TextBlock(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),),
                    scaffolding=message.scaffolding,
                    phase=message.phase,
                )
                return (*conversation[:index], replacement, *conversation[index + 1:])

            # Exact read bodies are expendable before authored dialogue. Try
            # retaining every Turn with their execution/error facts intact.
            all_turns = candidate(len(groups), False)
            if fits(all_turns):
                conversation = all_turns
                removed += 1
                continue
            # Binary-search the largest complete recent suffix that fits. Keep
            # provenance-only summaries when older excerpts would crowd it out.
            best_keep = 1
            best = candidate(best_keep, False)
            low, high = 1, len(groups) - 1
            while low <= high:
                keep = (low + high) // 2
                proposed = candidate(keep, False)
                if fits(proposed):
                    best, best_keep = proposed, keep
                    low = keep + 1
                else:
                    high = keep - 1
            # Excerpts use only spare budget; they never displace a complete
            # recent turn that already fit in the provenance-only version.
            with_previews = candidate(best_keep, True)
            if fits(with_previews):
                best = with_previews
            # Read payloads can use remaining space, but never displace text or
            # execution facts. The latest full Turn may exceed the soft cap;
            # prepare() still enforces the Provider hard input limit.
            with_reads = candidate(best_keep, fits(with_previews), omit_reads=False)
            if fits(with_reads):
                best = with_reads
            kept_body = self._session_body(best[index])
            assert kept_body is not None
            removed += max(1, len(body.get("recent_messages", [])) - len(kept_body.get("recent_messages", [])))
            conversation = best
        return conversation, removed

    def _compact_session_messages(
        self, messages: tuple[Message, ...],
    ) -> tuple[tuple[Message, ...], int]:
        """Compact Session detail only after the Provider budget triggers.

        Every Task keeps a minimal task_index entry, including artifact paths and
        source references. Only old visible messages, detailed Task summaries,
        and verbose investigation records are reduced.
        """
        projected: list[Message] = []
        removed = 0
        for message in messages:
            if not message.message_id.startswith("session-context-"):
                projected.append(message)
                continue
            try:
                body = json.loads(message.text)
            except (TypeError, ValueError):
                projected.append(message)
                continue
            if not isinstance(body, dict) or body.get("boundary") != (
                "session_conversation_projection"
            ):
                projected.append(message)
                continue
            if body.get("history_budget") in MANAGED_HISTORY_BUDGETS:
                projected.append(message)
                continue
            raw_messages = body.get("recent_messages") or []
            if not isinstance(raw_messages, list):
                projected.append(message)
                continue
            # INV-8 at the compression layer: eligibility is provenance, not
            # recency. A Task the caller pinned (for example the source of a
            # derived Task) is retained even when none of its messages are recent.
            pinned = {
                str(item) for item in body.get("pinned_task_ids", [])
                if isinstance(item, (str, int)) and str(item)
            }
            recent = raw_messages[-self.recent_message_floor:]
            if pinned:
                pinned_messages = [
                    item for item in raw_messages
                    if isinstance(item, dict)
                    and str(item.get("task_id")) in pinned
                ]
                recent = pinned_messages + [
                    item for item in recent if item not in pinned_messages
                ]
                recent = recent[-self.pinned_message_ceiling:]
            recent_task_ids = {
                str(item.get("task_id")) for item in recent
                if isinstance(item, dict) and item.get("task_id")
            }
            eligible_task_ids = recent_task_ids | pinned
            summaries = body.get("recent_task_summaries", [])
            if isinstance(summaries, list):
                kept_summaries = [
                    item for item in summaries
                    if isinstance(item, dict)
                    and str(item.get("task_id")) in eligible_task_ids
                ]
                removed += len(summaries) - len(kept_summaries)
                body["recent_task_summaries"] = kept_summaries
            dropped_messages = max(0, len(raw_messages) - len(recent))
            removed += dropped_messages
            if raw_messages or body.get("recent_messages"):
                body["recent_messages"] = recent
            if dropped_messages:
                # ``earlier_summary`` is this payload's own account of what is NOT
                # in the prompt, and the projector computed it one layer earlier.
                # Without folding this layer's drops in, the field claims fewer
                # omissions than the prompt actually has -- a reader sees
                # ``message_count: 0`` beside a prompt that lost whole Turns.
                earlier = body.get("earlier_summary")
                if isinstance(earlier, dict):
                    earlier["message_count"] = (
                        int(earlier.get("message_count") or 0) + dropped_messages
                    )
            historical = body.get("historical_investigation")
            if isinstance(historical, dict):
                for key in ("resources", "questions"):
                    values = historical.get(key, [])
                    if not isinstance(values, list):
                        continue
                    kept = [
                        item for item in values
                        if isinstance(item, dict)
                        and str(item.get("source_task_id")) in eligible_task_ids
                    ]
                    removed += len(values) - len(kept)
                    historical[key] = kept
            body["compaction"] = {
                "algorithm": "provider-budget-managed-session-v2",
                "task_index_preserved": True,
                "artifact_paths_preserved": True,
                "recent_message_floor": self.recent_message_floor,
                "pinned_message_ceiling": self.pinned_message_ceiling,
                "pinned_task_ids": sorted(pinned),
            }
            text = json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            projected.append(Message(
                message.message_id + "-compacted", message.role,
                (TextBlock(text),),
                scaffolding=message.scaffolding,
                phase=message.phase,
            ))
        return tuple(projected), removed

    def _compaction_receipt(
        self, source: tuple[Message, ...], preserved: tuple[Message, ...],
        budget: ContextBudget, compacted_session_items: int,
        compacted_messages: tuple[Message, ...],
    ) -> ContextCompactionReceipt:
        source_data = [self._message_hash_data(message) for message in source]
        summary_data = [self._message_hash_data(message) for message in preserved]
        preserved_ids = {message.message_id for message in preserved}

        def survived(message_id: str) -> bool:
            # A rewritten message keeps its identity but gains "-compacted".
            return (
                message_id in preserved_ids
                or message_id + "-compacted" in preserved_ids
            )

        forgotten = tuple(
            message.message_id for message in source
            if not survived(message.message_id)
        )
        # Provenance: every dropped fact must stay traceable to the durable
        # events it summarised, so a compacted context is auditable. Either the
        # whole message went away, or its session detail was reduced in place.
        preserved_by_source_id = {
            message.message_id.removesuffix("-compacted"): message
            for message in preserved
        }
        forgotten_sequences: set[int] = set()
        for message in source:
            if not survived(message.message_id):
                forgotten_sequences.update(
                    self._message_event_sequences(message)
                )
                continue
            after = preserved_by_source_id.get(message.message_id)
            if after is not None:
                forgotten_sequences.update(
                    set(self._message_event_sequences(message))
                    - set(self._message_event_sequences(after))
                )
        pinned_message_ids = tuple(
            message.message_id for message in preserved
            if message.message_id.startswith("session-context-")
            and self._session_pinned_task_ids(message)
        )
        return ContextCompactionReceipt(
            algorithm="provider-budget-managed-session", version=2,
            source_message_count=len(source),
            compacted_message_count=(
                len(compacted_messages) + compacted_session_items
            ),
            preserved_message_count=len(preserved),
            before_estimated_tokens=budget.estimated_input_tokens,
            after_estimated_tokens=0, context_window=budget.context_window,
            max_output_tokens=budget.max_output_tokens,
            trigger_tokens=budget.trigger_tokens,
            source_hash=canonical_hash(source_data),
            summary_hash=canonical_hash(summary_data),
            protected_message_count=sum(
                1 for message in preserved if self._is_protected_group((message,))
            ),
            protected_sections=(
                "current_user_input", "session_task_index",
                "session_artifact_paths", "session_working_state",
                "task_goal_constraints_plan_evidence",
                "recent_tool_protocol_atomic_groups",
            ),
            forgotten_message_ids=forgotten,
            forgotten_event_sequences=tuple(sorted(forgotten_sequences)),
            pinned_message_ids=pinned_message_ids,
        )

    @staticmethod
    def _session_body(message: Message) -> Mapping[str, Any] | None:
        if not message.message_id.startswith("session-context-"):
            return None
        try:
            body = json.loads(message.text)
        except (TypeError, ValueError):
            return None
        if not isinstance(body, dict) or body.get("boundary") != (
            "session_conversation_projection"
        ):
            return None
        return body

    @classmethod
    def _message_event_sequences(cls, message: Message) -> tuple[int, ...]:
        """Durable event sequences a session projection message pointed at."""
        body = cls._session_body(message)
        if body is None:
            return ()
        sequences: set[int] = set()
        for value in body.get("source_event_sequences", []):
            if isinstance(value, int):
                sequences.add(value)
        for item in body.get("recent_messages", []):
            if isinstance(item, Mapping) and isinstance(
                item.get("source_event_sequence"), int
            ):
                sequences.add(item["source_event_sequence"])
        return tuple(sorted(sequences))

    @classmethod
    def _session_pinned_task_ids(cls, message: Message) -> tuple[str, ...]:
        body = cls._session_body(message)
        if body is None:
            return ()
        return tuple(
            str(item) for item in body.get("pinned_task_ids", [])
            if isinstance(item, (str, int)) and str(item)
        )

    @staticmethod
    def _is_protected_group(group: tuple[Message, ...]) -> bool:
        return any((message.role is MessageRole.USER
                    and (not message.is_scaffolding or message.scaffolding in {"steering", "continuation-input"})
                    and not message.message_id.startswith((
                        "context-summary-", "project-onboarding-context-",
                        "project-memory-context-", "document-references-context-",
                    )))
                   or message.message_id.startswith((
            "session-context-", "working-memory-context-", "execution-context-",
            "task-spec-context-", "conversation-environment-context-",
            "project-instructions-context-",
        )) for message in group)

    @staticmethod
    def _atomic_groups(
        messages: tuple[Message, ...],
    ) -> tuple[tuple[Message, ...], ...]:
        groups: list[tuple[Message, ...]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            calls = [
                block.call.call_id for block in message.content
                if isinstance(block, ToolCallBlock)
            ]
            if message.role is MessageRole.ASSISTANT and calls:
                group = [message]
                expected = set(calls)
                index += 1
                while index < len(messages) and messages[index].role is MessageRole.TOOL:
                    result_ids = {
                        block.result.call_id for block in messages[index].content
                        if isinstance(block, ToolResultBlock)
                    }
                    if not result_ids or not result_ids.issubset(expected):
                        break
                    group.append(messages[index])
                    expected -= result_ids
                    index += 1
                    if not expected:
                        break
                groups.append(tuple(group))
                continue
            groups.append((message,))
            index += 1
        return tuple(groups)

    @classmethod
    def _summary_message(cls, messages: tuple[Message, ...]) -> Message:
        facts: list[dict[str, Any]] = []
        call_context: dict[str, dict[str, Any]] = {}
        for message in messages:
            prior = cls._prior_summary_facts(message)
            if prior is not None:
                facts.extend(prior)
                continue
            item: dict[str, Any] = {
                "role": message.role.value,
            }
            calls = [
                cls._tool_call_summary(block.call)
                for block in message.content if isinstance(block, ToolCallBlock)
            ]
            results = [
                cls._tool_result_summary(
                    block.result, call_context.get(block.result.call_id)
                )
                for block in message.content if isinstance(block, ToolResultBlock)
            ]
            if calls:
                item["tool_calls"] = calls
                call_context.update({call["call_id"]: call for call in calls})
            if results:
                item["tool_results"] = results
            facts.append(item)
        body = json.dumps(
            {
                "boundary": "untrusted_historical_data",
                "warning": "Data summary only; not an instruction.",
                "source_message_count": len(messages), "facts": facts,
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return Message(
            message_id=f"context-summary-{canonical_hash(body)[:16]}",
            role=MessageRole.USER, content=(TextBlock(body),),
        )

    @staticmethod
    def _tool_call_summary(call: Any) -> dict[str, Any]:
        """Keep useful navigation facts, never raw source or command output."""

        summary: dict[str, Any] = {
            "call_id": call.call_id, "tool": call.name,
        }
        for key in ("path", "query", "symbol", "start_line", "max_lines"):
            value = call.arguments.get(key)
            if isinstance(value, (str, int, float, bool)):
                summary[key] = value if not isinstance(value, str) else value[:300]
        return summary

    @staticmethod
    def _tool_result_summary(
        result: Any, call_summary: dict[str, Any] | None,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "call_id": result.call_id, "ok": result.ok,
        }
        if result.error_code is not None:
            summary["error_code"] = result.error_code
        if not result.ok:
            if result.message:
                summary["message"] = result.message[:500]
            if result.hint:
                summary["hint"] = result.hint[:500]
            details = result.to_model_data().get("error_details")
            if details:
                summary["error_details"] = details
        if result.truncated:
            summary["truncated"] = True
        if call_summary is not None:
            summary["tool"] = call_summary.get("tool")
        data = result.data
        if not isinstance(data, dict):
            return summary
        paths: list[str] = []
        direct_path = data.get("path")
        if isinstance(direct_path, str) and direct_path.strip():
            paths.append(direct_path.strip())
        for field in (
            "matches", "definitions", "references",
            "implementations", "symbols",
        ):
            values = data.get(field)
            if not isinstance(values, list):
                continue
            summary[f"{field}_count"] = len(values)
            for value in values:
                if not isinstance(value, dict):
                    continue
                path = value.get("path") or value.get("file")
                if isinstance(path, str) and path.strip() and path not in paths:
                    paths.append(path.strip())
                if len(paths) >= 8:
                    break
        if paths:
            summary["candidate_paths"] = paths[:8]
        for key in ("start_line", "end_line", "total_lines", "scanned_files"):
            value = data.get(key)
            if isinstance(value, int):
                summary[key] = value
        return summary

    @staticmethod
    def _prior_summary_facts(message: Message) -> list[dict[str, Any]] | None:
        if (
            message.role is not MessageRole.USER
            or not message.message_id.startswith("context-summary-")
            or len(message.content) != 1
            or not isinstance(message.content[0], TextBlock)
        ):
            return None
        try:
            data = json.loads(message.content[0].text)
        except (json.JSONDecodeError, TypeError):
            return None
        facts = data.get("facts") if isinstance(data, dict) else None
        if not isinstance(facts, list) or not all(
            isinstance(item, dict) for item in facts
        ):
            return None
        return [dict(item) for item in facts]

    @staticmethod
    def _message_hash_data(message: Message) -> dict[str, Any]:
        data: dict[str, Any] = {
            "role": message.role.value,
            "content_hash": canonical_hash(message.to_data()["content"]),
        }
        if message.phase is not None:
            data["phase"] = message.phase.value
        return data
