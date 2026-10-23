"""Skill core types and registry.

This module defines the runtime-visible Skill abstraction and a simple
in-memory registry. Concrete skills are registered at composition time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable


class SkillKind(StrEnum):
    """Kind of Skill.

    CODE skills dispatch to a Python callable.
    PLAN skills refer to a plan document such as a SKILL.md entry.
    """

    CODE = "code"
    PLAN = "plan"


@dataclass(frozen=True, slots=True)
class Skill:
    """One runtime-visible Skill.

    Skills may be CODE-backed (Python handler) or PLAN-backed (plan document).
    The model sees them as tools via their ``name`` and ``args_schema``.
    """

    id: str
    name: str
    description: str
    kind: SkillKind

    # JSON-schema-like argument and output contracts
    args_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] | None

    # CODE skills: a Python callable; PLAN skills: None
    handler: Callable[[Mapping[str, Any]], Any] | None

    # PLAN skills: path to SKILL.md or similar; CODE skills: None
    plan_path: str | None

    # Metadata
    tags: tuple[str, ...]
    safety_level: str  # e.g. "read_only", "write_local", "network", "exec"
    approval_required: bool


@dataclass
class SkillRegistry:
    """Process-local catalog rebuilt from user directories and code registrations.

    Availability is user-scoped. Session enablement methods are retained for
    compatibility with old snapshots; they do not gate file Skill discovery.
    """

    _skills: dict[str, Skill] = field(default_factory=dict)
    _discovered_ids: set[str] = field(default_factory=set)
    _session_enabled: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # --- Global skill catalog -------------------------------------------------

    def register(self, skill: Skill) -> None:
        """Register one Skill.

        Raises ``ValueError`` if the id already exists.
        """

        if skill.id in self._skills:
            raise ValueError(f"skill id already registered: {skill.id}")
        self._skills[skill.id] = skill

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def replace_discovered(self, skills: Iterable[Skill]) -> None:
        """Refresh file-backed entries without removing code-registered Skills."""
        for skill_id in self._discovered_ids:
            self._skills.pop(skill_id, None)
        self._discovered_ids.clear()
        for skill in skills:
            if skill.id not in self._skills:
                self._skills[skill.id] = skill
                self._discovered_ids.add(skill.id)

    def list_all(self) -> tuple[Skill, ...]:
        return tuple(self._skills.values())

    def list_by_tags(self, tags: Iterable[str]) -> tuple[Skill, ...]:
        wanted = set(tags)
        return tuple(
            skill for skill in self._skills.values()
            if wanted.intersection(skill.tags)
        )

    # --- Session-level enablement --------------------------------------------

    def set_session_enabled(
        self,
        session_id: str,
        skill_ids: tuple[str, ...],
    ) -> None:
        """Record the skills enabled for one Session.

        Callers must ensure that ``skill_ids`` refer to registered skills.
        """

        self._session_enabled[session_id] = skill_ids

    def get_session_enabled(self, session_id: str) -> tuple[str, ...]:
        return self._session_enabled.get(session_id, ())

    def list_session_skills(self, session_id: str) -> tuple[Skill, ...]:
        enabled_ids = self.get_session_enabled(session_id)
        return tuple(
            self._skills[skill_id]
            for skill_id in enabled_ids
            if skill_id in self._skills
        )

    # --- Local installation helper -------------------------------------------

    def install_from_skill(self, skill: Skill) -> None:
        """Convenience wrapper to register a Skill object.

        This mirrors ``register`` and exists mainly to give higher layers a
        semantically explicit entry point when they are installing Skills
        discovered from local directories or manifests.

        For now it simply calls ``register``; future versions may add
        validation or persistence hooks.
        """
        self.register(skill)


def install_skill(
    registry: "SkillRegistry",
    *,
    id: str,
    name: str,
    description: str,
    kind: str | SkillKind,
    args_schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
    handler: Callable[[Mapping[str, Any]], Any] | None = None,
    plan_path: str | None = None,
    tags: Iterable[str] | None = None,
    safety_level: str = "read_only",
    approval_required: bool = False,
) -> Skill:
    """Validate, normalise and register one Skill.

    This helper is intended as the common "install" entry point for higher
    layers (CLI, web, or tools) that discover skills from local manifests or
    configuration. It accepts a loose set of attributes, performs basic
    validation, constructs a :class:`Skill` object and registers it in the
    ``SkillRegistry``.
    """

    # Normalise kind
    if isinstance(kind, str):
        try:
            kind_value = SkillKind(kind)
        except ValueError as exc:
            raise ValueError(f"invalid skill kind: {kind!r}") from exc
    else:
        kind_value = kind

    # Default schemas/tags
    if args_schema is None:
        args_schema = {"type": "object", "properties": {}, "additionalProperties": True}

    if tags is None:
        tag_tuple: tuple[str, ...] = ()
    else:
        tag_tuple = tuple(str(t) for t in tags)

    # Kind-specific validation
    if kind_value is SkillKind.CODE:
        if handler is None:
            raise ValueError("CODE skill requires a handler")
        if plan_path is not None:
            raise ValueError("CODE skill must not set plan_path")
    elif kind_value is SkillKind.PLAN:
        if plan_path is None:
            raise ValueError("PLAN skill requires a plan_path")
        if handler is not None:
            raise ValueError("PLAN skill must not set handler")

    skill = Skill(
        id=id,
        name=name,
        description=description,
        kind=kind_value,
        args_schema=args_schema,
        output_schema=output_schema,
        handler=handler,
        plan_path=plan_path,
        tags=tag_tuple,
        safety_level=safety_level,
        approval_required=approval_required,
    )

    registry.install_from_skill(skill)
    return skill

