"""User-owned Skill files, discovery settings, and a rebuildable catalog."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from collections.abc import Mapping

import yaml

from tsm_agt.core.skills import Skill, SkillKind, SkillRegistry
from tsm_agt.adapters.skill_installations import SkillInstallations


class UserSkillsStore:
    """Only installs into our own directory; configured sources are read-only."""

    def __init__(self, home: Path | None = None) -> None:
        self.directory = ((home or Path.home()) / ".tsm-agent").resolve()
        self.install_directory = self.directory / "skills"
        self.settings_path = self.directory / "settings.json"
        self.installations = SkillInstallations(self.directory)
        self.issues: list[str] = []

    def settings(self) -> dict:
        if not self.settings_path.exists():
            return {}
        data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Skill settings must be a JSON object")
        return data

    def discovery_directories(self) -> tuple[Path, ...]:
        paths = self.settings().get("skill_discovery_directories", [])
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise ValueError("skill_discovery_directories must be a list of paths")
        return tuple(dict.fromkeys([
            self.install_directory.resolve(),
            *(Path(p).expanduser().resolve() for p in paths),
        ]))

    def save_directories(self, paths: list[str]) -> None:
        if not isinstance(paths, list) or any(not isinstance(p, str) or not p.strip() for p in paths):
            raise ValueError("请选择有效的 Skills 目录")
        canonical: list[str] = []
        for raw in paths:
            path = Path(raw).expanduser().resolve()
            if path == self.install_directory.resolve():
                continue
            if not path.is_dir():
                raise ValueError(f"目录不存在：{path}")
            if str(path) not in canonical:
                canonical.append(str(path))
        data = self.settings()
        data["skill_discovery_directories"] = canonical
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix=".settings-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, self.settings_path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def parse(path: Path) -> Skill:
        if path.stat().st_size > 256_000:
            raise ValueError("SKILL.md exceeds 256 KB")
        text = path.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        yaml_document = False
        if lines and lines[0].strip() == "---":
            end = next((i for i in range(1, len(lines)) if lines[i].strip() in {"---", "..."}), None)
            if end is None:
                # The original demo was a YAML-only document starting with
                # '---'. YAML does not require an explicit document terminator.
                # Accept it only if the entire file is valid Skill metadata;
                # never guess where an unclosed Markdown header ends.
                yaml_document = True
                yaml_text = text
            else:
                yaml_text = "\n".join(lines[1:end])
        else:
            # Compatibility with the original YAML-only demo SKILL.md.
            yaml_text = text
        try:
            metadata = yaml.safe_load(yaml_text)
        except yaml.YAMLError as error:
            if yaml_document:
                raise ValueError(
                    "SKILL.md 顶部的 YAML 元数据未闭合：请在 name、description 等字段后、"
                    "Markdown 正文前添加独占一行的 ---；旧版纯 YAML 文件需整份内容符合 YAML 格式。"
                ) from error
            raise ValueError(f"Invalid SKILL.md YAML: {error}") from error
        if not isinstance(metadata, dict):
            raise ValueError("SKILL.md requires YAML name and description")
        name, description = metadata.get("name"), metadata.get("description")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", name):
            raise ValueError("Invalid Skill name")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("SKILL.md requires a description")
        if metadata.get("kind", "plan") != "plan":
            raise ValueError("Only instruction Skills can be discovered from files")
        tags = metadata.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ValueError("Skill tags must be a list of strings")
        return Skill(
            id=name, name=name, description=description.strip(), kind=SkillKind.PLAN,
            args_schema={"type": "object", "properties": {}}, output_schema=None,
            handler=None, plan_path=str(path.resolve()), tags=tuple(tags),
            safety_level="read_only", approval_required=False,
        )

    def discover(self) -> tuple[Skill, ...]:
        self.issues = []
        found: dict[str, Skill] = {}
        try:
            directories = self.discovery_directories()
        except (OSError, ValueError) as error:
            self.issues.append(f"无法读取 Skills 设置：{error}")
            directories = (self.install_directory.resolve(),)
        seen: set[Path] = set()
        visited_directories: set[Path] = set()
        for directory in directories:
            if not directory.is_dir():
                if directory != self.install_directory.resolve():
                    self.issues.append(f"目录不存在：{directory}")
                continue
            # Other agent installers commonly link Skill folders into their
            # catalog. Follow those links while preventing recursive cycles.
            for root, children, files in os.walk(directory, followlinks=True):
                canonical_root = Path(root).resolve()
                if canonical_root in visited_directories:
                    children[:] = []
                    continue
                visited_directories.add(canonical_root)
                children[:] = sorted(child for child in children if not child.startswith("."))
                if "SKILL.md" not in files:
                    continue
                path = (Path(root) / "SKILL.md").resolve()
                if path in seen:
                    continue
                seen.add(path)
                try:
                    skill = self.parse(path)
                    if skill.name in found:
                        self.issues.append(f"重复 Skill {skill.name}，使用 {found[skill.name].plan_path}，忽略 {path}")
                    else:
                        found[skill.name] = skill
                except (OSError, ValueError, yaml.YAMLError) as error:
                    self.issues.append(f"{path}: {error}")
        return tuple(found.values())

    def refresh(self, registry: SkillRegistry) -> None:
        registry.replace_discovered(self.discover())

    @staticmethod
    def ignored_upload_file(filename: str) -> bool:
        """Finder/Windows metadata and Git history are not Skill resources."""
        return any(part in {".DS_Store", "__MACOSX", ".git", "Thumbs.db", "desktop.ini"}
                   for part in PurePosixPath(filename).parts)

    @staticmethod
    def same_files(source: Path, target: Path) -> bool:
        """An identical retry is successful; changed content never overwrites."""
        installed = list(target.rglob("*"))
        if any(path.is_symlink() for path in installed):
            return False
        source_files = {path.relative_to(source) for path in source.rglob("*") if path.is_file()}
        target_files = {
            path.relative_to(target) for path in installed
            if path.is_file() and not UserSkillsStore.ignored_upload_file(path.relative_to(target).as_posix())
        }
        return source_files == target_files and all(
            (source / path).read_bytes() == (target / path).read_bytes()
            for path in source_files
        )

    def install_upload(self, files: Mapping[str, bytes]) -> Skill:
        files = {name: content for name, content in files.items() if not self.ignored_upload_file(name)}
        if not files or len(files) > 500 or sum(map(len, files.values())) > 20_000_000:
            raise ValueError("Skill 上传最多 500 个文件、20 MB")
        self.install_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.install_directory, prefix=".upload-") as temporary:
            stage = Path(temporary)
            for filename, content in files.items():
                path = PurePosixPath(filename)
                if path.is_absolute() or ".." in path.parts or "\\" in filename or not path.name:
                    raise ValueError(f"非法上传路径：{filename}")
                destination = stage.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            manifests = list(stage.rglob("SKILL.md"))
            if len(manifests) != 1:
                raise ValueError("请选择只包含一个 SKILL.md 的 Skill 目录")
            try:
                skill = self.parse(manifests[0])
            except ValueError as error:
                relative_manifest = manifests[0].relative_to(stage).as_posix()
                raise ValueError(f"{relative_manifest}: {error}") from error
            target = self.install_directory / skill.name
            if target.exists():
                if target.is_dir() and not target.is_symlink() and self.same_files(manifests[0].parent, target):
                    return self.parse(target / "SKILL.md")
                raise FileExistsError(
                    f"同名 Skill 已安装且内容不同：{skill.name}（{target}）。现有文件未被覆盖。"
                )
            # Keep relative resources beside the manifest; the upload wrapper
            # folder is not part of the installed identity.
            manifests[0].parent.rename(target)
        return self.parse(target / "SKILL.md")

    def install_path(self, manifest: Path) -> Skill:
        root = manifest.expanduser().resolve().parent
        files: dict[str, bytes] = {}
        total_bytes = 0
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError("安装源不能包含符号链接")
            if path.is_file():
                total_bytes += path.stat().st_size
                if len(files) >= 500 or total_bytes > 20_000_000:
                    raise ValueError("Skill 安装最多 500 个文件、20 MB")
                files[path.relative_to(root).as_posix()] = path.read_bytes()
        return self.install_upload(files)

    def read(self, skill: Skill, resource: str = "SKILL.md") -> dict:
        if not skill.plan_path:
            raise ValueError("Skill has no instruction file")
        manifest = Path(skill.plan_path)
        root = manifest.parent.resolve()
        relative = PurePosixPath(resource)
        if relative.is_absolute() or ".." in relative.parts or "\\" in resource:
            raise ValueError("资源路径必须在 Skill 目录内")
        if any(part.startswith(".") for part in relative.parts):
            raise ValueError("不能读取隐藏的 Skill 资源")
        target = (root / resource).resolve()
        if not target.is_relative_to(root):
            raise ValueError("资源路径超出 Skill 目录")
        if target.stat().st_size > 256_000:
            raise ValueError("资源超过 256 KB，请拆分后读取")
        return {
            "name": skill.name, "directory": str(root), "path": str(target),
            "content": target.read_text(encoding="utf-8"),
            "guidance": "Follow these Skill instructions for the current task. Relative resources are based on directory; use skill.load with resource to read them. Loading instructions does not execute steps or complete the task. Existing tool permissions still apply.",
        }
