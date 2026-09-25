"""Guards against Python swallowing JS escape sequences inside INDEX_HTML.

``INDEX_HTML`` is a plain (non-raw) triple-quoted string, so a JS literal such
as ``'\\n'`` written with a single backslash is consumed by Python and emitted
as a real newline. That breaks the single-quoted JS string, which makes the
whole ``<script>`` block a syntax error and silently disables the entire UI.
Escapes intended for JS must be doubled (``'\\\\n'``).
"""

from __future__ import annotations

import inspect
import pathlib
import re

import tsm_agt.web.app as web_app

# A single backslash followed by a JS escape letter: Python eats it before JS
# ever sees it.
_SWALLOWED_ESCAPE_RE = re.compile(r"(?<!\\)\\[ntrbfv0]")
_INDEX_HTML_RE = re.compile(
    r'INDEX_HTML = """(.*?)"""\.replace\(', re.S
)
_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S)


def _index_html_source() -> str:
    source = pathlib.Path(inspect.getsourcefile(web_app) or "").read_text(
        encoding="utf-8"
    )
    match = _INDEX_HTML_RE.search(source)
    assert match, "could not locate the INDEX_HTML literal in web/app.py"
    return match.group(1)


def test_index_html_source_has_no_swallowed_js_escapes() -> None:
    offenders: list[str] = []
    for lineno, line in enumerate(_index_html_source().splitlines(), start=1):
        if _SWALLOWED_ESCAPE_RE.search(line):
            offenders.append(f"{lineno}: {line.strip()}")
    assert not offenders, (
        "single-backslash escapes inside INDEX_HTML are consumed by Python and "
        "break the rendered <script>; double them (e.g. '\\\\n'):\n"
        + "\n".join(offenders)
    )


def test_index_html_script_has_balanced_brackets() -> None:
    scripts = _SCRIPT_RE.findall(web_app.INDEX_HTML)
    assert scripts, "INDEX_HTML must contain at least one <script> block"
    for script in scripts:
        assert script.count("{") == script.count("}")
        assert script.count("(") == script.count(")")


def test_render_markdown_collapses_single_blank_line_without_extra_breaks() -> None:
    source = _index_html_source()

    assert "let blankLineCount = 0;" in source
    assert "blankLineCount += 1;" in source
    assert "if (blankLineCount >= 2)" in source
    assert "paragraph.push('');" not in source
    assert 'blocks.push(`<ul class="markdown-list">${listItems.join(\'\')}</ul>`);' in source
    assert source.count("split(String.fromCharCode(10))") == 4
    assert source.count("join(String.fromCharCode(10))") == 2
    assert "function isMarkdownProgress(text)" in source
    assert "line.innerHTML = renderMarkdown(item.text);" in source
