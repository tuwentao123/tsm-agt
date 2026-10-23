"""Plan flat text replacements with the shared, unique line matcher."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .patch_match import AmbiguousLineMatch, locate_lines
from .workspace import WorkspacePatchConflict


@dataclass(frozen=True)
class TextEditPlan:
    content: str
    matches: tuple[dict[str, Any], ...]


def plan_text_edits(
    path: str, content: str, edits: tuple[Mapping[str, str], ...],
) -> TextEditPlan:
    """Exact substring matching first; only multiline quotes get line fallback.

    All replacements are planned before a caller writes anything. Substring
    edits retain their exact semantics. Fallback replaces whole source lines,
    never a guessed substring inside one, and reports the actual affected span.
    """
    matches = []
    for index, edit in enumerate(edits):
        old, new = edit["old_text"], edit["new_text"]
        if not old:
            raise WorkspacePatchConflict(path, index, "old_text must not be empty for an existing file")
        count = content.count(old)
        if count > 1:
            raise WorkspacePatchConflict(path, index, f"old_text is ambiguous: found {count} matches")
        if count == 1:
            content = content.replace(old, new, 1)
            continue
        if "\\n" in old and "\n" not in old and content.count(old.replace("\\n", "\n")) == 1:
            raise WorkspacePatchConflict(path, index, "old_text contains literal \\n; use actual newline characters")
        source_lines = content.splitlines(keepends=True)
        match = None
        if "\n" in old:
            try:
                match = locate_lines(
                    [line.rstrip("\r\n") for line in source_lines],
                    old.splitlines(), allow_similarity=(old != new),
                )
            except AmbiguousLineMatch as error:
                raise WorkspacePatchConflict(path, index, "old_text has ambiguous line matches", diagnostics={
                    "match_failure": "ambiguous", "candidate_count": len(error.matches),
                    "candidate_count_is_lower_bound": True,
                    "match_strategy": error.matches[0].strategy,
                    "candidate_locations": [{"file_line": row.start + 1, "end_line": row.end}
                                            for row in error.matches],
                }) from error
        if match is None:
            raise WorkspacePatchConflict(path, index, "old_text was not found in the current file")
        start = sum(map(len, source_lines[:match.start]))
        end = start + sum(map(len, source_lines[match.start:match.end]))
        if not old.endswith(("\n", "\r")):
            end -= len(source_lines[match.end - 1]) - len(source_lines[match.end - 1].rstrip("\r\n"))
        # Keep uniform Windows line endings in newly inserted lines, as V4A does.
        if "\r\n" in content and "\n" not in content.replace("\r\n", ""):
            new = new.replace("\r\n", "\n").replace("\n", "\r\n")
        matches.append({"edit_index": index, "strategy": match.strategy,
                        "start_line": match.start + 1, "end_line": match.end,
                        "approximate": match.strategy == "anchored_similarity"})
        content = content[:start] + new + content[end:]
    return TextEditPlan(content, tuple(matches))
