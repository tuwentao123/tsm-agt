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


def test_restored_approval_is_rendered_in_a_fixed_conversation_dock() -> None:
    """A multi-turn Task must not bury or scroll a pending approval."""

    source = _index_html_source()

    assert "const pendingApprovals = [];" in source
    assert "Pending approvals are actionable session state" in source
    assert "...pendingApprovals," in source
    assert 'id=\\"approval-dock\\"' in source
    assert ".approval-dock{display:none;position:absolute" in source
    assert "approvalDock.appendChild(element);" in source
    assert "approvalDock.classList.add('visible');" in source
    assert "chatArea.classList.add('has-approval-dock');" in source


def test_composer_stays_outside_the_scrollable_conversation_stage() -> None:
    """Long transcripts or an approval dock must never push input off-screen."""

    source = _index_html_source()

    assert "html,body{height:100%;overflow:hidden}" in source
    assert ".chat-shell{flex:1 1 0;min-height:0;height:0" in source
    assert ".workspace-stage{flex:1 1 0;min-height:0;height:100%" in source
    assert ".composer{flex:0 0 auto;position:relative;z-index:30" in source


def test_approval_dock_uses_a_compact_summary_with_optional_details() -> None:
    source = _index_html_source()

    assert 'heading.innerHTML = `' in source
    assert '<div class="approval-title">需要授权</div>' in source
    assert "summary.textContent = '查看完整操作详情';" in source
    assert "makeApprovalItem('动作', approval.action, true)" in source
    assert ".approval-dock .message.approval{width:100%;max-width:none;min-height:0" in source


def test_unfinished_turn_trace_is_a_sibling_of_its_input_bubble() -> None:
    """A full-width trace must not stretch a right-aligned user message."""

    source = _index_html_source()

    assert ".turn-trace{align-self:stretch;width:100%;max-width:none" in source
    assert "inputTrace = document.createElement('div');" in source
    assert "inputTrace.className = 'turn-trace';" in source
    assert "if (inputTrace) chatArea.appendChild(inputTrace);" in source


def test_composer_suggest_menu_lists_available_skills() -> None:
    """Typing "/skill" must offer discovered Skills from the settings API."""

    source = _index_html_source()

    assert "{ prefix: '/skill ', label: '/skill <名称>'" in source
    assert "const SKILL_MENU_PREFIX = '/skill';" in source
    assert "async function loadSkillMenuSkills() {" in source
    assert "const response = await fetch('/settings/skills');" in source
    assert "const matched = skillMenuSkills.filter(skill => !needle" in source
    assert "skill.name.toLowerCase().includes(needle)" in source
    # 选中 Skill 后插入的是让模型先读 SKILL.md 的引用句，而不是立即执行。
    assert "function skillMentionText(name) {" in source
    assert "使用 Skill「${name}」：先用 skill.load 读取 SKILL.md" in source
    assert "applySkillMention(entry.skill, input);" in source


def test_composer_suggest_menu_uses_the_rendered_entry_list() -> None:
    """Arrow keys and Enter must follow the visible list, not the raw command array."""

    source = _index_html_source()

    assert "slashMenuItems = resolveSlashMenuItems(anchorInput.value);" in source
    assert "const total = slashMenuItems.length;" in source
    assert "const entry = slashMenuItems[slashMenuActiveIndex];" in source
    assert "applySlashMenuItem(entry, anchorInput);" in source


def test_composer_suggest_menu_matches_skill_names_without_the_skill_prefix() -> None:
    """Typing "/ph" must offer installed Skills, not only after "/skill"."""

    source = _index_html_source()

    assert "const DIRECT_SKILL_MENU_LIMIT = 8;" in source
    assert "function resolveDirectSkillItems(query) {" in source
    assert "skill.name.toLowerCase().startsWith(query)" in source
    assert "return [...commandEntries, ...resolveDirectSkillItems(query)];" in source
    # 直接输入 "/ph" 选中后，触发文本必须被整段替换成引用句。
    assert "function isDirectSkillQuery(value) {" in source
    assert "if (isSkillMenuMode(value) || isDirectSkillQuery(value)) {" in source
    assert "if (promptComposer && promptComposer.value.startsWith('/')) {" in source


def test_composer_suggest_menu_keeps_the_highlighted_entry_in_view() -> None:
    """The keyboard selection must scroll inside the menu instead of being clipped."""

    source = _index_html_source()

    assert "function revealSlashMenuItem(element) {" in source
    assert "const visibleBottom = visibleTop + menu.clientHeight;" in source
    assert "menu.scrollTop = itemBottom - menu.clientHeight;" in source
    assert "menu.scrollTop = itemTop;" in source
    assert "revealSlashMenuItem(children[index]);" in source
    # 新一次渲染不能沿用上一次的滚动位置。
    assert "menu.scrollTop = 0;" in source


def test_tool_calls_render_as_collapsible_execution_cards() -> None:
    """A command shows its real output inside a card that can be collapsed."""

    source = _index_html_source()

    assert "function toolExecutionCard(model) {" in source
    assert "function toolCardModel(progress) {" in source
    assert "card.dataset.open = initiallyOpen ? 'true' : 'false';" in source
    assert "TOOL_CARD_OPEN_STATE.set(stateKey, next);" in source
    assert "body.hidden = !next;" in source
    assert "body.hidden = !initiallyOpen;" in source
    assert "toolCardSection('参数', argumentsText)" in source
    assert "'输出'," in source
    assert "该工具不产生命令行输出" in source
    assert ".live-turn-line{font-size:13px;font-weight:600;color:#6b7280;" in source
    # 阶段性回复是正文片段：单独上下间距，不跟随进度行的灰色小字。
    assert ".live-turn-line.turn-progress-note{margin:10px 0;" in source
    assert "note: kind === 'commentary'," in source
    assert "if (line.note) row.classList.add('turn-progress-note');" in source
    # 参数/输出区要像 markdown 代码块：等宽、挂缩进、高行高。
    assert ".tool-card-pre{margin:0;padding:10px 12px 10px 26px;text-indent:-14px;" in source
    assert "line-height:1.75" in source
    assert "card: toolCardModel({ ...item, tool_name: name })," in source
    assert "holder.appendChild(toolExecutionCard(card));" in source
    assert ".tool-card-head{display:flex;" in source
    # display 作者样式会盖掉 [hidden]，必须显式声明收起态。
    assert ".tool-card-body{display:flex" in source
    assert ".tool-card-body[hidden]{display:none}" in source
