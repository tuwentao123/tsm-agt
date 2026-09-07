"""Compact large patches while keeping small changes directly reviewable."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus,
    ToolArgumentPresentation,
)


class AdaptiveToolArgumentPresenter:
    """Project-neutral UI policy for mutation and ordinary tool arguments.

    Small patches are printed in full. Large patches list every affected file but
    show only a few representative edit excerpts. These limits affect display
    only: the Kernel approval hash and execution retain the complete payload.
    """

    descriptor = AdapterDescriptor(
        "builtin.adaptive-tool-argument-presentation", "1.0.0",
        "ToolArgumentPresenterPort", "1.0",
        frozenset({"project-neutral", "adaptive-patch-preview"}),
    )

    def __init__(
        self, *, max_full_files: int = 2, max_full_edits: int = 4,
        max_full_lines: int = 40, max_full_characters: int = 4096,
        max_examples: int = 3, max_example_lines: int = 8,
        max_example_characters: int = 600,
    ) -> None:
        limits = (
            max_full_files, max_full_edits, max_full_lines,
            max_full_characters, max_examples, max_example_lines,
            max_example_characters,
        )
        if any(value < 1 for value in limits):
            raise ValueError("tool presentation limits must be positive")
        self._max_full_files = max_full_files
        self._max_full_edits = max_full_edits
        self._max_full_lines = max_full_lines
        self._max_full_characters = max_full_characters
        self._max_examples = max_examples
        self._max_example_lines = max_example_lines
        self._max_example_characters = max_example_characters
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "tool argument presenter ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    def present(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> ToolArgumentPresentation:
        if tool_name in {"core.apply_patch", "core.apply_patches"}:
            patches = self._patches(tool_name, arguments)
            if patches:
                return self._present_patches(patches)
        return self._present_generic(arguments)

    @staticmethod
    def _patches(
        tool_name: str, arguments: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], ...]:
        if tool_name == "core.apply_patch":
            return (arguments,) if isinstance(arguments.get("edits"), Sequence) else ()
        raw = arguments.get("patches")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()
        return tuple(item for item in raw if isinstance(item, Mapping))

    def _present_patches(
        self, patches: tuple[Mapping[str, Any], ...]
    ) -> ToolArgumentPresentation:
        edit_records: list[tuple[str, int, str, str]] = []
        file_rows: list[tuple[str, int, int, int]] = []
        total_characters = 0
        total_lines = 0
        for patch in patches:
            path = str(patch.get("path", "<unknown>"))
            raw_edits = patch.get("edits", ())
            edits = (
                raw_edits if isinstance(raw_edits, Sequence)
                and not isinstance(raw_edits, (str, bytes)) else ()
            )
            removed = 0
            added = 0
            valid_edits = 0
            for index, edit in enumerate(edits, 1):
                if not isinstance(edit, Mapping):
                    continue
                old = str(edit.get("old_text", ""))
                new = str(edit.get("new_text", ""))
                old_lines = self._line_count(old)
                new_lines = self._line_count(new)
                removed += old_lines
                added += new_lines
                total_lines += old_lines + new_lines
                total_characters += len(old) + len(new)
                valid_edits += 1
                edit_records.append((path, index, old, new))
            file_rows.append((path, valid_edits, removed, added))

        is_small = (
            len(file_rows) <= self._max_full_files
            and len(edit_records) <= self._max_full_edits
            and total_lines <= self._max_full_lines
            and total_characters <= self._max_full_characters
        )
        header = (
            f"补丁规模：{len(file_rows)} 个文件，{len(edit_records)} 处改动，"
            f"-{sum(row[2] for row in file_rows)}/+{sum(row[3] for row in file_rows)} 行"
        )
        if is_small:
            lines = [header, "完整补丁："]
            for path, index, old, new in edit_records:
                lines.extend(self._full_edit(path, index, old, new))
            return ToolArgumentPresentation(
                text="\n".join(lines), mode="full",
                visible_arguments={
                    "presentation": "full", "files": len(file_rows),
                    "edits": len(edit_records),
                },
            )

        lines = [header, "改动清单："]
        lines.extend(
            f"- {path}：{edits} 处，-{removed}/+{added} 行"
            for path, edits, removed, added in file_rows
        )
        shown = min(len(edit_records), self._max_examples)
        lines.append(f"代表性改动示例（{shown}/{len(edit_records)}）：")
        for path, index, old, new in edit_records[:shown]:
            lines.extend(self._example_edit(path, index, old, new))
        omitted = len(edit_records) - shown
        if omitted:
            lines.append(f"…其余 {omitted} 处改动未在终端展开。")
        lines.append(
            "以上是精简预览；授权绑定并执行完整补丁，授权前不会写文件。"
        )
        return ToolArgumentPresentation(
            text="\n".join(lines), mode="summary",
            visible_arguments={
                "presentation": "summary",
                "files": [
                    {"path": path, "edits": edits,
                     "removed_lines": removed, "added_lines": added}
                    for path, edits, removed, added in file_rows
                ],
                "total_edits": len(edit_records), "examples_shown": shown,
            },
        )

    @staticmethod
    def _line_count(text: str) -> int:
        return len(text.splitlines()) if text else 0

    @staticmethod
    def _full_edit(path: str, index: int, old: str, new: str) -> list[str]:
        return [
            f"--- {path}（改动 {index}，原内容）", old or "<空>",
            f"+++ {path}（改动 {index}，新内容）", new or "<空>",
        ]

    def _example_edit(
        self, path: str, index: int, old: str, new: str
    ) -> list[str]:
        return [
            f"--- {path}（示例 {index}，原内容）", self._excerpt(old),
            f"+++ {path}（示例 {index}，新内容）", self._excerpt(new),
        ]

    def _excerpt(self, text: str) -> str:
        if not text:
            return "<空>"
        source_lines = text.splitlines()
        clipped = source_lines[:self._max_example_lines]
        rendered = "\n".join(clipped)
        truncated = len(source_lines) > len(clipped)
        if len(rendered) > self._max_example_characters:
            rendered = rendered[:self._max_example_characters]
            truncated = True
        return rendered + ("\n…" if truncated else "")

    @staticmethod
    def _present_generic(arguments: Mapping[str, Any]) -> ToolArgumentPresentation:
        safe_arguments = {
            key: value for key, value in arguments.items()
            if not any(
                secret in key.casefold()
                for secret in ("secret", "token", "password", "api_key", "apikey")
            )
        }
        try:
            rendered = json.dumps(
                safe_arguments, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            rendered = "参数已绑定，但无法生成文本预览"
        if len(rendered) > 2000:
            rendered = rendered[:2000] + "…"
        return ToolArgumentPresentation(
            text=rendered, mode="full", visible_arguments=safe_arguments
        )
