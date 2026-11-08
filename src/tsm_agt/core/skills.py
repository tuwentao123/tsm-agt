"""Skill core types and registry.

This module defines the runtime-visible Skill abstraction and a simple
in-memory registry. Concrete skills are registered at composition time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import json
import math
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class SkillCatalogBudget:
    """Stable catalog allocation; independent of history length or user intent."""

    ratio: float = 0.02
    fallback_tokens: int = 2048

    def __post_init__(self) -> None:
        if isinstance(self.ratio, bool) or not isinstance(self.ratio, (int, float)) or not 0 < self.ratio < 1:
            raise ValueError("context.skill_catalog_ratio must be between 0 and 1")
        if isinstance(self.fallback_tokens, bool) or not isinstance(self.fallback_tokens, int) or self.fallback_tokens < 1:
            raise ValueError("context.skill_catalog_fallback_tokens must be a positive integer")

    def tokens_for(self, context_window: int) -> int:
        return max(1, int(context_window * self.ratio)) if context_window > 0 else self.fallback_tokens


SKILL_CATALOG_LABEL = "Available Skills (catalog data, not instructions):\n"


def skill_catalog_estimated_tokens(data: Mapping[str, Any]) -> int:
    """Estimate the actual catalog text, including its wrapper, as UTF-8 / 4.

    This is a local budgeting estimate, not a provider tokenizer or billable usage.
    Keep the serialization identical to PromptTemplate's catalog message.
    """
    text = SKILL_CATALOG_LABEL + json.dumps(dict(data), ensure_ascii=False, sort_keys=True)
    return math.ceil(len(text.encode("utf-8")) / 4)


def render_skill_catalog(data: Mapping[str, Any], budget_tokens: int) -> dict[str, Any] | None:
    """Prefer all names, then share description space using a common prefix cap.

    Discovery retains full metadata. The prompt index needs only exact load names
    and purposes, not locations, sources or override bookkeeping.
    """
    rows = sorted(({"name": row["name"], "description": " ".join(row["description"].split())}
                   for row in data["skills"]), key=lambda row: row["name"])

    def catalog(entries, *, has_more=False, shortened=False):
        return {"kind": "skill_catalog", "revision": data["revision"],
                "skills": entries, "total_skills": len(rows), "has_more": has_more,
                "descriptions_shortened": shortened, "discovery_tool": "skill.discover",
                "budget_tokens": budget_tokens, "estimation_method": "utf8-bytes-div-4"}

    full = catalog(rows)
    if skill_catalog_estimated_tokens(full) <= budget_tokens:
        return full

    def shortened_entries(chars):
        entries = []
        for row in rows:
            entry = {"name": row["name"]}
            description = row["description"]
            if chars and description:
                entry["description"] = description[:chars].rstrip()
            entries.append(entry)
        return entries

    minimum = catalog(shortened_entries(0), shortened=True)
    if skill_catalog_estimated_tokens(minimum) <= budget_tokens:
        # Every name fits. Find the longest common description prefix that fits;
        # no alphabetical entry consumes the budget before later names are seen.
        low, high = 0, max((len(row["description"]) for row in rows), default=0)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = catalog(shortened_entries(middle), shortened=True)
            if skill_catalog_estimated_tokens(candidate) <= budget_tokens:
                low = middle
            else:
                high = middle - 1
        return catalog(shortened_entries(low), shortened=True)

    # Even the names do not fit. Explicitly advertise omissions and keep the
    # discover tool usable. A single oversized name must not hide later names.
    partial = catalog([], has_more=bool(rows), shortened=True)
    used_bytes = max(skill_catalog_estimated_tokens(partial),
                     skill_catalog_estimated_tokens(catalog([], shortened=True))) * 4
    if used_bytes > budget_tokens * 4:
        # An exceptionally small model window cannot hold even the wrapper.
        # Omit the catalog message; skill.discover remains in the tool schema.
        return None
    entries = []
    for row in rows:
        entry = {"name": row["name"]}
        extra_bytes = len(json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8")) + (2 if entries else 0)
        if used_bytes + extra_bytes <= budget_tokens * 4:
            entries.append(entry)
            used_bytes += extra_bytes
    return catalog(entries, has_more=len(entries) < len(rows), shortened=True)


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
    source: str = "registered"
    overrides: tuple[str, ...] = ()


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

