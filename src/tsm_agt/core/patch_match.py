"""Ordered line matching for workspace edits; never writes files.

Exact-first normalization follows Codex's seek_sequence. The final, bounded
similarity pass follows Hermes's anchored, per-line comparison: every nonblank
line must qualify, both boundary lines must match, and the result must be unique.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable


_PUNCTUATION_GROUPS = {
        "-": "‐‑‒–—―−",
        "'": "‘’‚‛",
        '"': "“”„‟",
        " ": "\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000",
}
_PUNCTUATION_TABLE = {
    ord(char): replacement for replacement, chars in _PUNCTUATION_GROUPS.items() for char in chars
}


def normalize_punctuation(text: str) -> str:
    return text.translate(_PUNCTUATION_TABLE).strip()


@dataclass(frozen=True)
class LineMatch:
    start: int
    end: int
    positions: tuple[int, ...]
    strategy: str


class AmbiguousLineMatch(ValueError):
    def __init__(self, matches: list[LineMatch]):
        super().__init__(f"found at least {len(matches)} matching ranges")
        self.matches = matches


def _unique(matches: list[LineMatch]) -> LineMatch | None:
    if len(matches) > 1:
        raise AmbiguousLineMatch(matches)
    return matches[0] if matches else None


def _align(
    lines: list[str], pattern: list[str], start: int,
    transform: Callable[[str], str], strategy: str,
    skip_blank_lines: bool = False,
) -> LineMatch | None:
    positions = []
    cursor = start
    for expected in pattern:
        # Retain the existing ability to preserve source blank lines omitted
        # from the patch. No nonblank source line is skipped.
        while (skip_blank_lines and cursor < len(lines)
               and not lines[cursor].strip() and expected.strip()):
            cursor += 1
        if cursor >= len(lines) or transform(lines[cursor]) != transform(expected):
            return None
        positions.append(cursor)
        cursor += 1
    return LineMatch(start, cursor, tuple(positions), strategy)


def locate_lines(
    lines: list[str], pattern: list[str], start: int = 0, *,
    allow_similarity: bool = False,
) -> LineMatch | None:
    if not pattern:
        return LineMatch(start, start, (), "insertion")
    stages = (
        ("exact", lambda text: text),
        ("trailing_whitespace", str.rstrip),
        ("trimmed_whitespace", str.strip),
        ("unicode_normalized", normalize_punctuation),
    )
    for skip_blanks in (False, True):
        for stage, transform in stages:
            strategy = "blank_lines_" + stage if skip_blanks else stage
            matches = []
            first = transform(pattern[0])
            for index in range(start, len(lines)):
                if transform(lines[index]) != first:
                    continue
                match = _align(lines, pattern, index, transform, strategy, skip_blanks)
                if match is not None:
                    matches.append(match)
                    if len(matches) == 2:
                        # Two matches already prove ambiguity. Do not retain a
                        # potentially enormous set of repetitive source ranges.
                        raise AmbiguousLineMatch(matches)
            if matches:
                return _unique(matches)
    if not allow_similarity:
        return None

    meaningful = [i for i, text in enumerate(pattern) if text.strip()]
    if len(meaningful) < 3:
        return None
    first, last = meaningful[0], meaningful[-1]
    expected = [normalize_punctuation(text) for text in pattern]
    matches = []
    for index in range(start, len(lines) - len(pattern) + 1):
        if (normalize_punctuation(lines[index + first]) != expected[first]
                or normalize_punctuation(lines[index + last]) != expected[last]):
            continue
        for offset, quoted in enumerate(expected):
            actual = normalize_punctuation(lines[index + offset])
            if actual == quoted:
                continue
            # Similarity never skips lines or changes the inferred block size.
            # Bound expensive character comparisons on model-controlled text.
            if (not actual or not quoted or max(len(actual), len(quoted)) > 4096
                    or SequenceMatcher(None, quoted, actual, autojunk=False).ratio() < 0.80):
                break
        else:
            matches.append(LineMatch(index, index + len(pattern),
                                     tuple(range(index, index + len(pattern))),
                                     "anchored_similarity"))
            if len(matches) == 2:
                raise AmbiguousLineMatch(matches)
    return _unique(matches)
