"""Plan single-file V4A edits with exact-first, unique, layered matching.

All hunks are rendered in memory. The workspace transaction owns hash checks,
approval and committing; an ambiguous or unmatched hunk never writes a file.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from heapq import nlargest
import re
from typing import Any

from .patch_match import AmbiguousLineMatch, LineMatch, locate_lines
from .workspace import WorkspacePatchConflict


class V4APatchFormatError(ValueError):
    """The V4A patch envelope or hunk body is malformed."""


@dataclass(frozen=True)
class V4APlan:
    content: str
    matches: tuple[dict[str, Any], ...]


_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_UPDATE = "*** Update File: "
_ADD = "*** Add File:"
_DELETE = "*** Delete File:"


@dataclass(frozen=True)
class _Mismatch:
    text: str
    section_offset: int
    kind: str
    file_index: int | None = None


def _candidate_locations(
    original: str, mismatch: _Mismatch, cursor: int,
) -> list[dict[str, Any]]:
    """Bounded error hints; these snippets never select a write location."""
    lines = original.splitlines()
    if not lines:
        return []
    points: list[tuple[int, str]] = []
    if mismatch.file_index is not None:
        kind = "end_of_file" if mismatch.file_index >= len(lines) else "after_matched_context"
        points.append((min(mismatch.file_index, len(lines) - 1), kind))
    expected = mismatch.text.strip()[:512]
    # A matching line before the moving cursor can signal out-of-order hunks.
    if not points:
        for index, line in enumerate(lines[:cursor]):
            if line.strip() == expected:
                points.append((index, "exact_text_before_cursor"))
                break
    words = set(re.findall(r"\w+", expected))
    # Once the matcher has a concrete location, unrelated lexical suggestions
    # obscure the comparison the model needs to repair the old side of its hunk.
    if words and not points:
        def lexical_candidates():
            for index, line in enumerate(lines):
                text = line.strip()[:512]
                tokens = set(re.findall(r"\w+", text))
                common = len(words & tokens)
                if common:
                    yield (2 * common / (len(words) + len(tokens)), -index, text)

        # Expensive character similarity runs on at most eight short lines.
        ranked = []
        for overlap, negative_index, text in nlargest(8, lexical_candidates()):
            similarity = SequenceMatcher(None, expected, text, autojunk=False).ratio()
            if similarity >= 0.65 or overlap >= 0.8:
                ranked.append((overlap + similarity, negative_index))
        points.extend((-index, "similar_text") for _, index in sorted(ranked, reverse=True))

    locations: list[dict[str, Any]] = []
    for index, kind in points:
        if any(item["start_line"] <= index + 1 <= item["end_line"] for item in locations):
            continue
        start, end = max(0, index - 2), min(len(lines), index + 3)
        locations.append({
            "match_kind": kind, "line_number": index + 1,
            "start_line": start + 1, "end_line": end,
            "content": "\n".join(line[:200] for line in lines[start:end]),
            "content_truncated": any(len(line) > 200 for line in lines[start:end]),
        })
        if len(locations) == 3:
            break
    return locations


def _context_conflict(
    path: str, original: str, mismatch: _Mismatch, *,
    hunk_number: int, patch_line_number: int, cursor: int,
    hunk_changes_content: bool | None = None,
) -> WorkspacePatchConflict:
    line_kind, marker = {
        "del": ("deletion", "-"),
        "context": ("context", " "),
        "anchor": ("anchor", "@@"),
    }[mismatch.kind]
    lines = original.splitlines()
    file_index = mismatch.file_index
    actual = lines[file_index] if file_index is not None and file_index < len(lines) else None
    at_eof = file_index is not None and file_index >= len(lines)
    actual_line_number = min(file_index, len(lines)) + 1 if file_index is not None else None
    quoted = mismatch.text.strip()
    if len(quoted) > 160:
        quoted = quoted[:160] + "…"
    diagnostics = {
        "path": path, "hunk_number": hunk_number,
        "patch_line_number": patch_line_number,
        "patch_line_kind": line_kind, "patch_marker": marker,
        "match_side": "old_file",
        "expected_context": mismatch.text[:512],
        "expected_context_truncated": len(mismatch.text) > 512,
        "actual_line": actual[:512] if actual is not None else None,
        "actual_line_truncated": actual is not None and len(actual) > 512,
        "actual_line_number": actual_line_number,
        "actual_line_at_eof": at_eof,
        "actual_location_basis": "partial_hunk_match" if file_index is not None else None,
        "snapshot_sha256": sha256(original.encode("utf-8")).hexdigest(),
        "search_start_line": cursor + 1,
        "candidate_locations": _candidate_locations(original, mismatch, cursor),
        "candidates_are_suggestions": True,
        "diagnostics_scope": "first_conflict",
    }
    if hunk_changes_content is not None:
        diagnostics["hunk_changes_content"] = hunk_changes_content
    reason = (
        f"patch line {patch_line_number} is a {line_kind} ({marker!r}) on the "
        f"old-file matching side and could not be matched. Patch old text: {quoted!r}. "
    )
    if actual is not None:
        shown = actual.strip()
        if len(shown) > 160:
            shown = shown[:160] + "…"
        reason += f"File line {file_index + 1} at the best partial match: {shown!r}. "
    elif at_eof:
        reason += f"The partial match reached end of file at line {actual_line_number}. "
    reason += (
        "Read the current target and quote it exactly in the old '-' / context "
        "lines (or @@ anchor). Changing only '+' lines cannot repair this mismatch."
    )
    return WorkspacePatchConflict(
        path, 0, reason,
        diagnostics=diagnostics,
    )


def parse_v4a_add(patch: str) -> tuple[str, str] | None:
    """Parse an Add File patch, or return None for an Update patch."""
    if "\n" not in patch and "\\n" in patch:
        raise V4APatchFormatError(
            "patch contains literal \\n instead of actual newline characters"
        )
    lines = patch.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if len(lines) < 3 or lines[0].strip() != _BEGIN or lines[-1].strip() != _END:
        raise V4APatchFormatError(
            "patch must start with *** Begin Patch and end with *** End Patch"
        )
    if not lines[1].startswith(_ADD):
        return None
    path = lines[1][len(_ADD):].strip()
    if not path:
        raise V4APatchFormatError("Add File requires a non-empty path")
    body = lines[2:-1]
    if any(not line.startswith("+") for line in body):
        raise V4APatchFormatError(
            "Add File content lines must start with +; only one file is supported"
        )
    return path, "" if not body else "\n".join(line[1:] for line in body) + "\n"


def _is_blank(line: str) -> bool:
    return line.strip() == ""


def _lines_equal(file_line: str, patch_line: str) -> bool:
    # Whitespace-insensitive per line, matching Codex's most lenient fuzz level.
    return file_line.strip() == patch_line.strip()


def _split_envelope(patch: str) -> tuple[str, list[str]]:
    """Validate the patch envelope and return ``(path, body_lines)``."""
    if "\n" not in patch and "\\n" in patch:
        raise V4APatchFormatError(
            "patch contains literal \\n instead of actual newline characters"
        )
    lines = patch.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if len(lines) < 3 or lines[0].strip() != _BEGIN or lines[-1].strip() != _END:
        raise V4APatchFormatError(
            "patch must start with *** Begin Patch and end with *** End Patch"
        )
    header = lines[1]
    if not header.startswith(_UPDATE) or not header[len(_UPDATE):].strip():
        raise V4APatchFormatError(
            "patch must declare exactly one non-empty *** Update File path"
        )
    path = header[len(_UPDATE):].strip()
    body = lines[2:-1]
    for line in body:
        if line.startswith(_UPDATE) or line.startswith(_ADD) or line.startswith(_DELETE):
            raise V4APatchFormatError(
                "V4A patch supports exactly one updated file (no Add/Delete/Move)"
            )
    return path, body


def _read_section(body: list[str], index: int) -> tuple[list[tuple[str, str]], int]:
    """Read one hunk section as ordered ``(kind, text)`` lines until @@/***."""
    section: list[tuple[str, str]] = []
    while index < len(body):
        raw = body[index]
        if raw.startswith("@@") or raw.startswith("***"):
            break
        index += 1
        if raw.startswith("+"):
            section.append(("ins", raw[1:]))
        elif raw.startswith("-"):
            section.append(("del", raw[1:]))
        elif raw.startswith(" "):
            section.append(("context", raw[1:]))
        elif raw == "":
            section.append(("context", ""))
        else:
            raise V4APatchFormatError(
                "V4A hunk lines must start with +, -, a space, or @@"
            )
    return section, index


def _locate_anchor(orig: list[str], anchor: str, start: int) -> int:
    for position in range(max(start, 0), len(orig)):
        if orig[position].strip() == anchor:
            return position
    for position in range(0, len(orig)):
        if orig[position].strip() == anchor:
            return position
    return -1


def _render_at(
    orig: list[str], section: list[tuple[str, str]], start: int,
) -> tuple[tuple[list[str], int] | None, _Mismatch | None, int]:
    """Try to apply ``section`` with its first anchor line at ``orig[start]``.

    Returns ``(result, mismatch, matched_count)``. ``result`` is the
    rendered replacement lines and the file index just past the matched region
    on success, or ``None`` on mismatch, with the failing context/del line and
    its offsets. File blank lines that the patch
    context omits are skipped and preserved.
    """
    out: list[str] = []
    fp = start
    matched = 0
    for offset, (kind, text) in enumerate(section):
        if kind == "ins":
            out.append(text)
            continue
        # context or del: align to the next matching file line, preserving any
        # intervening file blank lines that the patch context left out.
        while fp < len(orig) and _is_blank(orig[fp]) and not _is_blank(text):
            out.append(orig[fp])
            fp += 1
        if fp >= len(orig) or not _lines_equal(orig[fp], text):
            return None, _Mismatch(text, offset, kind, fp), matched
        if kind == "context":
            out.append(orig[fp])
        fp += 1
        matched += 1
    return (out, fp), None, matched


def _apply_section(
    orig: list[str], section: list[tuple[str, str]], cursor: int,
) -> tuple[list[str], int, int, bool, _Mismatch | None]:
    """Locate and render one section at or after ``cursor``.

    Returns ``(rendered, start, end, changed, mismatch)``; ``start == -1``
    means the context was not found. The mismatch identifies the specific
    line and offsets so the model can correct its quoted text.
    """
    did_change = any(kind in ("del", "ins") for kind, _ in section)
    anchor = next((text for kind, text in section if kind in ("context", "del")), None)
    if anchor is None:
        # A section of pure insertions with no context lands at the cursor.
        rendered = [text for kind, text in section if kind == "ins"]
        return rendered, cursor, cursor, did_change, None
    best_fail: _Mismatch | None = None
    best_matched = -1
    for start in range(cursor, len(orig)):
        if not _lines_equal(orig[start], anchor):
            continue
        result, fail_line, matched = _render_at(orig, section, start)
        if result is not None:
            rendered, end = result
            return rendered, start, end, did_change, None
        if matched > best_matched:
            best_matched = matched
            best_fail = fail_line
    # Report the deepest partial match's failing line, or the anchor if the
    # section never matched at all, so the error is actionable.
    if best_fail is None:
        offset = next(index for index, (kind, _) in enumerate(section)
                      if kind in ("context", "del"))
        best_fail = _Mismatch(anchor, offset, section[offset][0])
    return [], -1, -1, did_change, best_fail


def parse_v4a_patch(patch: str) -> str:
    """Validate envelope and hunk structure before approval; return the path.

    The location-sensitive matching is deferred to :func:`apply_v4a_update`,
    which has the current file content.
    """
    path, body = _split_envelope(patch)
    index = 0
    any_change = False
    while index < len(body):
        if body[index].startswith("***"):
            break
        if body[index].startswith("@@"):
            index += 1
            continue
        section, index = _read_section(body, index)
        if any(kind in ("del", "ins") for kind, _ in section):
            any_change = True
    if not any_change:
        raise V4APatchFormatError("V4A patch contains no update hunk")
    return path


def _ambiguous_conflict(
    path: str, original: str, error: AmbiguousLineMatch, hunk: int, patch_line: int,
) -> WorkspacePatchConflict:
    lines = original.splitlines()
    locations = []
    for match in error.matches[:3]:
        end = min(len(lines), match.end)
        locations.append({"start_line": match.start + 1, "end_line": end,
                          "content": "\n".join(line[:200] for line in lines[match.start:min(end, match.start + 5)]),
                          "content_truncated": end - match.start > 5
                          or any(len(line) > 200 for line in lines[match.start:end])})
    return WorkspacePatchConflict(
        path, 0, f"patch hunk {hunk} is ambiguous: {error}; add unique context or an @@ anchor",
        diagnostics={"path": path, "hunk_number": hunk, "patch_line_number": patch_line,
                     "match_failure": "ambiguous", "candidate_count": len(error.matches),
                     "candidate_count_is_lower_bound": True,
                     "match_strategy": error.matches[0].strategy,
                     "snapshot_sha256": sha256(original.encode()).hexdigest(),
                     "candidate_locations": locations, "candidates_are_suggestions": True},
    )


def _render_match(
    orig: list[str], section: list[tuple[str, str]], match: LineMatch, crlf: bool,
) -> list[str]:
    rendered = []
    positions = iter(match.positions)
    cursor = match.start
    for kind, text in section:
        if kind == "ins":
            rendered.append(text.rstrip("\r") + "\r" if crlf else text)
        else:
            position = next(positions)
            rendered.extend(orig[cursor:position])
            if kind == "context":
                # A fuzzy/normalized quote must not rewrite unchanged source.
                rendered.append(orig[position])
            cursor = position + 1
    return rendered


def plan_v4a_update(path: str, original: str, patch: str) -> V4APlan:
    """Return the new contents and the actual locations used by all hunks."""
    _, body = _split_envelope(patch)
    orig = original.split("\n")
    crlf = "\r\n" in original and "\n" not in original.replace("\r\n", "")
    out: list[str] = []
    cursor = 0
    index = 0
    changed = False
    hunk_number = 0
    matches: list[dict[str, Any]] = []
    while index < len(body):
        line = body[index]
        if line.startswith("***"):
            break
        if line.startswith("@@"):
            hunk_number += 1
            anchor = line[2:].strip()
            anchor_patch_line = index + 3
            index += 1
            if anchor:
                try:
                    location = locate_lines(orig, [anchor], cursor)
                except AmbiguousLineMatch as error:
                    raise _ambiguous_conflict(path, original, error, hunk_number, anchor_patch_line) from error
                if location is None:
                    raise _context_conflict(
                        path, original, _Mismatch(anchor, 0, "anchor"),
                        hunk_number=hunk_number, patch_line_number=anchor_patch_line,
                        cursor=cursor,
                    )
                out.extend(orig[cursor:location.end])
                cursor = location.end
            continue
        section_start = index
        section, index = _read_section(body, index)
        if not section:
            continue
        hunk_number = max(1, hunk_number)
        old = [text for kind, text in section if kind != "ins"]
        new = [text for kind, text in section if kind != "del"]
        did_change = any(kind in ("del", "ins") for kind, _ in section)
        try:
            match = locate_lines(orig, old, cursor, allow_similarity=did_change and old != new)
        except AmbiguousLineMatch as error:
            raise _ambiguous_conflict(path, original, error, hunk_number, section_start + 3) from error
        if match is None:
            # Preserve detailed first-mismatch diagnostics when every matching
            # tier fails. This probe does not choose a write location.
            _, _, _, _, unmatched = _apply_section(orig, section, cursor)
            assert unmatched is not None
            raise _context_conflict(
                path, original, unmatched,
                hunk_number=hunk_number,
                patch_line_number=section_start + unmatched.section_offset + 3,
                cursor=cursor,
                hunk_changes_content=(
                    [text for kind, text in section if kind != "ins"]
                    != [text for kind, text in section if kind != "del"]
                ),
            )
        out.extend(orig[cursor:match.start])
        out.extend(_render_match(orig, section, match, crlf))
        if did_change:
            matches.append({"hunk_number": hunk_number, "strategy": match.strategy,
                            "start_line": match.start + 1, "end_line": match.end,
                            "approximate": match.strategy == "anchored_similarity"})
        cursor = match.end
        changed = changed or did_change
    if not changed:
        raise V4APatchFormatError("V4A patch contains no update hunk")
    out.extend(orig[cursor:])
    return V4APlan("\n".join(out), tuple(matches))


def apply_v4a_update(path: str, original: str, patch: str) -> str:
    return plan_v4a_update(path, original, patch).content
