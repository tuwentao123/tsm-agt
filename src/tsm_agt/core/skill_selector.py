"""Skill selection for per-turn exposure.

This module implements a simple SkillSelector that chooses which skills to
expose to the model for one turn based on the Session configuration and
current user text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .session import SessionSnapshot
from .skills import SkillRegistry


@dataclass
class SkillSelector:
    """Choose a subset of Session-enabled skills for one turn.

    The selector is deliberately simple and deterministic: it starts from the
    Session's enabled_skills list, scores each Skill against the current user
    text, and returns the highest-scoring ids up to max_skills.
    """

    registry: SkillRegistry

    async def select_for_turn(
        self,
        session: SessionSnapshot,
        user_text: str,
        *,
        max_skills: int = 8,
    ) -> tuple[str, ...]:
        text = user_text.lower()
        enabled_ids = session.enabled_skills
        if not enabled_ids:
            return ()

        scores: list[tuple[int, str]] = []
        for skill_id in enabled_ids:
            skill = self.registry.get(skill_id)
            if skill is None:
                continue
            score = 0
            name = skill.name.lower()
            desc = skill.description.lower()
            tags = [t.lower() for t in skill.tags]

            if any(tag in text for tag in tags):
                score += 5
            if name and name in text:
                score += 3
            if any(word in desc for word in ("search", "code", "test", "file")):
                score += 1

            scores.append((score, skill_id))

        if not scores:
            return enabled_ids[:max_skills]
        max_score = max(score for score, _sid in scores)
        if max_score <= 0:
            # No positive signal: fall back to the Session-wide ordering.
            return enabled_ids[:max_skills]

        scores.sort(key=lambda pair: pair[0], reverse=True)
        return tuple(skill_id for _score, skill_id in scores[:max_skills])

