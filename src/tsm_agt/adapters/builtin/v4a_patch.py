"""Strict parser for a single-file V4A update patch."""

from __future__ import annotations

from dataclasses import dataclass


class V4APatchSyntaxError(ValueError):
    """The V4A patch payload is malformed or unsuitable for exact replacement."""


@dataclass(frozen=True, slots=True)
class V4APatch:
    path: str
    edits: tuple[dict[str, str], ...]


def parse_v4a_update_patch(patch: str) -> V4APatch:
    """Parse one ``*** Update File`` V4A patch into exact replacement edits."""
    if "\n" not in patch and "\\n" in patch:
        raise V4APatchSyntaxError(
            "patch contains literal \\n instead of actual newline characters"
        )
    lines = patch.splitlines()
    if len(lines) < 4 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise V4APatchSyntaxError(
            "patch must start with *** Begin Patch and end with *** End Patch"
        )
    update = lines[1]
    prefix = "*** Update File: "
    if not update.startswith(prefix) or not update[len(prefix):].strip():
        raise V4APatchSyntaxError(
            "patch must declare exactly one non-empty *** Update File path"
        )
    path = update[len(prefix):].strip()
    edits: list[dict[str, str]] = []
    old_lines: list[str] = []
    new_lines: list[str] = []
    changed = False

    def flush() -> None:
        nonlocal changed
        if not old_lines and not new_lines:
            return
        if not changed:
            raise V4APatchSyntaxError("every V4A hunk must contain a + or - line")
        edits.append({
            "old_text": "\n".join(old_lines),
            "new_text": "\n".join(new_lines),
        })
        old_lines.clear()
        new_lines.clear()
        changed = False

    for line in lines[2:-1]:
        if line.startswith("*** Update File:"):
            raise V4APatchSyntaxError("V4A patch supports exactly one updated file")
        if line.startswith("@@"):
            flush()
            continue
        if line.startswith("-"):
            old_lines.append(line[1:])
            changed = True
            continue
        if line.startswith("+"):
            new_lines.append(line[1:])
            changed = True
            continue
        if line.startswith(" "):
            old_lines.append(line[1:])
            new_lines.append(line[1:])
            continue
        if line == "":
            old_lines.append("")
            new_lines.append("")
            continue
        raise V4APatchSyntaxError(
            "V4A hunk lines must start with +, -, a space, or @@"
        )
    flush()
    if not edits:
        raise V4APatchSyntaxError("V4A patch contains no update hunk")
    return V4APatch(path, tuple(edits))
