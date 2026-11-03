"""Merge shipped instruction resources with user-owned Skill discovery."""
from __future__ import annotations

from dataclasses import replace
from importlib.resources import files
from pathlib import Path
import hashlib
import json

import yaml

from tsm_agt.core.skills import Skill, SkillKind, SkillRegistry
from tsm_agt.ports.skill_files import SkillFilesPort, SkillResourceError, validate_skill_resource


BUILTIN_SKILLS = {"verify-change": "verify_change"}


def _bundled_resource(folder: str, resource: str):
    root = files("tsm_agt").joinpath("bundled_skills", folder)
    if root.joinpath("SKILL.md").is_file():
        if isinstance(root, Path) and not root.joinpath(resource).resolve().is_relative_to(root.resolve()):
            raise SkillResourceError("SKILL_RESOURCE_FORBIDDEN", "Skill resource escapes its package directory", "Choose a file inside the Skill directory.")
        return root.joinpath(resource)
    # Editable/source checkouts retain the single authored source. Installed
    # wheels always take the package-resource branch, independent of cwd.
    checkout = Path(__file__).resolve().parents[3]
    if (checkout / "pyproject.toml").is_file():
        root = checkout / "skills" / folder
        target = (root / resource).resolve()
        if not target.is_relative_to(root.resolve()):
            raise SkillResourceError("SKILL_RESOURCE_FORBIDDEN", "Skill resource escapes its source directory", "Choose a file inside the Skill directory.")
        return target
    raise FileNotFoundError("Bundled Skill resources are missing from the installation")


class SkillCatalog:
    def __init__(self, user_store: SkillFilesPort) -> None:
        self.user_store = user_store
        self.issues: list[str] = []

    def disabled(self) -> list[str]:
        values = self.user_store.settings().get("disabled_builtin_skills", [])
        if not isinstance(values, list) or any(not isinstance(name, str) for name in values):
            raise ValueError("disabled_builtin_skills must be a list of names")
        unknown = set(values) - BUILTIN_SKILLS.keys()
        if unknown:
            raise ValueError("Unknown bundled Skills: " + ", ".join(sorted(unknown)))
        return values

    def discover(self) -> tuple[Skill, ...]:
        selected = {skill.name: skill for skill in self.user_store.discover()}
        self.issues = list(self.user_store.issues)
        try:
            disabled = self.disabled()
        except (OSError, ValueError) as error:
            self.issues.append(str(error))
            disabled = list(BUILTIN_SKILLS)
        for name, folder in BUILTIN_SKILLS.items():
            if name in disabled:
                continue
            try:
                text = _bundled_resource(folder, "SKILL.md").read_text(encoding="utf-8")
                metadata = yaml.safe_load(text.split("---", 2)[1])
                if metadata["name"] != name or not isinstance(metadata["description"], str):
                    raise ValueError(f"Invalid bundled Skill metadata: {name}")
                bundled = Skill(
                    id=name, name=name, description=metadata["description"], kind=SkillKind.PLAN,
                    args_schema={"type": "object", "properties": {}}, output_schema=None,
                    handler=None, plan_path=f"builtin://{name}/SKILL.md",
                    tags=tuple(metadata.get("tags", [])), safety_level="read_only",
                    approval_required=False, source="builtin",
                )
                if name in selected:
                    selected[name] = replace(selected[name], overrides=("builtin",))
                    self.issues.append(f"用户 Skill {name} 覆盖内置版本，使用 {selected[name].plan_path}")
                else:
                    selected[name] = bundled
            except (OSError, ValueError, KeyError, IndexError, TypeError, yaml.YAMLError) as error:
                self.issues.append(f"内置 Skill {name}: {error}")
        return tuple(selected.values())

    def refresh(self, registry: SkillRegistry) -> tuple[Skill, ...]:
        registry.replace_discovered(self.discover())
        return registry.list_all()

    def read(self, skill: Skill, resource: str = "SKILL.md") -> dict:
        validate_skill_resource(resource)
        if skill.source != "builtin":
            data = self.user_store.read(skill, resource)
            return {**data, "source": skill.source, "overrides": list(skill.overrides)}
        target = _bundled_resource(BUILTIN_SKILLS[skill.name], resource)
        if not target.is_file():
            if target.is_dir():
                raise SkillResourceError("SKILL_RESOURCE_NOT_FILE", "resource points to a directory, not a text file.", "Pass a complete relative file path, such as references/workflow.md.")
            raise FileNotFoundError(f"Skill resource not found: {resource}")
        raw = target.read_bytes()
        if len(raw) > 256_000:
            raise SkillResourceError("SKILL_RESOURCE_TOO_LARGE", "Skill resource exceeds 256 KB", "Select a smaller reference file or split the resource before loading it.")
        return {"name": skill.name, "source": "builtin", "overrides": [],
                "directory": f"builtin://{skill.name}",
                "path": f"builtin://{skill.name}/{resource}",
                "content": raw.decode("utf-8"),
                "guidance": "Follow these instructions within the current request. Loading does not execute checks; ordinary tool permissions still apply."}

    def model_data(self, skills: tuple[Skill, ...]) -> dict:
        entries = [{"name": skill.name, "description": skill.description,
                    "source": skill.source, "location": skill.plan_path,
                    "overrides": list(skill.overrides)} for skill in skills if skill.kind is SkillKind.PLAN]
        versioned = []
        for skill in skills:
            if skill.kind is SkillKind.PLAN:
                try:
                    content = self.read(skill)["content"]
                except (OSError, ValueError, UnicodeError) as error:
                    content = f"unavailable:{type(error).__name__}"
                versioned.append([skill.name, hashlib.sha256(content.encode()).hexdigest()])
        revision = hashlib.sha256(json.dumps([entries, versioned], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {"kind": "skill_catalog", "revision": revision, "skills": entries}
