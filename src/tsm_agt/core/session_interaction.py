"""Deterministic resolution for a previously displayed choice list."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .session import SessionInteractionRequest


class SessionChoiceAction(StrEnum):
    SELECT = "SELECT"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class SessionChoiceDecision:
    action: SessionChoiceAction
    target_id: str | None = None
    option_id: str | None = None
    reason_code: str = "not_a_deterministic_choice"
    metadata: dict[str, Any] = field(default_factory=dict)


class DeterministicSessionChoiceResolver:
    """Resolve protocol identifiers and localized ordinals, never intent.

    This resolver only interprets a response against the exact persisted menu.
    Open-ended language remains the responsibility of the semantic resolver.
    """

    _CHINESE_DIGITS = {
        "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }

    def select(
        self, text: str, interaction: SessionInteractionRequest,
    ) -> SessionChoiceDecision:
        normalized = text.strip()
        if not normalized:
            return SessionChoiceDecision(SessionChoiceAction.UNRESOLVED)
        for option in interaction.options:
            if normalized in {option.option_id, option.target_id}:
                return self._selected(option, "exact_choice_identifier")
        ordinal = self._ordinal(normalized)
        if ordinal is not None:
            option = next((
                item for item in interaction.options if item.ordinal == ordinal
            ), None)
            if option is not None:
                return self._selected(option, "displayed_choice_ordinal")
        exact_labels = [
            item for item in interaction.options if normalized == item.label.strip()
        ]
        if len(exact_labels) == 1:
            return self._selected(exact_labels[0], "unique_exact_choice_label")
        return SessionChoiceDecision(SessionChoiceAction.UNRESOLVED)

    @classmethod
    def _ordinal(cls, text: str) -> int | None:
        compact = re.sub(r"[\s。.!！,，?？]+$", "", text.lower())
        match = re.fullmatch(
            r"(?:option|choice|item)?\s*#?(\d+)(?:st|nd|rd|th)?"
            r"(?:\s*(?:option|choice|item))?", compact, re.IGNORECASE,
        )
        if match:
            return int(match.group(1))
        # Chinese ordinal grammar is UI localization, not Task-intent inference.
        match = re.fullmatch(r"第([一二两三四五六七八九十])(?:个|项)?(?:吧|呢|呀)?", compact)
        return cls._CHINESE_DIGITS.get(match.group(1)) if match else None

    @staticmethod
    def _selected(option, reason: str) -> SessionChoiceDecision:
        return SessionChoiceDecision(
            SessionChoiceAction.SELECT, option.target_id, option.option_id, reason,
            dict(option.metadata),
        )
