"""Deterministic, read-only project onboarding with source evidence."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tsm_agt.ports import WorkspacePathPort

from .configuration import canonical_hash
from .trust import ProjectTrustLevel


ONBOARDING_PHASES = (
    "technology_detection",
    "boundary_and_entry_discovery",
    "command_discovery",
    "trusted_rule_discovery",
    "summary_generation",
)
_EXCLUDED_DIRECTORIES = frozenset({
    ".agent", ".git", ".gradle", ".idea", ".venv", ".vscode",
    "build", "dist", "node_modules", "target", "vendor",
})
_SENSITIVE_NAMES = frozenset({
    ".env", "credentials", "credentials.json", "id_rsa", "id_ed25519",
})
_MAX_FILES = 5000
_MAX_SOURCE_BYTES = 2_000_000


@dataclass(frozen=True, slots=True)
class OnboardingSource:
    path: str
    sha256: str

    def to_data(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> OnboardingSource:
        return cls(str(data["path"]), str(data["sha256"]))


@dataclass(frozen=True, slots=True)
class OnboardingFact:
    fact_id: str
    category: str
    value: str
    sources: tuple[OnboardingSource, ...]
    confidence: str = "deterministic"

    def __post_init__(self) -> None:
        if not self.fact_id.strip() or not self.category.strip() or not self.value.strip():
            raise ValueError("onboarding fact identity, category, and value are required")
        if not self.sources:
            raise ValueError("onboarding facts require at least one source")

    def to_data(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id, "category": self.category,
            "value": self.value,
            "sources": [source.to_data() for source in self.sources],
            "confidence": self.confidence,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> OnboardingFact:
        raw_sources = data.get("sources")
        if not isinstance(raw_sources, list):
            raise ValueError("onboarding fact sources must be a list")
        return cls(
            str(data["fact_id"]), str(data["category"]), str(data["value"]),
            tuple(
                OnboardingSource.from_data(source)
                for source in raw_sources if isinstance(source, Mapping)
            ),
            str(data.get("confidence", "deterministic")),
        )


@dataclass(frozen=True, slots=True)
class OnboardingInventory:
    files: tuple[str, ...]
    sources: Mapping[str, OnboardingSource]
    fingerprint: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class OnboardingCheckpoint:
    schema_version: int
    revision: int
    workspace: str
    subject: str
    project_fingerprint: str
    discovery_fingerprint: str
    completed_phases: tuple[str, ...]
    facts: tuple[OnboardingFact, ...]
    trusted_rules_read: bool
    started_at: datetime
    updated_at: datetime
    inventory_truncated: bool

    @property
    def next_phase(self) -> str | None:
        return next((phase for phase in ONBOARDING_PHASES if phase not in self.completed_phases), None)

    @property
    def checkpoint_hash(self) -> str:
        return canonical_hash(self._content_data())

    def complete_phase(
        self, phase: str, facts: tuple[OnboardingFact, ...],
        *, trusted_rules_read: bool | None = None,
    ) -> OnboardingCheckpoint:
        if phase != self.next_phase:
            raise ValueError(f"onboarding phase out of order: expected {self.next_phase}, got {phase}")
        by_id = {fact.fact_id: fact for fact in self.facts}
        for fact in facts:
            by_id[fact.fact_id] = fact
        return replace(
            self, completed_phases=self.completed_phases + (phase,),
            facts=tuple(sorted(by_id.values(), key=lambda item: item.fact_id)),
            trusted_rules_read=(
                self.trusted_rules_read
                if trusted_rules_read is None else trusted_rules_read
            ),
            updated_at=datetime.now(timezone.utc),
        )

    def _content_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "revision": self.revision,
            "workspace": self.workspace, "subject": self.subject,
            "project_fingerprint": self.project_fingerprint,
            "discovery_fingerprint": self.discovery_fingerprint,
            "completed_phases": list(self.completed_phases),
            "facts": [fact.to_data() for fact in self.facts],
            "trusted_rules_read": self.trusted_rules_read,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "inventory_truncated": self.inventory_truncated,
        }

    def to_data(self) -> dict[str, Any]:
        return {**self._content_data(), "checkpoint_hash": self.checkpoint_hash}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> OnboardingCheckpoint:
        raw_phases = data.get("completed_phases")
        raw_facts = data.get("facts")
        if not isinstance(raw_phases, list) or not isinstance(raw_facts, list):
            raise ValueError("onboarding checkpoint phases and facts must be lists")
        checkpoint = cls(
            int(data.get("schema_version", 1)), int(data["revision"]),
            str(data["workspace"]), str(data["subject"]),
            str(data["project_fingerprint"]), str(data["discovery_fingerprint"]),
            tuple(str(phase) for phase in raw_phases),
            tuple(
                OnboardingFact.from_data(fact)
                for fact in raw_facts if isinstance(fact, Mapping)
            ),
            bool(data.get("trusted_rules_read", False)),
            datetime.fromisoformat(str(data["started_at"])),
            datetime.fromisoformat(str(data["updated_at"])),
            bool(data.get("inventory_truncated", False)),
        )
        if tuple(checkpoint.completed_phases) != ONBOARDING_PHASES[:len(checkpoint.completed_phases)]:
            raise ValueError("onboarding checkpoint phases are not a valid prefix")
        if data.get("checkpoint_hash") and str(data["checkpoint_hash"]) != checkpoint.checkpoint_hash:
            raise ValueError("onboarding checkpoint integrity hash does not match")
        return checkpoint


@dataclass(frozen=True, slots=True)
class ProjectOnboardingSnapshot:
    schema_version: int
    revision: int
    workspace: str
    subject: str
    project_fingerprint: str
    discovery_fingerprint: str
    facts: tuple[OnboardingFact, ...]
    trusted_rules_read: bool
    completed_at: datetime
    inventory_truncated: bool

    @property
    def snapshot_hash(self) -> str:
        return canonical_hash(self._content_data())

    @classmethod
    def from_checkpoint(cls, checkpoint: OnboardingCheckpoint) -> ProjectOnboardingSnapshot:
        if checkpoint.completed_phases != ONBOARDING_PHASES:
            raise ValueError("cannot finalize an incomplete onboarding checkpoint")
        return cls(
            checkpoint.schema_version, checkpoint.revision, checkpoint.workspace,
            checkpoint.subject, checkpoint.project_fingerprint,
            checkpoint.discovery_fingerprint, checkpoint.facts,
            checkpoint.trusted_rules_read, checkpoint.updated_at,
            checkpoint.inventory_truncated,
        )

    def _content_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "revision": self.revision,
            "workspace": self.workspace, "subject": self.subject,
            "project_fingerprint": self.project_fingerprint,
            "discovery_fingerprint": self.discovery_fingerprint,
            "facts": [fact.to_data() for fact in self.facts],
            "trusted_rules_read": self.trusted_rules_read,
            "completed_at": self.completed_at.isoformat(),
            "inventory_truncated": self.inventory_truncated,
        }

    def to_data(self) -> dict[str, Any]:
        return {**self._content_data(), "snapshot_hash": self.snapshot_hash}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ProjectOnboardingSnapshot:
        raw_facts = data.get("facts")
        if not isinstance(raw_facts, list):
            raise ValueError("onboarding snapshot facts must be a list")
        snapshot = cls(
            int(data.get("schema_version", 1)), int(data["revision"]),
            str(data["workspace"]), str(data["subject"]),
            str(data["project_fingerprint"]), str(data["discovery_fingerprint"]),
            tuple(
                OnboardingFact.from_data(fact)
                for fact in raw_facts if isinstance(fact, Mapping)
            ),
            bool(data.get("trusted_rules_read", False)),
            datetime.fromisoformat(str(data["completed_at"])),
            bool(data.get("inventory_truncated", False)),
        )
        if data.get("snapshot_hash") and str(data["snapshot_hash"]) != snapshot.snapshot_hash:
            raise ValueError("onboarding snapshot integrity hash does not match")
        return snapshot


class ProjectOnboardingScanner:
    """Bounded static discovery; never executes project code or commands."""

    def __init__(self, path_service: WorkspacePathPort) -> None:
        self._paths = path_service

    def inventory(self, workspace: Path) -> OnboardingInventory:
        root = self._paths.normalize_workspace(workspace)
        files: list[str] = []
        sources: dict[str, OnboardingSource] = {}
        truncated = False
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_names[:] = sorted(
                name for name in directory_names
                if name not in _EXCLUDED_DIRECTORIES
                and not self._paths.is_link_like(Path(directory) / name)
            )
            for name in sorted(file_names):
                if len(files) >= _MAX_FILES:
                    truncated = True
                    break
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                if self._sensitive(relative) or self._paths.is_link_like(path):
                    continue
                try:
                    resolved = self._paths.resolve_access_path(root, relative).path
                except (PermissionError, ValueError):
                    continue
                if not resolved.is_file():
                    continue
                files.append(relative)
                size = resolved.stat().st_size
                digest = hashlib.sha256()
                digest.update(relative.encode("utf-8"))
                digest.update(str(size).encode("ascii"))
                if size <= _MAX_SOURCE_BYTES:
                    digest.update(resolved.read_bytes())
                else:
                    digest.update(b"oversized-source")
                sources[relative] = OnboardingSource(relative, digest.hexdigest())
            if truncated:
                break
        ordered = tuple(sorted(files))
        fingerprint = canonical_hash({
            "files": [sources[path].to_data() for path in ordered],
            "truncated": truncated,
        })
        return OnboardingInventory(ordered, sources, fingerprint, truncated)

    def run_phase(
        self, phase: str, workspace: Path, inventory: OnboardingInventory,
        trust: ProjectTrustLevel,
    ) -> tuple[tuple[OnboardingFact, ...], bool | None]:
        if phase == "technology_detection":
            return self._technologies(inventory), None
        if phase == "boundary_and_entry_discovery":
            return self._boundaries_and_entries(inventory), None
        if phase == "command_discovery":
            return self._commands(workspace, inventory), None
        if phase == "trusted_rule_discovery":
            trusted = trust is not ProjectTrustLevel.UNTRUSTED
            return self._rules(inventory, trusted), trusted
        if phase == "summary_generation":
            return (), None
        raise ValueError(f"unknown onboarding phase: {phase}")

    @staticmethod
    def _fact(category: str, value: str, sources: tuple[OnboardingSource, ...]) -> OnboardingFact:
        fact_id = canonical_hash({
            "category": category, "value": value,
            "sources": [source.to_data() for source in sources],
        })[:24]
        return OnboardingFact(fact_id, category, value, sources)

    def _technologies(self, inventory: OnboardingInventory) -> tuple[OnboardingFact, ...]:
        marker_map = {
            "build.gradle": ("language:Java/Kotlin", "build_system:Gradle"),
            "build.gradle.kts": ("language:Kotlin", "build_system:Gradle"),
            "settings.gradle": ("build_system:Gradle",),
            "settings.gradle.kts": ("build_system:Gradle",),
            "pyproject.toml": ("language:Python", "build_system:Python/pyproject"),
            "package.json": ("language:JavaScript/TypeScript", "build_system:Node.js"),
            "Cargo.toml": ("language:Rust", "build_system:Cargo"),
            "go.mod": ("language:Go", "build_system:Go modules"),
            "pom.xml": ("language:Java", "build_system:Maven"),
        }
        detected: dict[str, list[OnboardingSource]] = {}
        for path in inventory.files:
            name = Path(path).name
            for value in marker_map.get(name, ()):
                detected.setdefault(value, []).append(inventory.sources[path])
        suffix_map = {
            ".kt": "language:Kotlin", ".java": "language:Java",
            ".py": "language:Python", ".ts": "language:TypeScript",
            ".tsx": "language:TypeScript", ".js": "language:JavaScript",
            ".rs": "language:Rust", ".go": "language:Go",
        }
        for path in inventory.files:
            value = suffix_map.get(Path(path).suffix.lower())
            if value and len(detected.setdefault(value, [])) < 5:
                detected[value].append(inventory.sources[path])
        return tuple(
            self._fact(*value.split(":", 1), tuple(sources))
            for value, sources in sorted(detected.items())
        )

    def _boundaries_and_entries(self, inventory: OnboardingInventory) -> tuple[OnboardingFact, ...]:
        facts: list[OnboardingFact] = []
        roots: dict[str, OnboardingSource] = {}
        entry_names = {
            "main.py", "app.py", "manage.py", "index.ts", "index.js",
            "main.ts", "main.js", "MainActivity.kt", "MainActivity.java",
            "Application.kt", "Application.java", "lib.rs", "main.rs",
        }
        for path in inventory.files:
            parts = Path(path).parts
            if parts and parts[0] in {"app", "src", "lib", "test", "tests"}:
                roots.setdefault(parts[0], inventory.sources[path])
            if Path(path).name in entry_names:
                facts.append(self._fact("entry_candidate", path, (inventory.sources[path],)))
        facts.extend(
            self._fact("source_boundary", root, (source,))
            for root, source in sorted(roots.items())
        )
        return tuple(sorted(facts, key=lambda fact: fact.fact_id))

    def _commands(self, workspace: Path, inventory: OnboardingInventory) -> tuple[OnboardingFact, ...]:
        commands: dict[str, OnboardingSource] = {}
        for path in inventory.files:
            source = inventory.sources[path]
            name = Path(path).name
            try:
                resolved = self._paths.resolve_access_path(workspace, path).path
                if resolved.stat().st_size > _MAX_SOURCE_BYTES:
                    continue
                if name == "package.json":
                    data = json.loads(resolved.read_text(encoding="utf-8"))
                    scripts = data.get("scripts") if isinstance(data, dict) else None
                    if isinstance(scripts, dict):
                        runner = "npm run"
                        for script in sorted(scripts):
                            if script in {"build", "test", "lint", "format", "check", "typecheck"}:
                                commands[f"{runner} {script}"] = source
                elif name == "pyproject.toml":
                    data = tomllib.loads(resolved.read_text(encoding="utf-8"))
                    tool = data.get("tool") if isinstance(data, dict) else None
                    if isinstance(tool, dict):
                        if "pytest" in tool:
                            commands["python -m pytest"] = source
                        if "ruff" in tool:
                            commands["ruff check ."] = source
                elif name in {"gradlew", "gradlew.bat"}:
                    prefix = "gradlew.bat" if name.endswith(".bat") else "./gradlew"
                    commands[f"{prefix} build"] = source
                    commands[f"{prefix} test"] = source
                elif name == "Cargo.toml":
                    commands["cargo build"] = source
                    commands["cargo test"] = source
                elif name == "go.mod":
                    commands["go test ./..."] = source
                elif name == "pom.xml":
                    commands["mvn test"] = source
            except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError):
                continue
        return tuple(
            self._fact("candidate_command", command, (source,))
            for command, source in sorted(commands.items())
        )

    def _rules(
        self, inventory: OnboardingInventory, trusted: bool,
    ) -> tuple[OnboardingFact, ...]:
        if not trusted:
            return ()
        # Project Instructions use one explicit path and are loaded by their
        # dedicated trust-gated loader.  Onboarding deliberately does not scan
        # arbitrary Markdown files as authority.
        return ()

    @staticmethod
    def _sensitive(relative: str) -> bool:
        path = Path(relative)
        lowered = path.name.lower()
        return (
            lowered in _SENSITIVE_NAMES or lowered.startswith(".env")
            or path.suffix.lower() in {".key", ".jks", ".keystore", ".p12", ".pem", ".pfx"}
        )
