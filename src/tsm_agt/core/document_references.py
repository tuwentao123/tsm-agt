"""Explicit remote-document reference extraction."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_URL_PATTERN = re.compile(r"https?://[^\s<>\"'`()\[\]{}]+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?"


def extract_document_references(text: str) -> tuple[dict[str, str], ...]:
    """Return stable, de-duplicated http(s) document references from user text."""
    references: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _URL_PATTERN.finditer(text):
        raw_url = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        try:
            parsed = urlsplit(raw_url)
        except ValueError:
            # An invalid link candidate in prose is not a valid document
            # reference, and must not prevent processing the rest of the input.
            continue
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            continue
        url = urlunsplit((
            parsed.scheme.lower(), parsed.netloc, parsed.path,
            parsed.query, parsed.fragment,
        ))
        if url in seen:
            continue
        seen.add(url)
        references.append({
            "reference_type": "DOCUMENT_URL",
            "url": url,
            "scheme": parsed.scheme.lower(),
        })
    return tuple(references)
