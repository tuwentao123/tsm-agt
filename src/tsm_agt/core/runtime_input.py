"""Deterministic, inspectable routing for input received during a Task."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .configuration import canonical_hash


_RETRY_ACTION = re.compile(
    r"(?:继续|接着(?:做|来)?|恢复|重试|再试|重新试|重新跑|再跑|"
    r"重新执行|再执行|再来|retry|continue)",
    re.IGNORECASE,
)
_PREVIOUS_OR_INTERRUPTED = re.compile(
    r"(?:刚才|上次|之前|原来|断了|中断|停了|失败|没完成|没跑完)",
    re.IGNORECASE,
)
_NEW_TARGET_WITHOUT_BOUNDARY = re.compile(
    r"^(?:继续|接着|恢复|重试|再试|重新试|重新跑|再跑|重新执行|再执行)"
    r"(?:修改|分析|检查|实现|处理|查询|搜索|查看|写|删除|创建|修复).+",
    re.IGNORECASE,
)
_RETRY_STEERING_SPLIT = re.compile(r"[，,；;：:]\s*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RetryInterruptedInput:
    steering_text: str | None = None


def parse_retry_last_interrupted_input(
    text: str,
) -> RetryInterruptedInput | None:
    """Parse resume intent and an optional requirement after punctuation."""
    normalized = text.strip()
    if not normalized:
        return None
    parts = _RETRY_STEERING_SPLIT.split(normalized, maxsplit=1)
    resume_clause = re.sub(
        r"[\s。.!！?？~～]+$", "", parts[0].strip()
    )
    if _NEW_TARGET_WITHOUT_BOUNDARY.search(resume_clause):
        return None
    has_action = _RETRY_ACTION.search(normalized) is not None
    interrupted_reference = _PREVIOUS_OR_INTERRUPTED.search(resume_clause) is not None
    compact = re.sub(r"\s+", "", resume_clause).lower()
    content_free = len(compact) <= 24
    if not has_action or not (content_free or interrupted_reference):
        return None
    first_has_action = _RETRY_ACTION.search(resume_clause) is not None
    steering = (
        parts[1].strip()
        if first_has_action and len(parts) == 2 and parts[1].strip() else None
    )
    return RetryInterruptedInput(steering)


def is_retry_last_interrupted_input(text: str) -> bool:
    return parse_retry_last_interrupted_input(text) is not None


class FollowUpMode(StrEnum):
    STEER = "STEER"
    QUEUE = "QUEUE"


@dataclass(frozen=True, slots=True)
class QueuedFollowUp:
    input_id: str
    session_id: str
    after_task_id: str
    text: str
    inbound_sequence: int


class RuntimeInputIntent(StrEnum):
    STEER = "STEER"
    REPLACE = "REPLACE"
    STATUS_QUERY = "STATUS_QUERY"
    NEW_TASK_AFTER_CURRENT = "NEW_TASK_AFTER_CURRENT"
    CLARIFICATION_ANSWER = "CLARIFICATION_ANSWER"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class RuntimeInputContext:
    task_state: str
    awaiting_clarification: bool = False
    awaiting_approval: bool = False

    def to_classifier_data(self) -> dict[str, Any]:
        return {
            "task_state": self.task_state,
            "awaiting_clarification": self.awaiting_clarification,
            "awaiting_approval": self.awaiting_approval,
        }


@dataclass(frozen=True, slots=True)
class RuntimeInputRoute:
    intent: RuntimeInputIntent
    confidence: float
    reason_code: str
    requires_confirmation: bool
    applied: bool = False
    router_version: str = "deterministic-v1"

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("runtime input confidence must be between 0 and 1")

    def with_applied(self, applied: bool) -> RuntimeInputRoute:
        return replace(self, applied=applied)

    def to_data(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value, "confidence": self.confidence,
            "reason_code": self.reason_code,
            "requires_confirmation": self.requires_confirmation,
            "applied": self.applied, "router_version": self.router_version,
        }

    def to_event_data(self, text: str, input_id: str) -> dict[str, Any]:
        """Persist decision metadata, never the user's plaintext input."""
        return {
            **self.to_data(), "input_id": input_id,
            "text_hash": canonical_hash(text.strip()),
        }


class RuntimeInputRouter:
    """Rules handle obvious cases; uncertain language is never auto-applied."""

    _STATUS = re.compile(
        r"(?:现在|目前)?.*(?:做到哪|进度|状态|卡住|修改了哪些|改了哪些|"
        r"正在做什么|what.*status|progress)", re.IGNORECASE,
    )
    _REPLACE = re.compile(
        r"(?:别|不要|不用|停止|取消)(?:再|继续)?(?:修|做|改|处理|执行)?.{0,12}"
        r"(?:了|，|,|。|\s).*(?:改成|现在|只|转为)|"
        r"(?:前面|之前|原来).{0,10}(?:取消|作废|不要).{0,16}(?:现在|改成)|"
        r"^(?:改成|目标改为|新目标是|replace)", re.IGNORECASE,
    )
    _AFTER = re.compile(
        r"(?:完成|做完|结束)(?:这个|当前|后|之后).{0,12}(?:再|然后|另外)|"
        r"(?:下一个任务|另开(?:一个)?任务)", re.IGNORECASE,
    )
    _STEER = re.compile(
        r"^(?:另外|还有|补充|顺便|记得|同时|要求|注意|不要|请确保|输出|"
        r"并且|还要|也要)|(?:兼容|单元测试|不要修改|尽量简洁|约束)$",
        re.IGNORECASE,
    )

    def route(
        self, text: str, context: RuntimeInputContext,
        explicit_intent: RuntimeInputIntent | None = None,
        fallback_intent: RuntimeInputIntent | None = None,
    ) -> RuntimeInputRoute:
        normalized = text.strip()
        if not normalized:
            raise ValueError("runtime input must not be empty")
        if len(normalized) > 20_000:
            raise ValueError("runtime input exceeds 20000 characters")
        if explicit_intent is not None:
            if explicit_intent not in {
                RuntimeInputIntent.STEER, RuntimeInputIntent.REPLACE,
                RuntimeInputIntent.NEW_TASK_AFTER_CURRENT,
            }:
                raise ValueError("unsupported explicit runtime input intent")
            return RuntimeInputRoute(
                explicit_intent, 1.0, "explicit_override", False,
                router_version="explicit-v1",
            )
        if context.awaiting_approval:
            return RuntimeInputRoute(
                RuntimeInputIntent.AMBIGUOUS, 1.0,
                "approval_requires_explicit_decision", True,
            )
        if context.awaiting_clarification:
            return RuntimeInputRoute(
                RuntimeInputIntent.CLARIFICATION_ANSWER, 0.98,
                "pending_clarification", False,
            )
        if self._STATUS.search(normalized):
            return RuntimeInputRoute(
                RuntimeInputIntent.STATUS_QUERY, 0.96, "status_phrase", False,
            )
        if self._AFTER.search(normalized):
            return RuntimeInputRoute(
                RuntimeInputIntent.NEW_TASK_AFTER_CURRENT, 0.9,
                "after_current_phrase", False,
            )
        if self._REPLACE.search(normalized):
            return RuntimeInputRoute(
                RuntimeInputIntent.REPLACE, 0.91, "replacement_phrase", False,
            )
        if self._STEER.search(normalized):
            return RuntimeInputRoute(
                RuntimeInputIntent.STEER, 0.88, "additive_phrase", False,
            )
        if fallback_intent is not None:
            if fallback_intent is not RuntimeInputIntent.STEER:
                raise ValueError("only STEER is supported as a fallback intent")
            return RuntimeInputRoute(
                RuntimeInputIntent.STEER, 1.0,
                "follow_up_mode_default", False,
                router_version="follow-up-mode-v1",
            )
        return RuntimeInputRoute(
            RuntimeInputIntent.AMBIGUOUS, 0.45, "no_decisive_rule", True,
        )
