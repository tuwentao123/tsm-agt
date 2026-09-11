"""Deterministic context budgeting and protocol-safe compaction."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from typing import Any

from tsm_agt.ports import (
    Message, MessageRole, TextBlock, ToolCallBlock, ToolResultBlock, ToolSpec,
)

from .configuration import canonical_hash
from .prompt import PromptTemplate


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

    @property
    def should_compact(self) -> bool:
        return self.estimated_input_tokens + self.max_output_tokens >= self.trigger_tokens

    def allocation_data(self) -> list[dict[str, Any]]:
        return [item.to_data() for item in self.allocations]

    def event_data(self) -> dict[str, Any]:
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
            "summary_body_persisted": False,
        }


@dataclass(frozen=True, slots=True)
class PreparedContext:
    messages: tuple[Message, ...]
    budget: ContextBudget
    compaction: ContextCompactionReceipt | None = None


@dataclass(frozen=True, slots=True)
class ContextWindowManager:
    trigger_ratio: float = 0.80
    recent_message_floor: int = 8
    latency_soft_input_tokens: int = 0

    def __post_init__(self) -> None:
        if not 0 < self.trigger_ratio < 1:
            raise ValueError("context trigger_ratio must be between 0 and 1")
        if self.recent_message_floor < 1:
            raise ValueError("recent_message_floor must be positive")
        if self.latency_soft_input_tokens < 0:
            raise ValueError(
                "latency_soft_input_tokens must be zero or positive"
            )

    def snapshot_data(self) -> dict[str, Any]:
        return {
            "algorithm": "provider-budget-managed-session",
            "version": 2,
            "trigger_ratio": self.trigger_ratio,
            "recent_message_floor": self.recent_message_floor,
            "latency_soft_input_tokens": self.latency_soft_input_tokens,
            "estimation_method": "utf8-bytes-div-4-conservative-v1",
            "preserve_first_user_goal": True,
            "preserve_session_and_working_memory": True,
            "preserve_all_session_task_indexes": True,
            "preserve_session_artifact_paths": True,
            "preserve_tool_call_result_groups": True,
        }

    def prepare(
        self, *, conversation: tuple[Message, ...], tools: tuple[ToolSpec, ...],
        prompt_template: PromptTemplate, context_window: int,
        max_output_tokens: int,
        runtime_instruction: str | None = None,
    ) -> PreparedContext:
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if context_window <= 0:
            estimated, allocations = self.estimate_input_budget(
                conversation, tools, prompt_template, runtime_instruction
            )
            return PreparedContext(
                conversation, ContextBudget(
                    0, max_output_tokens, 0, 0,
                    estimated,
                    "disabled-provider-window-unknown",
                    allocations,
                )
            )
        if max_output_tokens >= context_window:
            raise ContextWindowExceeded(
                "max_output_tokens must be smaller than provider context_window"
            )
        before, before_allocations = self.estimate_input_budget(
            conversation, tools, prompt_template, runtime_instruction
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
        )
        if not budget.should_compact:
            return PreparedContext(conversation, budget)

        compacted, receipt = self._compact(
            conversation, tools, prompt_template, budget, runtime_instruction
        )
        after, after_allocations = self.estimate_input_budget(
            compacted, tools, prompt_template, runtime_instruction
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
        return PreparedContext(
            compacted, ContextBudget(
                context_window, max_output_tokens, budget.trigger_tokens,
                budget.hard_input_tokens, after, budget.estimation_method,
                after_allocations,
            ), receipt,
        )

    @staticmethod
    def estimate_input_tokens(
        conversation: tuple[Message, ...], tools: tuple[ToolSpec, ...],
        prompt_template: PromptTemplate,
        runtime_instruction: str | None = None,
    ) -> int:
        estimated, _ = ContextWindowManager.estimate_input_budget(
            conversation, tools, prompt_template, runtime_instruction
        )
        return estimated

    @staticmethod
    def estimate_input_budget(
        conversation: tuple[Message, ...], tools: tuple[ToolSpec, ...],
        prompt_template: PromptTemplate,
        runtime_instruction: str | None = None,
    ) -> tuple[int, tuple[ContextBudgetAllocation, ...]]:
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
            if message.message_id.startswith("prompt-runtime-"):
                bucket = "runtime_instruction"
            elif message.message_id.startswith("task-spec-context-"):
                bucket = "project_context"
            elif message.role is MessageRole.SYSTEM:
                bucket = "static_system"
            elif message.message_id.startswith((
                "project-onboarding-context-", "project-memory-context-",
                "task-spec-context-",
            )):
                bucket = "project_context"
            elif message.message_id.startswith("session-context-"):
                bucket = "session_history"
            elif message.message_id.startswith("working-memory-context-"):
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
    ) -> tuple[tuple[Message, ...], ContextCompactionReceipt | None]:
        source_conversation = conversation
        conversation, compacted_session_items = self._compact_session_messages(
            conversation
        )
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
            raw_messages = body.get("recent_messages", [])
            if not isinstance(raw_messages, list):
                projected.append(message)
                continue
            recent = raw_messages[-self.recent_message_floor:]
            recent_task_ids = {
                str(item.get("task_id")) for item in recent
                if isinstance(item, dict) and item.get("task_id")
            }
            summaries = body.get("recent_task_summaries", [])
            if isinstance(summaries, list):
                kept_summaries = [
                    item for item in summaries
                    if isinstance(item, dict)
                    and str(item.get("task_id")) in recent_task_ids
                ]
                removed += len(summaries) - len(kept_summaries)
                body["recent_task_summaries"] = kept_summaries
            removed += len(raw_messages) - len(recent)
            body["recent_messages"] = recent
            historical = body.get("historical_investigation")
            if isinstance(historical, dict):
                for key in ("resources", "questions"):
                    values = historical.get(key, [])
                    if not isinstance(values, list):
                        continue
                    kept = [
                        item for item in values
                        if isinstance(item, dict)
                        and str(item.get("source_task_id")) in recent_task_ids
                    ]
                    removed += len(values) - len(kept)
                    historical[key] = kept
            body["compaction"] = {
                "algorithm": "provider-budget-managed-session-v2",
                "task_index_preserved": True,
                "artifact_paths_preserved": True,
                "recent_message_floor": self.recent_message_floor,
            }
            text = json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            projected.append(Message(
                message.message_id + "-compacted", message.role,
                (TextBlock(text),),
            ))
        return tuple(projected), removed

    def _compaction_receipt(
        self, source: tuple[Message, ...], preserved: tuple[Message, ...],
        budget: ContextBudget, compacted_session_items: int,
        compacted_messages: tuple[Message, ...],
    ) -> ContextCompactionReceipt:
        source_data = [self._message_hash_data(message) for message in source]
        summary_data = [self._message_hash_data(message) for message in preserved]
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
        )

    @staticmethod
    def _is_protected_group(group: tuple[Message, ...]) -> bool:
        return any(message.message_id.startswith((
            "session-context-", "working-memory-context-",
            "task-spec-context-", "project-instructions-context-",
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
        if call.evidence_question is not None:
            summary["question_id"] = call.evidence_question.question_id[:64]
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
        return {
            "role": message.role.value,
            "content_hash": canonical_hash(message.to_data()["content"]),
        }
